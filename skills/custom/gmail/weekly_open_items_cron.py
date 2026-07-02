#!/usr/bin/env python3
"""Weekly Open Items Review Cron — Monday morning sweep of the past 7 days that
surfaces every thread still AWAITING BRIAN, using live Gmail + Slack only (no
stored memory / top-of-mind, which goes stale).

Runs once weekly on Monday at 08:00 local time (configurable).

Strict surfacing rule:
  Surface a thread ONLY IF the last message is inbound to Brian and Brian has
  NOT replied after it. If Brian sent anything in the same thread/conversation
  AFTER the inbound message -> already handled, drop silently.

Why this exists (root-cause notes baked into the prompt):
  - gmail_tool.py returns a HARD CAP of 10 results per query with no pagination.
    A single broad "past week" search silently truncates to the 10 newest and
    misses everything older (this is how the AIG/Vista-QoE prep email got
    missed). The prompt below paginates around the cap with narrow slices and
    dedupes by threadId.
  - Multi-recipient asks (Brian named alongside Shalom/Pradeep/etc.) must be
    caught by READING the full latest message, never the preview/snippet.

Env vars required:
  - SLACK_BOT_TOKEN, SLACK_OWNER_USER_ID
  - GOOGLE_CLIENT_ID, GOOGLE_CLIENT_SECRET, GOOGLE_REFRESH_TOKEN
  - LANGGRAPH_URL (default: http://localhost:2024)

Optional:
  - WEEKLY_REVIEW_HOUR    (default: 8  — 8 AM)
  - WEEKLY_REVIEW_WEEKDAY (default: 0  — Monday; Python weekday(): Mon=0..Sun=6)
  - WEEKLY_REVIEW_LOOKBACK_DAYS (default: 7)
"""

import json
import logging
import os
import sys
import time
import traceback
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / '_shared'))
from env_loader import load_env
load_env()

logging.basicConfig(
    level=logging.INFO,
    format='[WEEKLY %(asctime)s] %(message)s',
    datefmt='%H:%M:%S',
)
log = logging.getLogger('weekly_open_items')

REVIEW_HOUR    = int(os.environ.get('WEEKLY_REVIEW_HOUR', '8'))      # 8 AM
REVIEW_WEEKDAY = int(os.environ.get('WEEKLY_REVIEW_WEEKDAY', '0'))   # Monday
LOOKBACK_DAYS  = int(os.environ.get('WEEKLY_REVIEW_LOOKBACK_DAYS', '7'))
CHECK_INTERVAL_SECS = 3600  # Check hourly, act only at the target weekday+hour

BRIAN_EMAIL    = 'brian.mauck@tryjeeves.com'
# Canonical resolution incl. the bot-id-as-owner misconfig guard (P3-5).
# Using the bot's ID here would surface DMs the BOT received (Kacper/Chin/
# Pablo/etc.) as if they were addressed to Brian.
from identity import owner_slack_id  # noqa: E402  (_shared already on sys.path)
BRIAN_SLACK_ID = owner_slack_id()


def _state_path() -> Path:
    here = Path(__file__).resolve()
    return here.parents[3] / 'backend' / '.deer-flow' / '_weekly_open_items_state.json'


def load_state() -> dict:
    p = _state_path()
    if p.exists():
        with open(p) as f:
            return json.load(f)
    return {'last_run': None, 'review_count': 0}


def save_state(state: dict) -> None:
    # Callers stamp last_run explicitly — a rejected or failed dispatch must
    # not look like a completed review (GW-F3 class).
    p = _state_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, 'w') as f:
        json.dump(state, f, indent=2)


