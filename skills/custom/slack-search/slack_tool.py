#!/usr/bin/env python3
"""Slack tool: search workspace messages, look up users, and send DMs.

Usage:
    python slack_tool.py search "query" [--days 30] [--count 20]
    python slack_tool.py lookup <email>
    python slack_tool.py send <user-id-or-email> "message text"

Search and lookup use the user token (SLACK_USER_TOKEN, xoxp-...).
Send uses the bot token (SLACK_BOT_TOKEN, xoxb-...) so recipients see
the bot's identity, not Brian's.
"""

import os
import sys
from datetime import datetime, timedelta
from pathlib import Path


def _slack_sdk():
    try:
        from slack_sdk import WebClient
    except ImportError:
        import subprocess
        subprocess.check_call([sys.executable, '-m', 'pip', 'install', '-q', 'slack_sdk'])
        from slack_sdk import WebClient
    return WebClient


def _get_client():
    """User-token client for search/lookup (xoxp)."""
    token = os.environ.get('SLACK_USER_TOKEN')
    if not token:
        print("ERROR: Missing SLACK_USER_TOKEN environment variable.", file=sys.stderr)
        print("Add a Slack user token (xoxp-...) with search:read, users:read, users:read.email scopes.", file=sys.stderr)
        sys.exit(1)
    WebClient = _slack_sdk()
    return WebClient(token=token)


def _get_bot_client():
    """Bot-token client for send (xoxb) so messages appear from the bot, not Brian."""
    token = os.environ.get('SLACK_BOT_TOKEN')
    if not token:
        print("ERROR: Missing SLACK_BOT_TOKEN environment variable.", file=sys.stderr)
        print("Add a Slack bot token (xoxb-...) with chat:write scope.", file=sys.stderr)
        sys.exit(1)
    WebClient = _slack_sdk()
    return WebClient(token=token)


def cmd_search(query, days=30, count=20):
    """Search Slack messages using Slack search syntax."""
    client = _get_client()

    # Build the query with date filter if not already present
    if 'after:' not in query and 'before:' not in query:
        after_date = (datetime.now() - timedelta(days=days)).strftime('%Y-%m-%d')
        full_query = f"{query} after:{after_date}"
    else:
        full_query = query

    try:
        result = client.search_messages(query=full_query, count=count, sort='timestamp', sort_dir='desc')
    except Exception as e:
        print(f"ERROR: Slack search failed: {e}", file=sys.stderr)
        sys.exit(1)

    messages = result.get('messages', {}).get('matches', [])
    total = result.get('messages', {}).get('total', 0)

    if not messages:
        print(f'No messages found matching: {query}')
        return

    print(f'Found {total} message(s) matching "{query}" (showing {len(messages)}):\n')

    for msg in messages:
        ts = msg.get('ts', '')
        try:
            dt = datetime.fromtimestamp(float(ts))
            date_str = dt.strftime('%Y-%m-%d %H:%M')
        except (ValueError, OSError):
            date_str = ts

        channel_name = msg.get('channel', {}).get('name', 'unknown')
        channel_is_im = msg.get('channel', {}).get('is_im', False)
        if channel_is_im:
            channel_label = 'DM'
        else:
            channel_label = f'#{channel_name}'

        username = msg.get('username', '') or msg.get('user', 'unknown')
        text = msg.get('text', '').replace('\n', '\n  ')

        # Truncate long messages
        if len(text) > 300:
            text = text[:300] + '...'

        print(f'[{date_str}] {channel_label} \u2014 {username}')
        print(f'  {text}')
        print()


def cmd_lookup(email):
    """Look up a Slack user by email address."""
    client = _get_client()

    try:
        result = client.users_lookupByEmail(email=email)
    except Exception as e:
        err = str(e)
        if 'users_not_found' in err:
            print(f'No Slack user found for email: {email}')
            return
        print(f"ERROR: Slack lookup failed: {e}", file=sys.stderr)
        sys.exit(1)

    user = result.get('user', {})
    user_id = user.get('id', 'N/A')
    real_name = user.get('real_name', user.get('name', 'N/A'))
    display_name = user.get('profile', {}).get('display_name', '')
    title = user.get('profile', {}).get('title', '')
    tz = user.get('tz_label', '')

    print(f'User found:')
    print(f'  ID:           {user_id}')
    print(f'  Name:         {real_name}')
    if display_name:
        print(f'  Display name: {display_name}')
    if title:
        print(f'  Title:        {title}')
    if tz:
        print(f'  Timezone:     {tz}')
    print(f'  Email:        {email}')


