#!/usr/bin/env python3
"""Linear Mention Monitor Cron -- watches for Linear @mentions of Brian via
Gmail notification emails and autonomously handles them (reads the ticket,
takes the appropriate action, posts a reply comment on Linear).

Behavior:
  - Every 5 minutes: search Gmail for new Linear notification emails whose
    subject indicates a genuine @mention ("X mentioned you in a comment on
    [TICKET-ID] ..."), as opposed to routine "X commented on [TICKET-ID]"
    activity on a ticket Brian merely watches (not a tag -- ignored).
  - For each new mention, dispatch to the DeerFlow agent with full ticket
    context to read, decide, and act (post a Linear comment / update status)
    with no per-item approval from Brian, per his explicit instruction
    (2026-07-08): "I get tagged on tickets regularly... when I'm tagged I
    want you to handle [it]... automatic."
  - State is persisted so we never double-handle the same notification email.

Env vars required:
  - GOOGLE_CLIENT_ID, GOOGLE_CLIENT_SECRET, GOOGLE_REFRESH_TOKEN (Gmail)
  - LINEAR_API_KEY
  - SLACK_BOT_TOKEN, SLACK_OWNER_USER_ID
"""

import json
import logging
import os
import re
import sys
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / '_shared'))
from env_loader import load_env
load_env()

logging.basicConfig(
    level=logging.INFO,
    format='[LinearMentionMonitor %(asctime)s] %(message)s',
    datefmt='%H:%M:%S',
)
log = logging.getLogger('linear_mention_monitor')

POLL_INTERVAL = int(os.environ.get('LINEAR_MENTION_MONITOR_INTERVAL', '300'))  # 5 min

MENTION_SUBJECT_RE = re.compile(r'mentioned you in a comment on \[([A-Z]+-\d+)\]')


def _state_path() -> str:
    here = Path(__file__).resolve()
    backend = here.parents[3] / 'backend'
    return str(backend / '.deer-flow' / '_linear_mention_state.json')


def _load_state() -> dict:
    path = _state_path()
    if os.path.exists(path):
        try:
            with open(path) as f:
                return json.load(f)
        except Exception:
            pass
    return {'handled_msg_ids': []}


def _save_state(state: dict) -> None:
    path = _state_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    if len(state.get('handled_msg_ids', [])) > 500:
        state['handled_msg_ids'] = state['handled_msg_ids'][-500:]
    with open(path, 'w') as f:
        json.dump(state, f, indent=2)


def _search_gmail_mentions() -> list:
    """Returns list of {id, subject, ticket_id} for new mention emails.

    gmail_tool.py's cmd_search() only prints (CLI-shaped, no return value),
    so this calls the Gmail API the same way cmd_search does internally but
    returns structured data instead.
    """
    gmail_dir = str(Path(__file__).resolve().parent.parent / 'gmail')
    if gmail_dir not in sys.path:
        sys.path.insert(0, gmail_dir)
    import gmail_tool

    service = gmail_tool._get_service()
    results = service.users().messages().list(
        userId='me', q='from:linear.app newer_than:2d', maxResults=20
    ).execute()
    messages = results.get('messages', [])

    out = []
    for msg_ref in messages:
        msg = service.users().messages().get(
            userId='me', id=msg_ref['id'], format='metadata',
            metadataHeaders=['Subject']
        ).execute()
        headers = msg.get('payload', {}).get('headers', [])
        subject = gmail_tool._header(headers, 'Subject') or ''
        m = MENTION_SUBJECT_RE.search(subject)
        if not m:
            continue  # routine "commented on" activity, not a tag -- ignore
        out.append({'id': msg_ref['id'], 'subject': subject, 'ticket_id': m.group(1)})
    return out


def _dispatch_ticket(item: dict) -> bool:
    ticket_id = item['ticket_id']
    log.info("Dispatching handling for %s (subject: %s)", ticket_id, item['subject'][:80])

    try:
        from dispatch_queue import enqueue_or_dispatch
        enqueue_or_dispatch(
            (
                f"Brian Mauck was just @mentioned in a Linear comment on ticket {ticket_id} "
                f"(Gmail notification subject: \"{item['subject']}\").\n\n"
                "Handle this autonomously as Brian's Capital Markets agent, per his standing "
                "instruction (2026-07-08): when he's tagged on a Linear ticket, read it and act "
                "-- do not wait for his approval on routine items.\n\n"
                "Steps:\n"
                f"1. Load the linear skill. Pull the full ticket + comment history: "
                f"python /mnt/skills/custom/linear/linear_tool.py get {ticket_id}\n"
                "2. Read the tag/ask in full -- do not act on a truncated comment. If more than "
                "10 comments exist, query the Linear API directly for the full thread (see the "
                "linear skill's cmd_get for the GraphQL pattern).\n"
                "3. Decide the right action:\n"
                "   - If you can verify/answer accurately with tools you have (Redshift, Drive, "
                "Gmail, Slack, other Linear tickets) -- do the verification, then post an accurate "
                "reply comment: python /mnt/skills/custom/linear/linear_tool.py comment "
                f"{ticket_id} \"...\"\n"
                "   - If the ask requires something only Brian can do (e.g. a production credential "
                "only he holds, a business/legal judgment call, sign-off on something outside your "
                "authority) -- post an honest status comment saying so (never fabricate a result), "
                "and separately DM Brian on Slack flagging that it needs his personal action.\n"
                "   - If the ticket is clearly misdirected at Brian (wrong owner), say so in the "
                "comment and suggest/tag the right owner if identifiable from ticket context.\n"
                "4. NEVER paste secrets, passwords, or values that look like AWS Secrets Manager "
                "paths (e.g. strings starting with /production/...) into a comment or any tool "
                "argument verbatim -- rephrase around them. This has previously triggered a path-"
                "safety rejection; it is also a real security concern.\n"
                "5. After acting, send Brian a one-line Slack DM noting what was posted and a link "
                "to the ticket, for visibility -- this is an FYI, not a request for approval.\n"
                "6. If this reveals a reusable pattern (e.g. how a specific class of DEV ticket "
                "should be handled), log it via skill_manage.py log for the linear skill."
            ),
            notification=f":ticket: *Linear tag on {ticket_id}* -- handling it now.",
            category="linear-mention",
            source_id=f"linear-mention-{item['id']}",
            source_metadata={
                'ticket_id': ticket_id,
                'gmail_msg_id': item['id'],
                'subject': item['subject'],
            },
        )
        return True
    except Exception as e:
        log.error("Failed to dispatch for %s: %s", ticket_id, e)
        return False


def run_once() -> None:
    state = _load_state()
    handled = set(state.get('handled_msg_ids', []))

    try:
        mentions = _search_gmail_mentions()
    except Exception as e:
        log.error("Gmail search failed: %s", e)
        return

    new_items = [m for m in mentions if m['id'] not in handled]
    if not new_items:
        log.info("No new Linear mentions.")
        return

    for item in new_items:
        ok = _dispatch_ticket(item)
        if ok:
            handled.add(item['id'])

    state['handled_msg_ids'] = list(handled)
    _save_state(state)


def run_loop() -> None:
    log.info("Linear Mention Monitor starting (poll=%ds)", POLL_INTERVAL)
    while True:
        try:
            run_once()
        except Exception as e:
            log.error("Loop error: %s", e)
        time.sleep(POLL_INTERVAL)


if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser(description='Linear Mention Monitor Cron')
    parser.add_argument('mode', nargs='?', choices=['once'],
                        help='Run one check immediately instead of looping')
    args = parser.parse_args()

    if args.mode == 'once':
        run_once()
    else:
        run_loop()