def _build_prompt(review_number: int) -> str:
    now = datetime.now()
    today = now.strftime('%A, %B %d %Y')
    today_short = now.strftime('%Y-%m-%d')
    since_dt = (now - timedelta(days=LOOKBACK_DAYS)).strftime('%Y-%m-%d')

    return (
        "WEEKLY OPEN ITEMS REVIEW #" + str(review_number) + " -- " + today + "\n\n"
        "You are DeerFlow-Analyst. This is Brian Mauck's automated Monday-morning\n"
        "sweep of the past " + str(LOOKBACK_DAYS) + " days. Surface every thread that is STILL AWAITING\n"
        "BRIAN. Do NOT draft replies. Do NOT use stored memory / top-of-mind / the\n"
        "strategic context file as a source -- those go stale. LIVE Gmail + Slack ONLY.\n\n"
        "Brian's email: " + BRIAN_EMAIL + "\n"
        "Brian's Slack ID: " + BRIAN_SLACK_ID + " (NOT the bot's U09PQTZ5DHC)\n"
        "Today: " + today_short + "    Lookback window: " + since_dt + " to now\n\n"
        "---\n\n"
        "## THE STRICT SURFACING RULE (apply to every candidate)\n"
        "Surface a thread ONLY IF:\n"
        "  (a) the LAST message in the thread/conversation is inbound to Brian, AND\n"
        "  (b) Brian has NOT sent any message in that same thread AFTER it.\n"
        "If Brian replied after the inbound message -> ALREADY HANDLED, drop silently.\n"
        "Being CC'd for visibility is NOT an action item unless there is a genuine ask\n"
        "directed at Brian and no other Jeeves person is the clear owner.\n\n"
        "## CLASSIFY each surviving candidate\n"
        "  AWAITING_REPLY -- direct question/request/decision/deliverable for Brian,\n"
        "                    unanswered. (the only tier that becomes a red item)\n"
        "  WATCH          -- live on a thread Brian is on, but another Jeeves person\n"
        "                    is the owner, or it's blocked on a third party. (watch tier)\n"
        "  DROP           -- FYI-only, already handled, bot/automation, drive-share\n"
        "                    notification, emoji/reaction-only. Never mention these.\n\n"
        "---\n\n"
        "## Step 1: GMAIL -- paginate around the 10-result cap\n"
        "gmail_tool.py returns AT MOST 10 results per query with NO pagination. A single\n"
        "broad weekly search WILL silently truncate and miss real items. So run SEVERAL\n"
        "narrow queries and UNION the results, deduping by threadId:\n\n"
        "  python /mnt/skills/custom/gmail/gmail_tool.py search \"to:" + BRIAN_EMAIL + " after:" + since_dt + " -from:" + BRIAN_EMAIL + "\"\n"
        "  python /mnt/skills/custom/gmail/gmail_tool.py search \"cc:" + BRIAN_EMAIL + " after:" + since_dt + " -from:" + BRIAN_EMAIL + "\"\n"
        "  python /mnt/skills/custom/gmail/gmail_tool.py search \"in:inbox is:unread after:" + since_dt + "\"\n"
        "  # If any single query returns exactly 10 (i.e. likely truncated), re-run it\n"
        "  # split day-by-day: append 'after:YYYY-MM-DD before:YYYY-MM-DD' for each of\n"
        "  # the last " + str(LOOKBACK_DAYS) + " days so no day can hide behind the cap.\n"
        "  # Also sweep key counterparties individually, e.g.:\n"
        "  #   from:goodwinlaw.com, from:vistacreditpartners.com, from:cim-llc.com,\n"
        "  #   from:tryjeeves.com (Alexander/Shalom/Pablo/Isabel/Brandon/Pradeep)\n\n"
        "For EACH unique thread, READ THE FULL LATEST MESSAGE (never the snippet):\n"
        "  python /mnt/skills/custom/gmail/gmail_tool.py read <message_id>\n"
        "This is mandatory for multi-recipient asks where Brian is named alongside\n"
        "others (e.g. an Alex email to Brian+Shalom+Pradeep with a deliverable).\n\n"
        "Thread reply-check (MANDATORY before marking AWAITING_REPLY):\n"
        "  python /mnt/skills/custom/gmail/gmail_tool.py search \"in:sent from:" + BRIAN_EMAIL + " after:" + since_dt + "\"\n"
        "  Match on threadId. If Brian sent in that thread AFTER the inbound -> drop.\n\n"
        "## Step 2: SLACK -- DMs + mentions, then reply-check\n"
        "  python /mnt/skills/custom/slack-search/slack_tool.py search \"to:@brian.mauck\" --days " + str(LOOKBACK_DAYS) + " --count 60\n"
        "  python /mnt/skills/custom/slack-search/slack_tool.py search \"<@" + BRIAN_SLACK_ID + ">\" --days " + str(LOOKBACK_DAYS) + " --count 40\n\n"
        "Drop bot/automation senders (the 'capital markets analyst' / deerflow_analyst\n"
        "bot, 'google drive', etc.) and emoji/reaction-only messages.\n"
        "Reply-check (MANDATORY before AWAITING_REPLY):\n"
        "  python /mnt/skills/custom/slack-search/slack_tool.py search \"from:@brian.mauck\" --days " + str(LOOKBACK_DAYS) + " --count 60\n"
        "  In the SAME DM/channel, if Brian posted AFTER the inbound -> drop. Watch for\n"
        "  acknowledgements that imply he answered (e.g. the other person replies\n"
        "  'Got it'/'Thanks'/'TY!' shortly after) -> handled, drop.\n\n"
        "## Step 3: POST the briefing to Brian's Slack DM\n"
        "Send the WHOLE briefing as ONE message (a prior run lost its body by splitting\n"
        "it -- keep it to a single message). Format:\n\n"
        "  *Weekly Open Items -- " + today + "*\n"
        "  _Past " + str(LOOKBACK_DAYS) + " days, live Slack + email, threads still awaiting you._\n\n"
        "  *Awaiting your reply*\n"
        "  - [Email/Slack] *<person>* -- <one-line ask> _(age: Nd)_\n"
        "  ...\n\n"
        "  *Watch* (not your action, but live)\n"
        "  - *<person/topic>* -- <one line> _(owner: <name>)_\n"
        "  ...\n\n"
        "If a tier is empty, write 'None'. Bullets only, no paragraphs, under 350 words.\n"
        "Use slack_tool.py send ONLY if posting to a channel other than this DM;\n"
        "the DM reply is returned directly.\n\n"
        "---\n"
        "ACCURACY RULES (mandatory):\n"
        "- Every item must be a thread you actually opened and read in full this run.\n"
        "- No memory/top-of-mind sourcing. If you can't verify from live Gmail/Slack, omit it.\n"
        "- When ownership is genuinely ambiguous, put it under Watch with a [Needs\n"
        "  Confirmation] note rather than Awaiting.\n"
        "- Do NOT draft replies. Brian only wants to know what is open.\n"
    )