def cmd_send(recipient, message):
    """Send a DM via the bot identity. Recipient can be a Slack user_id (UXXX/WXXX) or an email.

    Logs `[Slack outbound]` to bot_dm_history.log so the message shows up in
    Grafana right away — no need to wait for the polling reconciliation.
    """
    if not message or not message.strip():
        print("ERROR: empty message — refusing to send.", file=sys.stderr)
        sys.exit(1)

    bot = _get_bot_client()
    owner_id = os.environ.get('SLACK_OWNER_USER_ID', '').strip()

    # Resolve recipient → user_id
    user_id = recipient.strip()
    if '@' in user_id:
        # Treat as email; needs user token for users.lookupByEmail
        try:
            user_client = _get_client()
            resp = user_client.users_lookupByEmail(email=user_id)
            user_id = resp['user']['id']
        except Exception as e:
            print(f"ERROR: couldn't look up {recipient}: {e}", file=sys.stderr)
            sys.exit(1)
    elif not (user_id.startswith('U') or user_id.startswith('W')):
        print(f"ERROR: recipient must be a Slack user_id (UXXX/WXXX) or an email; got: {recipient}", file=sys.stderr)
        sys.exit(1)

    # Resolve display name for the audit log (best-effort).
    recipient_name = user_id
    try:
        info = bot.users_info(user=user_id)
        prof = info.get('user', {}).get('profile', {})
        recipient_name = prof.get('real_name') or prof.get('display_name') or user_id
    except Exception:
        pass

    # Open the DM channel.
    try:
        dm = bot.conversations_open(users=[user_id])
        channel_id = dm['channel']['id']
    except Exception as e:
        print(f"ERROR: couldn't open DM with {user_id} ({recipient_name}): {e}", file=sys.stderr)
        sys.exit(1)

    # Send.
    try:
        resp = bot.chat_postMessage(
            channel=channel_id,
            text=message,
            unfurl_links=False,
            unfurl_media=False,
        )
        ts = resp.get('ts', '')
    except Exception as e:
        print(f"ERROR: chat_postMessage to {user_id} ({recipient_name}) failed: {e}", file=sys.stderr)
        sys.exit(1)

    # Resolve bot's own user_id for the audit line.
    bot_user_id = ''
    try:
        bot_user_id = bot.auth_test().get('user_id', '')
    except Exception:
        pass

    # Append to the unified bot DM audit log so Grafana sees it instantly.
    # Walk up looking for the repo root (marker = start.sh) to be robust to
    # being called from inside the sandbox path-mapped tree.
    try:
        log_path = None
        cur = Path(__file__).resolve().parent
        for _ in range(8):
            if (cur / "start.sh").exists():
                log_path = cur / "bot_dm_history.log"
                break
            if cur.parent == cur:
                break
            cur = cur.parent
        if log_path is None:
            # Fall back to a path the cron also uses, derived absolutely.
            log_path = Path("/c/Jeeves/redshift-bot/bot_dm_history.log")
        text_for_log = message.replace('\n', '\\n').replace('\r', ' ')
        try:
            when = datetime.fromtimestamp(float(ts)).strftime('%Y-%m-%dT%H:%M:%S') if ts else ''
        except Exception:
            when = ts
        line = (
            f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')} "
            f"[Slack outbound] sent_at={when} ts={ts} "
            f"to_user_id={user_id} to_user_name={recipient_name!r} "
            f"channel={channel_id} bot_user_id={bot_user_id} "
            f"sender=slack-send-tool "
            f"text={message!r}"
        )
        with log_path.open('a', encoding='utf-8') as f:
            f.write(line + '\n')
    except Exception as e:
        # Don't fail the send if logging fails.
        print(f"WARN: audit log append failed: {e}", file=sys.stderr)

    # Friendly stdout — the agent will read this back.
    print(f"Sent to {recipient_name} ({user_id}) at {ts}. Logged for owner audit.")
    if owner_id and user_id == owner_id:
        print("(Note: this is the owner. The polling cron filters this out of the dashboard.)")


