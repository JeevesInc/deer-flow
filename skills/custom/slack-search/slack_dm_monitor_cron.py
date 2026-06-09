#!/usr/bin/env python3
"""Slack DM Monitor Cron — watches inbound DMs to the bot and handles them autonomously.

Behavior:
  - Every 2 minutes: check all IM channels for new messages FROM the other person
  - For each new message, dispatch to the DeerFlow agent with full context
  - Every hour: send Brian a summary of all messages handled
  - State is persisted so we never double-handle a message

Env vars required:
  - SLACK_BOT_TOKEN
  - SLACK_OWNER_USER_ID  (Brian's user ID)
"""

import json
import logging
import os
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / '_shared'))
from env_loader import load_env
load_env()

logging.basicConfig(
    level=logging.INFO,
    format='[SlackDMMonitor %(asctime)s] %(message)s',
    datefmt='%H:%M:%S',
)
log = logging.getLogger('slack_dm_monitor')

POLL_INTERVAL = int(os.environ.get('SLACK_DM_MONITOR_INTERVAL', '120'))   # 2 min
SUMMARY_INTERVAL = int(os.environ.get('SLACK_DM_SUMMARY_INTERVAL', '3600'))  # 1 hour

IGNORED_USERS = {'USLACKBOT'}
BRIAN_USER_ID = os.environ.get('SLACK_OWNER_USER_ID', 'U05B5HGNCN9')


def _state_path() -> str:
    here = Path(__file__).resolve()
    backend = here.parents[3] / 'backend'
    return str(backend / '.deer-flow' / '_slack_dm_monitor_state.json')


def _load_state() -> dict:
    path = _state_path()
    if os.path.exists(path):
        try:
            with open(path) as f:
                return json.load(f)
        except Exception:
            pass
    return {'seen_ts': {}, 'handled': [], 'last_summary': None}


def _save_state(state: dict) -> None:
    path = _state_path()
    if len(state.get('handled', [])) > 500:
        state['handled'] = state['handled'][-500:]
    with open(path, 'w') as f:
        json.dump(state, f, indent=2)


def _get_client():
    token = os.environ.get('SLACK_BOT_TOKEN')
    from slack_sdk import WebClient
    return WebClient(token=token)


def _get_brian_dm_channel(client) -> str:
    result = client.conversations_list(types='im', limit=100)
    for ch in result['channels']:
        if ch.get('user') == BRIAN_USER_ID:
            return ch['id']
    raise RuntimeError(f'Could not find DM channel with Brian ({BRIAN_USER_ID})')


def _get_im_channels(client) -> list:
    result = client.conversations_list(types='im', limit=100)
    return result.get('channels', [])


def _get_display_name(client, user_id: str) -> str:
    try:
        user_token = os.environ.get('SLACK_USER_TOKEN')
        from slack_sdk import WebClient as WC
        uc = WC(token=user_token) if user_token else client
        result = uc.users_info(user=user_id)
        profile = result['user']['profile']
        return profile.get('display_name') or profile.get('real_name') or user_id
    except Exception:
        return user_id


def _fetch_new_messages(client, channel_id: str, other_user_id: str, since_ts) -> list:
    kwargs = {'channel': channel_id, 'limit': 20}
    if since_ts:
        kwargs['oldest'] = since_ts

    result = client.conversations_history(**kwargs)
    messages = result.get('messages', [])

    new_msgs = []
    for m in messages:
        msg_user = m.get('user', '')
        msg_ts = m.get('ts', '0')
        if msg_user in (BRIAN_USER_ID, '', None) or msg_user in IGNORED_USERS:
            continue
        if m.get('bot_id'):
            continue
        if msg_user != other_user_id:
            continue
        if since_ts and float(msg_ts) <= float(since_ts):
            continue
        new_msgs.append(m)

    return new_msgs