# ------------------------------------------------------------------ #
# Relationship-health digest (PRD §7 H4)                               #
# ------------------------------------------------------------------ #
# ONA staleness ("gone quiet") + a small dialectic pass over the most
# active external counterparties. Posts a SECOND short Monday DM — and
# only when there's signal (silent when nothing to report).

DIGEST_TOP_N = int(os.environ.get('WEEKLY_DIGEST_TOP_PEERS', '5'))
_INTERNAL_DOMAIN = 'tryjeeves.com'

_DIGEST_FLAG_PROMPT = (
    "Looking at the last few weeks of interactions with this person: has "
    "anything been dropped or left hanging, or has their tone or engagement "
    "noticeably shifted? Answer in at most 40 words grounded in concrete "
    "interactions. If nothing is notable, answer exactly: NONE"
)


def _ona_edges() -> list:
    """Brian's edges from ona_graph.json. [] fail-open."""
    try:
        path = (Path(__file__).resolve().parents[3] / 'backend' / '.deer-flow'
                / 'dossiers' / 'ona_graph.json')
        override = os.environ.get('DOSSIER_PATH', '')
        if override:
            path = Path(override) / 'ona_graph.json'
        if not path.exists():
            return []
        return json.loads(path.read_text()).get('edges', [])
    except Exception as e:
        log.warning("could not load ona_graph.json: %s", e)
        return []


def _owner_counterpart(edge: dict, owner_email: str):
    s = (edge.get('source') or '').lower()
    t = (edge.get('target') or '').lower()
    if s == owner_email:
        return t
    if t == owner_email:
        return s
    return None


def _is_external_person(email: str) -> bool:
    e = (email or '').lower()
    if '@' not in e or e.endswith('@' + _INTERNAL_DOMAIN):
        return False
    bad = ('calendar.google.com', 'noreply', 'no-reply', 'donotreply',
           'notifications@', 'mailer-daemon')
    return not any(b in e for b in bad)


def gone_quiet_peers(edges: list, owner_email: str, min_weight: int = 20, cap: int = 8) -> list:
    """External counterparties with real history (weight >= min_weight) and
    ZERO interactions in the last 90 days (weight_90d == 0, per H-BUG-2)."""
    out = []
    for e in edges:
        other = _owner_counterpart(e, owner_email)
        if not other or not _is_external_person(other):
            continue
        if int(e.get('weight') or 0) >= min_weight and int(e.get('weight_90d') or 0) == 0:
            out.append({
                'email': other,
                'weight': int(e.get('weight') or 0),
                'last_interaction': (e.get('last_interaction') or '')[:10],
            })
    out.sort(key=lambda x: -x['weight'])
    return out[:cap]


def top_active_peers(edges: list, owner_email: str, n: int = DIGEST_TOP_N) -> list:
    """Most-active external counterparties over the last 90 days."""
    out = []
    for e in edges:
        other = _owner_counterpart(e, owner_email)
        if not other or not _is_external_person(other):
            continue
        w90 = int(e.get('weight_90d') or 0)
        if w90 > 0:
            out.append({'email': other, 'weight_90d': w90})
    out.sort(key=lambda x: -x['weight_90d'])
    return out[:n]