def cmd_allowlist(action, target=None):
    """Manage the inbound DM allowlist (backend/.deer-flow/_slack_dm_allowlist.json).

    Senders not on this list get no response from the DM monitor; Brian is
    notified instead. Only add people Brian has explicitly authorized.
    """
    import json as _json
    from datetime import datetime as _dt
    al_path = None
    cur = Path(__file__).resolve().parent
    for _ in range(8):
        cand = cur / 'backend' / '.deer-flow' / '_slack_dm_allowlist.json'
        if cand.parent.exists():
            al_path = cand
            break
        if cur.parent == cur:
            break
        cur = cur.parent
    if al_path is None:
        print('ERROR: could not locate backend/.deer-flow directory', file=sys.stderr)
        sys.exit(1)
    data = {'allowed': {}}
    if al_path.exists():
        data = _json.loads(al_path.read_text())
    allowed = data.setdefault('allowed', {})

    if action == 'list':
        for uid, meta in allowed.items():
            print('{0}  {1}  (added {2}; {3})'.format(uid, meta.get('name', '?'), meta.get('added', '?'), meta.get('reason', '')))
        return

    if not target:
        print('ERROR: add/remove require a user_id or email', file=sys.stderr)
        sys.exit(1)

    # Resolve email -> user_id
    user_id = target.strip()
    name = user_id
    if '@' in user_id:
        client = _get_client()
        resp = client.users_lookupByEmail(email=user_id)
        user_id = resp['user']['id']
        name = resp['user']['profile'].get('real_name') or user_id
    else:
        try:
            info = _get_bot_client().users_info(user=user_id)
            name = info['user']['profile'].get('real_name') or user_id
        except Exception:
            pass

    if action == 'add':
        allowed[user_id] = {'name': name, 'added': _dt.now().isoformat(timespec='seconds'), 'reason': 'authorized by Brian'}
        data['updated'] = _dt.now().isoformat(timespec='seconds')
        al_path.write_text(_json.dumps(data, indent=2))
        print('Added {0} ({1}) to allowlist.'.format(name, user_id))
    elif action == 'remove':
        if user_id in allowed:
            removed = allowed.pop(user_id)
            data['updated'] = _dt.now().isoformat(timespec='seconds')
            al_path.write_text(_json.dumps(data, indent=2))
            print('Removed {0} ({1}) from allowlist.'.format(removed.get('name', '?'), user_id))
        else:
            print('{0} was not on the allowlist.'.format(user_id))
    else:
        print('ERROR: unknown allowlist action: ' + action, file=sys.stderr)
        sys.exit(1)