def _dispatch_message(client, channel_id: str, sender_name: str, sender_id: str, message: dict) -> bool:
    text = message.get('text', '')
    ts = message.get('ts', '')
    dt = datetime.fromtimestamp(float(ts)).strftime('%Y-%m-%d %H:%M ET') if ts else 'unknown time'

    log.info("Dispatching message from %s: %s", sender_name, text[:80])

    try:
        from dispatch_queue import enqueue_or_dispatch
        enqueue_or_dispatch(
            (
                f"A Slack DM was received on behalf of Brian Mauck from {sender_name} "
                f"(Slack ID: {sender_id}) at {dt}.\n\n"
                f"Message: \"{text}\"\n\n"
                f"Handle this as Brian's AI assistant. Check STRATEGIC_CONTEXT.md for context on who this "
                f"person is and any active workstreams. Take the appropriate action — e.g., if they're "
                f"responding to an NB scheduling request, note their preferred time and let Brian know; "
                f"if they have a question, draft a response for Brian. Post your assessment and actions "
                f"to Brian's Slack DM."
            ),
            notification=f":speech_balloon: New DM from *{sender_name}*: _{text[:120]}_",
            category="slack-dm",
            source_id=f"slack-dm-{channel_id}-{ts}",
            source_metadata={
                'sender_name': sender_name,
                'sender_id': sender_id,
                'channel_id': channel_id,
                'ts': ts,
                'text': text,
            },
        )
        return True
    except Exception as e:
        log.error("Failed to dispatch from %s: %s", sender_name, e)
        return False


def _send_hourly_summary(client, brian_dm: str, handled: list) -> None:
    if not handled:
        return

    now_str = datetime.now().strftime('%b %d, %I:%M %p')
    lines = [f":memo: *Slack DM Monitor — Hourly Summary* ({now_str})\n"]
    for item in handled:
        lines.append(f"• *{item['sender_name']}* at {item['time']}: _{item['text'][:100]}_")
    lines.append("\nAll messages dispatched to me for handling.")

    try:
        client.chat_postMessage(channel=brian_dm, text="\n".join(lines))
        log.info("Sent hourly summary (%d items)", len(handled))
    except Exception as e:
        log.error("Failed to send hourly summary: %s", e)


def run_loop():
    log.info("Slack DM Monitor starting (poll=%ds, summary=%ds)", POLL_INTERVAL, SUMMARY_INTERVAL)

    client = _get_client()
    brian_dm = _get_brian_dm_channel(client)
    log.info("Brian DM channel: %s", brian_dm)

    state = _load_state()
    last_summary_str = state.get('last_summary')
    last_summary = datetime.fromisoformat(last_summary_str) if last_summary_str else datetime.now()
    pending_summary = []

    while True:
        try:
            channels = _get_im_channels(client)

            for ch in channels:
                other_user = ch.get('user', '')
                channel_id = ch.get('id', '')

                if other_user == BRIAN_USER_ID or other_user in IGNORED_USERS:
                    continue

                since_ts = state['seen_ts'].get(channel_id)
                new_msgs = _fetch_new_messages(client, channel_id, other_user, since_ts)

                if new_msgs:
                    sender_name = _get_display_name(client, other_user)
                    log.info("%d new msg(s) from %s", len(new_msgs), sender_name)

                    for msg in new_msgs:
                        success = _dispatch_message(client, channel_id, sender_name, other_user, msg)
                        if success:
                            item = {
                                'sender_name': sender_name,
                                'sender_id': other_user,
                                'channel_id': channel_id,
                                'ts': msg['ts'],
                                'time': datetime.fromtimestamp(float(msg['ts'])).strftime('%I:%M %p'),
                                'text': msg.get('text', ''),
                                'dispatched_at': datetime.now().isoformat(),
                            }
                            state['handled'].append(item)
                            pending_summary.append(item)

                        cur_seen = state['seen_ts'].get(channel_id, '0')
                        if float(msg['ts']) > float(cur_seen):
                            state['seen_ts'][channel_id] = msg['ts']

            _save_state(state)

        except Exception as e:
            log.error("Poll error: %s", e)

        now = datetime.now()
        if (now - last_summary).total_seconds() >= SUMMARY_INTERVAL:
            _send_hourly_summary(client, brian_dm, pending_summary)
            pending_summary = []
            last_summary = now
            state['last_summary'] = now.isoformat()
            _save_state(state)

        time.sleep(POLL_INTERVAL)


if __name__ == '__main__':
    run_loop()