def build_relationship_digest(edges: list, ask_fn, owner_email: str, top_n: int = DIGEST_TOP_N) -> str:
    """Digest text, or '' when there is nothing worth Brian's attention.
    ask_fn(email) -> str|None is the dialectic (injected for testability)."""
    quiet = gone_quiet_peers(edges, owner_email)
    flags = []
    for peer in top_active_peers(edges, owner_email, top_n):
        try:
            resp = ask_fn(peer['email'])
        except Exception:
            resp = None
        if resp and resp.strip() and resp.strip().upper() != 'NONE':
            flags.append((peer['email'], resp.strip()[:300]))
    if not quiet and not flags:
        return ''
    lines = ["*Relationship health — weekly*"]
    if flags:
        lines.append("\n*Flags on active counterparties* (from the relationship model)")
        for email, flag in flags:
            lines.append(f"- *{email}* — {flag}")
    if quiet:
        lines.append("\n*Gone quiet* (real history, nothing in 90+ days)")
        for q in quiet:
            last = f", last touch {q['last_interaction']}" if q['last_interaction'] else ''
            lines.append(f"- *{q['email']}* — {q['weight']} lifetime interactions{last}")
    return "\n".join(lines)


def run_relationship_digest() -> None:
    """Best-effort H4 digest. Never raises — must not affect the main review."""
    try:
        edges = _ona_edges()
        if not edges:
            return
        import importlib.util
        pct_path = Path(__file__).resolve().parent.parent / 'honcho-peers' / 'peer_card_tool.py'
        spec = importlib.util.spec_from_file_location('peer_card_tool_weekly', pct_path)
        pct = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(pct)
        digest = build_relationship_digest(
            edges, lambda email: pct.peer_ask(email, _DIGEST_FLAG_PROMPT), BRIAN_EMAIL)
        if not digest:
            log.info("Relationship digest: nothing to report — staying silent.")
            return
        from notify import dm_owner
        if dm_owner(digest):
            log.info("Relationship digest posted.")
        else:
            log.warning("Relationship digest DM failed.")
    except Exception as e:
        log.warning("Relationship digest skipped: %s", e)


def run_review() -> None:
    log.info("Starting weekly open-items review...")
    state = load_state()
    review_number = state.get('review_count', 0) + 1
    prompt = _build_prompt(review_number)

    notification = (
        "Weekly Open Items review starting -- sweeping the past "
        + str(LOOKBACK_DAYS) + " days of Slack + email for threads still awaiting you. "
        "Briefing to follow shortly."
    )

    try:
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent / '_shared'))
        # Plain dispatch, NOT enqueue_or_dispatch: run_loop retries every tick
        # while last_run is unstamped, and a persisted-queue copy can't stamp
        # our state — it would re-run a review the queue already dispatched.
        from autonomous_dispatch import dispatch

        dispatched = dispatch(
            prompt,
            notification=notification,
            category="Weekly Open Items",
            source_id="weekly-" + datetime.now().strftime('%Y%m%d'),
            source_metadata={"review_number": review_number},
        )
        if dispatched:
            state['review_count'] = review_number
            log.info("Weekly review dispatched successfully.")
        else:
            log.warning("Weekly review rejected -- agent at capacity. Will retry next cycle.")
            return
    except Exception as e:
        log.error("Weekly review dispatch failed: %s", e)
        traceback.print_exc()
        return

    state['last_run'] = datetime.now().isoformat()
    save_state(state)

    # H4: relationship-health digest rides the same weekly cadence.
    # Best-effort and posts only when there's signal.
    run_relationship_digest()


def run_loop() -> None:
    from cron_schedule import weekly_run_due

    wd = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun'][REVIEW_WEEKDAY]
    log.info("Weekly Open Items cron started. Triggers %s at %02d:00." % (wd, REVIEW_HOUR))
    while True:
        now = datetime.now()
        # Due whenever the last run predates the most recent scheduled time —
        # a Monday missed to downtime catches up on the next tick instead of
        # silently skipping the whole week (GW-F10).
        if weekly_run_due(load_state().get('last_run'), now, REVIEW_WEEKDAY, REVIEW_HOUR):
            try:
                run_review()
            except Exception as e:
                log.error("Weekly loop error: %s", e)
                traceback.print_exc()
        time.sleep(CHECK_INTERVAL_SECS)


if __name__ == '__main__':
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    if hasattr(sys.stderr, 'reconfigure'):
        sys.stderr.reconfigure(encoding='utf-8', errors='replace')

    import argparse
    parser = argparse.ArgumentParser(description='Weekly Open Items Review Cron')
    parser.add_argument('mode', nargs='?', choices=['once', 'show-prompt'],
                        help="'once' runs one review now; 'show-prompt' prints the prompt and exits")
    args = parser.parse_args()

    if args.mode == 'show-prompt':
        print(_build_prompt(load_state().get('review_count', 0) + 1))
    elif args.mode == 'once':
        run_review()
    else:
        run_loop()