def _find_proposal(card_ts):
    """Look up a proposal_log.jsonl record by the queue card's ts.

    Returns the record dict or None. Walks up from this file to the repo root
    (marker = start.sh) to locate backend/.deer-flow/proposal_log.jsonl.
    """
    cur = Path(__file__).resolve().parent
    log_path = None
    for _ in range(8):
        cand = cur / "deer-flow" / "backend" / ".deer-flow" / "proposal_log.jsonl"
        if cand.exists():
            log_path = cand
            break
        if (cur / "start.sh").exists():
            cand2 = cur / "deer-flow" / "backend" / ".deer-flow" / "proposal_log.jsonl"
            log_path = cand2 if cand2.exists() else None
            break
        cur = cur.parent
    # Fallback: relative to this skill (skills/custom/slack-search → backend)
    if not log_path:
        cand = Path(__file__).resolve().parents[3] / "backend" / ".deer-flow" / "proposal_log.jsonl"
        log_path = cand if cand.exists() else None
    if not log_path or not log_path.exists():
        return None
    match = None
    with open(log_path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except Exception:
                continue
            if rec.get("slack_ts") == card_ts:
                match = rec  # last write wins
    return match


def cmd_send_as_owner(channel, message, thread_ts=None, from_proposal=None):
    """Send a message AS the owner (Brian) using the user token (xoxp-...).

    Used by the approval-queue flow: when Brian approves a drafted Slack reply,
    the message is posted under HIS identity into the original conversation, not
    the bot's. `channel` is the target conversation id (a DM channel id like
    D..., or a channel C.../G...). `thread_ts` optionally replies in a thread.

    When `from_proposal` (a queue card ts) is given, the target channel and draft
    text are read from proposal_log.jsonl — so the caller never has to re-quote a
    multi-line draft on the command line.
    """
    if from_proposal:
        rec = _find_proposal(from_proposal)
        if not rec:
            print(f"ERROR: no proposal found for card ts {from_proposal}.", file=sys.stderr)
            sys.exit(1)
        channel = channel or rec.get("slack_target_channel", "")
        thread_ts = thread_ts or rec.get("slack_target_thread") or None
        if not message or not message.strip():
            message = rec.get("draft_reply", "")
    if not message or not message.strip():
        print("ERROR: empty message — refusing to send.", file=sys.stderr)
        sys.exit(1)
    channel = (channel or '').strip()
    if not channel:
        print("ERROR: --channel (target conversation id) is required.", file=sys.stderr)
        sys.exit(1)
    user = _get_client()  # user token → posts as Brian
    kwargs = {'channel': channel, 'text': message, 'unfurl_links': False, 'unfurl_media': False}
    if thread_ts:
        kwargs['thread_ts'] = thread_ts
    try:
        resp = user.chat_postMessage(**kwargs)
        loc = f"{channel}" + (f" thread {thread_ts}" if thread_ts else "")
        print(f"SENT as owner to {loc} (ts={resp.get('ts', '')})")
    except Exception as e:
        print(f"ERROR: send-as-owner to {channel} failed: {e}", file=sys.stderr)
        sys.exit(1)


def main():
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    if hasattr(sys.stderr, 'reconfigure'):
        sys.stderr.reconfigure(encoding='utf-8', errors='replace')

    if len(sys.argv) < 2:
        print("Usage: python slack_tool.py <command> [args]")
        print("Commands: search, lookup, send, send-as-owner, allowlist")
        sys.exit(1)

    command = sys.argv[1]

    if command == 'search':
        if len(sys.argv) < 3:
            print('Usage: python slack_tool.py search "query" [--days 30] [--count 20]', file=sys.stderr)
            sys.exit(1)
        query = sys.argv[2]
        days = 30
        count = 20
        i = 3
        while i < len(sys.argv):
            if sys.argv[i] == '--days' and i + 1 < len(sys.argv):
                days = int(sys.argv[i + 1])
                i += 2
            elif sys.argv[i] == '--count' and i + 1 < len(sys.argv):
                count = int(sys.argv[i + 1])
                i += 2
            else:
                i += 1
        cmd_search(query, days=days, count=count)

    elif command == 'lookup':
        if len(sys.argv) < 3:
            print('Usage: python slack_tool.py lookup <email>', file=sys.stderr)
            sys.exit(1)
        cmd_lookup(sys.argv[2])

    elif command == 'send':
        if len(sys.argv) < 4:
            print('Usage: python slack_tool.py send <user-id-or-email> "message text"', file=sys.stderr)
            sys.exit(1)
        cmd_send(sys.argv[2], sys.argv[3])

    elif command == 'allowlist':
        if len(sys.argv) < 3 or sys.argv[2] not in ('add', 'remove', 'list'):
            print('Usage: python slack_tool.py allowlist <add|remove|list> [user-id-or-email]', file=sys.stderr)
            sys.exit(1)
        cmd_allowlist(sys.argv[2], sys.argv[3] if len(sys.argv) > 3 else None)

    elif command == 'send-as-owner':
        # python slack_tool.py send-as-owner --from-proposal <card_ts>
        # python slack_tool.py send-as-owner --channel <id> [--thread <ts>] "message text"
        channel = thread = msg = from_proposal = None
        i = 2
        while i < len(sys.argv):
            a = sys.argv[i]
            if a == '--channel' and i + 1 < len(sys.argv):
                channel = sys.argv[i + 1]; i += 2
            elif a == '--thread' and i + 1 < len(sys.argv):
                thread = sys.argv[i + 1]; i += 2
            elif a == '--from-proposal' and i + 1 < len(sys.argv):
                from_proposal = sys.argv[i + 1]; i += 2
            else:
                msg = sys.argv[i]; i += 1
        if not from_proposal and (not channel or not msg):
            print('Usage: python slack_tool.py send-as-owner --from-proposal <card_ts>', file=sys.stderr)
            print('   or: python slack_tool.py send-as-owner --channel <id> [--thread <ts>] "message text"', file=sys.stderr)
            sys.exit(1)
        cmd_send_as_owner(channel, msg, thread, from_proposal)

    else:
        print(f"Unknown command: {command}", file=sys.stderr)
        sys.exit(1)


if __name__ == '__main__':
    main()
