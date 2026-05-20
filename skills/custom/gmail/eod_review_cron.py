#!/usr/bin/env python3
"""End of Day Review Cron — proactively surfaces unhandled items directed at Brian
and drafts responses so he can review and send with one click.

Runs once daily at 5:00 PM local time (configurable).

What it does:
  1. Fetches Gmail + Slack messages from the past 24h addressed to Brian
  2. Uses LLM classification on each item to determine if it is a genuine
     action item (reads full content -- NOT a 'last recipient' heuristic)
  3. Reads Gemini meeting notes and classifies action items from today's meetings
  4. Drafts ready-to-send responses for all ACTIONABLE items
  5. Posts a structured HIGH/MEDIUM/LOW EOD briefing to Brian's Slack DM
  6. Saves full summary to Google Drive

The goal: Brian should be able to clear his day in < 15 minutes by reviewing
this brief and approving/sending the pre-drafted responses.

Env vars required:
  - SLACK_BOT_TOKEN, SLACK_OWNER_USER_ID
  - GOOGLE_CLIENT_ID, GOOGLE_CLIENT_SECRET, GOOGLE_REFRESH_TOKEN
  - LANGGRAPH_URL (default: http://localhost:2024)

Optional:
  - EOD_REVIEW_HOUR (default: 17 — 5 PM)
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
    format='[EOD %(asctime)s] %(message)s',
    datefmt='%H:%M:%S',
)
log = logging.getLogger('eod_review')

EOD_HOUR = int(os.environ.get('EOD_REVIEW_HOUR', '17'))   # 5 PM
CHECK_INTERVAL_SECS = 3600  # Check every hour, skip if not EOD time

BRIAN_EMAIL    = 'brian.mauck@tryjeeves.com'
BRIAN_SLACK_ID = 'U09PQTZ5DHC'

# ------------------------------------------------------------------ #
# State                                                                #
# ------------------------------------------------------------------ #

def _state_path() -> Path:
    here = Path(__file__).resolve()
    return here.parents[3] / 'backend' / '.deer-flow' / '_eod_review_state.json'


def load_state() -> dict:
    p = _state_path()
    if p.exists():
        with open(p) as f:
            return json.load(f)
    return {'last_eod': None, 'review_count': 0}


def save_state(state: dict) -> None:
    p = _state_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    state['last_eod'] = datetime.now().isoformat()
    with open(p, 'w') as f:
        json.dump(state, f, indent=2)


def _already_ran_today(state: dict) -> bool:
    last = state.get('last_eod')
    if not last:
        return False
    last_dt = datetime.fromisoformat(last)
    return last_dt.date() == datetime.now().date()


# ------------------------------------------------------------------ #
# Prompt builder                                                       #
# ------------------------------------------------------------------ #

def _build_eod_prompt(review_number: int) -> str:
    today = datetime.now().strftime('%A, %B %d %Y')
    today_short = datetime.now().strftime('%Y-%m-%d')
    lookback_dt = (datetime.now() - timedelta(hours=24)).strftime('%Y-%m-%d')

    return (
        "END OF DAY REVIEW #" + str(review_number) + " -- " + today + "\n\n"
        "You are DeerFlow-Analyst. This is Brian Mauck's automated end-of-day review.\n"
        "Surface every genuine action item directed at Brian today that has not been\n"
        "handled, and draft ready-to-send responses so he can clear the day in under\n"
        "15 minutes.\n\n"
        "Brian's email: " + BRIAN_EMAIL + "\n"
        "Brian's Slack ID: " + BRIAN_SLACK_ID + "\n"
        "Today: " + today_short + "\n"
        "Lookback: " + lookback_dt + " to now\n\n"
        "---\n\n"
        "## CLASSIFIER STANDARD -- apply to every single item in every step below\n\n"
        "Before flagging ANYTHING as an action item, read the full content and answer:\n\n"
        "  1. Does this message contain a direct question addressed to Brian?\n"
        "  2. Does it contain an explicit request for Brian to do something?\n"
        "  3. Does it reference a decision or approval Brian needs to give?\n"
        "  4. Does it reference a deliverable Brian is responsible for?\n"
        "  5. Is there a deadline or time pressure implied?\n\n"
        "Label each item:\n"
        "  ACTIONABLE      -- yes to any of 1-5, AND Brian has not yet responded\n"
        "  FYI_ONLY        -- informational only, no response required\n"
        "  ALREADY_HANDLED -- Brian already replied or acted on this\n"
        "  UNCLEAR         -- genuinely ambiguous; list separately for Brian to decide\n\n"
        "Only ACTIONABLE items get drafted and included in the briefing.\n"
        "FYI_ONLY and ALREADY_HANDLED are silently dropped -- do not mention them.\n\n"
        "CRITICAL: do NOT use last-recipient-in-thread as a proxy for actionability.\n"
        "An email where Brian is last recipient may still be FYI_ONLY.\n"
        "An email where Brian is CC'd may be ACTIONABLE if it contains a genuine ask.\n"
        "You MUST read the full body before classifying -- snippets are not enough.\n\n"
        "---\n\n"
        "## Step 1: Gmail -- fetch and classify\n\n"
        "Load the gmail skill. Run this search:\n"
        "  to:brian.mauck@tryjeeves.com after:" + lookback_dt + " -from:brian.mauck@tryjeeves.com\n\n"
        "For EACH message returned (expect 20-30):\n\n"
        "  1a. Read the FULL email body -- not the snippet:\n"
        "        python /mnt/skills/custom/gmail/gmail_tool.py read <message_id>\n\n"
        "  1b. Apply the CLASSIFIER STANDARD above.\n"
        "      Ask: what is this person actually asking Brian to do, specifically?\n\n"
        "  1c. Thread check: look for a reply from Brian AFTER the inbound message.\n"
        "      If Brian already replied -> ALREADY_HANDLED, drop it.\n\n"
        "  1d. For ACTIONABLE items only:\n"
        "      - Quote the specific ask verbatim (paraphrase only if very long)\n"
        "      - Urgency:\n"
        "          HIGH   = external counterparty (lender, investor, auditor, legal)\n"
        "          MEDIUM = internal Jeeves colleague\n"
        "          LOW    = soft ask, no deadline, low stakes\n"
        "      - Draft a reply in Gmail Drafts:\n"
        "            python /mnt/skills/custom/gmail/gmail_tool.py draft <id> '<reply>'\n"
        "      - If the reply needs data (numbers, dates, portfolio figures) pull it first.\n"
        "      - If too complex to complete fully, create a placeholder draft and note\n"
        "        what Brian needs to add: [CONFIRM AMOUNT], [CHECK WITH ALEX], etc.\n"
        "      - Voice: short, direct, no over-explaining. Match Brian's style.\n\n"
        "## Step 2: Slack -- fetch and classify\n\n"
        "Load the slack-search skill. Search for:\n"
        "  - DMs to Brian (" + BRIAN_SLACK_ID + ") in the past 24 hours\n"
        "  - @mentions of Brian in any channel\n\n"
        "For EACH message, apply the CLASSIFIER STANDARD.\n"
        "Read the full message AND thread context -- not just the notification text.\n\n"
        "For ACTIONABLE Slack items:\n"
        "  - Note: sender, channel, the specific ask\n"
        "  - Prepare copy-pasteable reply text Brian can send himself\n"
        "  - Do NOT send on Brian's behalf\n\n"
        "Common FYI_ONLY false positives to drop:\n"
        "  - @mentions that loop Brian in for awareness with no explicit ask\n"
        "  - Messages Brian has already replied to in the thread\n"
        "  - Automated bot or workflow notification messages\n"
        "  - Threads already resolved by another team member\n\n"
        "## Step 3: Calendar -- today's meetings\n\n"
        "Load google-calendar. Pull today's calendar events.\n"
        "Note which meetings occurred today. Do NOT try to extract action items from\n"
        "calendar metadata -- use Gemini notes (Step 3b) for all meeting action items.\n\n"
        "## Step 3b: Gemini meeting notes -- classify action items\n\n"
        "Gemini for Google Meet auto-generates meeting summaries and action item lists.\n"
        "These are the highest-signal source of commitments made in today's meetings.\n\n"
        "Search Gmail:\n"
        "  from:meet-recordings-noreply@google.com after:" + today_short + " subject:Notes from\n"
        "Also try: subject:'Notes from your meeting' newer_than:1d\n\n"
        "For EACH set of notes found:\n"
        "  1. Read the full notes document (gmail_tool.py read <msg_id> or fetch Drive link)\n"
        "  2. Find the action items / next steps section\n"
        "  3. Apply CLASSIFIER STANDARD to each action item:\n"
        "     - Assigned to Brian by name, 'you', or implicitly as the Jeeves representative?\n"
        "     - Already completed? Look for evidence in Gmail/Slack before flagging.\n"
        "  4. ACTIONABLE meeting items: include meeting name + counterparty + urgency\n"
        "  5. Any commitment made to an external counterparty is automatically HIGH\n\n"
        "## Step 4: Lender pipeline -- proactive check\n\n"
        "Check open workstreams for items at risk of going cold:\n"
        "  - BBVA: 9 open diligence items -- any new inbound? Anything stalled?\n"
        "  - CIM 5th Amendment: new correspondence or deadlines?\n"
        "  - NB: any follow-ups overdue?\n"
        "  - CBIZ audit: new requests from Vincent McAndress or finops team?\n\n"
        "Flag any where Brian's last outbound was >3 business days ago with no reply.\n\n"
        "## Step 5: Post EOD briefing to Slack\n\n"
        "Post to Brian's Slack DM using this format:\n\n"
        "  EOD Review #" + str(review_number) + " -- " + today + "\n\n"
        "  HIGH (action today):\n"
        "  - [Email/Slack/Meeting] [person] -- [specific ask] -> [draft created / next step]\n\n"
        "  MEDIUM (action tomorrow AM):\n"
        "  - ...\n\n"
        "  LOW:\n"
        "  - ...\n\n"
        "  UNCLEAR (Brian to decide):\n"
        "  - [item] -- [why it's ambiguous]\n\n"
        "  Drafted: [N] Gmail drafts in Drafts folder | [N] Slack replies in Drive doc\n"
        "  Lender pipeline: [any items at risk or gone quiet]\n\n"
        "Bullets only. No paragraphs. Under 400 words total.\n\n"
        "## Step 6: Save to Google Drive\n\n"
        "Save the full summary (including Slack reply texts) as a Google Doc.\n"
        "File name: EOD Review - " + today_short + ".md\n"
        "Upload and include the Drive link in the Slack message.\n\n"
        "---\n\n"
        "ACCURACY RULES -- mandatory:\n"
        "- Only flag items you actually found and fully read. Never infer from snippets alone.\n"
        "- If Brian has already replied: ALREADY_HANDLED, drop it silently.\n"
        "- Never draft text that commits Brian to deal terms, numbers, or legal language.\n"
        "  Use placeholders: [CONFIRM AMOUNT], [CHECK WITH ALEX], [VERIFY DATE].\n"
        "- Uncertain whether action is needed -> UNCLEAR, not ACTIONABLE.\n"
    )


# ------------------------------------------------------------------ #
# Dispatch                                                             #
# ------------------------------------------------------------------ #

def run_eod_review() -> None:
    log.info("Starting EOD review...")
    state = load_state()

    review_number = state.get('review_count', 0) + 1
    prompt = _build_eod_prompt(review_number)

    today = datetime.now().strftime('%B %d')
    notification = (
        f"📋 *EOD Review #{review_number} starting* — "
        f"scanning today's ({today}) unhandled items. I'll post a full briefing shortly."
    )

    try:
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent / '_shared'))
        from autonomous_dispatch import dispatch

        dispatched = dispatch(
            prompt,
            notification=notification,
            category="EOD Review",
            source_id=f"eod-{datetime.now().strftime('%Y%m%d')}",
            source_metadata={"review_number": review_number},
        )
        if dispatched:
            state['review_count'] = review_number
            log.info("EOD review dispatched successfully.")
        else:
            log.warning("EOD review rejected — agent at capacity. Will retry next cycle.")
            # Don't update review_count — allow retry
            return
    except Exception as e:
        log.error("EOD review dispatch failed: %s", e)
        traceback.print_exc()
        return

    save_state(state)


# ------------------------------------------------------------------ #
# Loop                                                                 #
# ------------------------------------------------------------------ #

def run_loop() -> None:
    log.info(f"EOD Review cron started. Will trigger at {EOD_HOUR:02d}:00 daily.")

    while True:
        now = datetime.now()

        if now.hour == EOD_HOUR:
            state = load_state()
            if not _already_ran_today(state):
                try:
                    run_eod_review()
                except Exception as e:
                    log.error("EOD loop error: %s", e)
                    traceback.print_exc()
            else:
                log.info("Already ran EOD review today, skipping.")
        else:
            log.debug(f"Not EOD time yet (current hour: {now.hour}, target: {EOD_HOUR})")

        time.sleep(CHECK_INTERVAL_SECS)


if __name__ == '__main__':
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    if hasattr(sys.stderr, 'reconfigure'):
        sys.stderr.reconfigure(encoding='utf-8', errors='replace')

    import argparse
    parser = argparse.ArgumentParser(description='EOD Review Cron')
    parser.add_argument('mode', nargs='?', choices=['once'],
                        help='Run one review immediately instead of looping')
    args = parser.parse_args()

    if args.mode == 'once':
        run_eod_review()
    else:
        run_loop()
