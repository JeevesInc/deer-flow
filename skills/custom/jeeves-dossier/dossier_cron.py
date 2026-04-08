#!/usr/bin/env python3
"""Dossier auto-briefing cron: checks upcoming calendar events and posts
pre-meeting dossier briefings to Slack ~15 minutes before each meeting.

Runs as a background process alongside LangGraph + Gateway.

Env vars required:
  - GOOGLE_CLIENT_ID, GOOGLE_CLIENT_SECRET, GOOGLE_REFRESH_TOKEN
  - SLACK_BOT_TOKEN (xoxb-... for posting DMs)
  - SLACK_USER_TOKEN (xoxp-... for searching messages)
  - SLACK_OWNER_USER_ID (the user's Slack ID for DM delivery)
  - ANTHROPIC_API_KEY (for synthesis calls)

Optional:
  - DOSSIER_CRON_INTERVAL (seconds between checks, default: 600)
  - DOSSIER_PATH (override dossier storage dir)
"""

import json
import logging
import os
import sys
import time
import traceback
from datetime import datetime, timedelta, timezone

# Load .env before anything else (belt-and-suspenders: uv run loads it too,
# but subprocess invocations don't inherit the env automatically)
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', '_shared'))
from env_loader import load_env
load_env()
from pathlib import Path

logging.basicConfig(
    level=logging.INFO,
    format='[DossierCron %(asctime)s] %(message)s',
    datefmt='%H:%M:%S',
)
log = logging.getLogger('dossier_cron')

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

CHECK_INTERVAL = int(os.environ.get('DOSSIER_CRON_INTERVAL', '600'))  # 10 min
LOOKAHEAD_MIN = 15  # Look for events starting in 15-25 min
LOOKAHEAD_MAX = 25
MY_EMAIL = os.environ.get('GOOGLE_CALENDAR_EMAIL', 'brian.mauck@tryjeeves.com')
SYNTHESIS_MODEL = 'claude-sonnet-4-6'


def _state_path():
    base = os.environ.get('DOSSIER_PATH', '')
    if not base:
        backend = Path(__file__).resolve().parent.parent.parent.parent / 'backend' / '.deer-flow' / 'dossiers'
        base = str(backend)
    os.makedirs(base, exist_ok=True)
    return os.path.join(base, '_cron_state.json')


def _dossier_dir():
    base = os.environ.get('DOSSIER_PATH', '')
    if not base:
        backend = Path(__file__).resolve().parent.parent.parent.parent / 'backend' / '.deer-flow' / 'dossiers'
        base = str(backend)
    os.makedirs(base, exist_ok=True)
    return base


def _dossier_path(email):
    safe = email.replace('@', '_at_').replace('.', '_')
    return os.path.join(_dossier_dir(), f'{safe}.json')


# ---------------------------------------------------------------------------
# State management
# ---------------------------------------------------------------------------

def load_state():
    path = _state_path()
    if os.path.exists(path):
        with open(path, 'r') as f:
            return json.load(f)
    return {"prepped_events": {}, "last_check": None}


def save_state(state):
    state['last_check'] = datetime.now().isoformat()
    # Clean entries older than 24 hours
    cutoff = (datetime.now() - timedelta(hours=24)).isoformat()
    state['prepped_events'] = {
        k: v for k, v in state.get('prepped_events', {}).items()
        if v > cutoff
    }
    with open(_state_path(), 'w') as f:
        json.dump(state, f, indent=2)


# ---------------------------------------------------------------------------
# Google Calendar
# ---------------------------------------------------------------------------

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', '_shared'))
from google_auth import get_credentials as _get_google_creds_required


def _get_google_creds():
    return _get_google_creds_required(required=False)


def get_upcoming_events():
    """Find events starting in LOOKAHEAD_MIN to LOOKAHEAD_MAX minutes."""
    creds = _get_google_creds()
    if not creds:
        log.warning("Google creds not configured, skipping.")
        return []
    from googleapiclient.discovery import build
    service = build('calendar', 'v3', credentials=creds)

    now = datetime.now(timezone.utc)
    time_min = (now + timedelta(minutes=LOOKAHEAD_MIN)).isoformat()
    time_max = (now + timedelta(minutes=LOOKAHEAD_MAX)).isoformat()

    result = service.events().list(
        calendarId='primary',
        timeMin=time_min,
        timeMax=time_max,
        singleEvents=True,
        orderBy='startTime',
        maxResults=10,
    ).execute()

    return result.get('items', [])


# ---------------------------------------------------------------------------
# Data gathering (reuses dossier_tool logic inline)
# ---------------------------------------------------------------------------

def gather_for_contact(email, days=14):
    """Lightweight gather — runs dossier_tool.py as subprocess."""
    import subprocess
    tool_path = os.path.join(os.path.dirname(__file__), 'dossier_tool.py')
    try:
        result = subprocess.run(
            [sys.executable, tool_path, 'gather', email, '--days', str(days)],
            capture_output=True, text=True, timeout=120,
            env={**os.environ},
        )
        output = result.stdout
        # Extract JSON from output (skip the summary lines)
        json_start = output.find('{')
        if json_start >= 0:
            return json.loads(output[json_start:])
    except Exception as e:
        log.error(f"Gather failed for {email}: {e}")
    return None


def read_existing_dossier(email):
    """Read existing dossier JSON if it exists."""
    path = _dossier_path(email)
    if os.path.exists(path):
        with open(path, 'r', encoding='utf-8') as f:
            return json.load(f)
    return None


def save_dossier(email, data):
    """Save dossier JSON."""
    data['email'] = email
    data['last_updated'] = datetime.now().isoformat()
    path = _dossier_path(email)
    with open(path, 'w', encoding='utf-8') as f:
        json.dump(data, f, indent=2, default=str)
    log.info(f"Saved dossier for {email}")


# ---------------------------------------------------------------------------
# Synthesis via Anthropic API
# ---------------------------------------------------------------------------

SYNTHESIS_PROMPT = """You are a relationship analyst creating a contact dossier briefing for Brian Mauck (brian.mauck@tryjeeves.com) at Jeeves Financial Technology. All dossiers are from Brian's perspective — "you" = Brian, the contact = the other person.

Given the raw interaction data and any existing dossier, produce an UPDATED dossier JSON.

Rules:
- NEVER fabricate interactions — only cite what's in the gathered data
- Preserve existing coaching notes — append new ones
- Keep recent_interactions to last 10 entries
- If gathered data is empty for a source, skip it
- MERGE with existing dossier — don't replace
- health_score default: 6 if insufficient data
- Be conservative with scores

Output ONLY valid JSON matching this schema:
{
  "email": "...",
  "name": "...",
  "last_updated": "...",
  "relationship": {"health_score": 1-10, "trend": "improving|stable|cooling", "summary": "..."},
  "communication_style": {"observations": ["..."], "formality_trend": "..."},
  "recent_interactions": [{"date": "...", "type": "...", "source": "...", "summary": "...", "sentiment": "...", "key_topics": ["..."], "action_items": ["..."]}],
  "coaching_notes": [{"date": "...", "note": "...", "suggestion": "...", "category": "..."}],
  "open_threads": [{"topic": "...", "last_mentioned": "...", "status": "...", "context": "..."}],
  "meeting_frequency": {"meetings_30d": N, "cadence": "...", "last_meeting": "..."}
}"""


def synthesize_dossier(email, gathered_data, existing_dossier):
    """Call Anthropic API to synthesize/update a dossier."""
    api_key = os.environ.get('ANTHROPIC_API_KEY')
    if not api_key:
        log.error("ANTHROPIC_API_KEY not set, cannot synthesize.")
        return None

    try:
        import anthropic
    except ImportError:
        import subprocess
        subprocess.check_call([sys.executable, '-m', 'pip', 'install', '-q', 'anthropic'])
        import anthropic

    client = anthropic.Anthropic(api_key=api_key)

    user_msg = f"Contact email: {email}\n\n"
    user_msg += f"## Raw gathered data (last {gathered_data.get('days_back', 30)} days):\n"
    user_msg += json.dumps(gathered_data, indent=2, default=str)[:15000]  # Limit size

    if existing_dossier:
        user_msg += f"\n\n## Existing dossier:\n"
        user_msg += json.dumps(existing_dossier, indent=2, default=str)[:5000]
    else:
        user_msg += "\n\n## No existing dossier — create a new one."

    try:
        response = client.messages.create(
            model=SYNTHESIS_MODEL,
            max_tokens=4096,
            system=SYNTHESIS_PROMPT,
            messages=[{"role": "user", "content": user_msg}],
        )
        text = response.content[0].text

        # Extract JSON from response
        json_start = text.find('{')
        json_end = text.rfind('}') + 1
        if json_start >= 0 and json_end > json_start:
            return json.loads(text[json_start:json_end])
    except Exception as e:
        log.error(f"Synthesis failed for {email}: {e}")

    return None


# ---------------------------------------------------------------------------
# Slack DM posting
# ---------------------------------------------------------------------------

def post_briefing(event, dossiers):
    """Post a pre-meeting briefing to Slack DM."""
    bot_token = os.environ.get('SLACK_BOT_TOKEN')
    owner_id = os.environ.get('SLACK_OWNER_USER_ID')

    if not bot_token or not owner_id:
        log.warning("SLACK_BOT_TOKEN or SLACK_OWNER_USER_ID not set, skipping Slack post.")
        return False

    try:
        from slack_sdk import WebClient
    except ImportError:
        import subprocess
        subprocess.check_call([sys.executable, '-m', 'pip', 'install', '-q', 'slack_sdk'])
        from slack_sdk import WebClient

    client = WebClient(token=bot_token)

    summary = event.get('summary', 'Meeting')
    start = event.get('start', {}).get('dateTime', event.get('start', {}).get('date', ''))

    blocks = [
        {
            "type": "header",
            "text": {"type": "plain_text", "text": f"Meeting Prep: {summary[:75]}"}
        },
        {
            "type": "context",
            "elements": [{"type": "mrkdwn", "text": f"Starts at {start}"}]
        },
        {"type": "divider"},
    ]

    for email, dossier in dossiers.items():
        if not dossier:
            continue
        name = dossier.get('name', email)
        rel = dossier.get('relationship', {})
        health = rel.get('health_score', '?')
        trend = rel.get('trend', '?')
        summary_text = rel.get('summary', 'No summary available')

        # Coaching notes
        coaching = dossier.get('coaching_notes', [])
        coaching_text = ''
        if coaching:
            latest = coaching[-1]
            coaching_text = f"\n> :bulb: _{latest.get('suggestion', '')}_"

        # Open threads
        threads = dossier.get('open_threads', [])
        threads_text = ''
        if threads:
            items = [f"- {t.get('topic', '?')} ({t.get('status', '?')})" for t in threads[:3]]
            threads_text = '\n'.join(items)

        section_text = f"*{name}* — Health: {health}/10 ({trend})\n{summary_text}"
        if coaching_text:
            section_text += coaching_text
        if threads_text:
            section_text += f"\n*Open threads:*\n{threads_text}"

        blocks.append({
            "type": "section",
            "text": {"type": "mrkdwn", "text": section_text[:3000]}
        })
        blocks.append({"type": "divider"})

    try:
        # Open DM channel
        dm = client.conversations_open(users=[owner_id])
        channel_id = dm['channel']['id']

        client.chat_postMessage(
            channel=channel_id,
            text=f"Meeting Prep: {event.get('summary', 'Meeting')}",
            blocks=blocks,
        )
        log.info(f"Posted briefing to Slack for: {event.get('summary', '')}")
        return True
    except Exception as e:
        log.error(f"Slack post failed: {e}")
        return False


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------

def process_event(event, state):
    """Process one upcoming event: gather, synthesize, post briefing."""
    event_id = event.get('id', '')
    summary = event.get('summary', '(No title)')

    if event_id in state.get('prepped_events', {}):
        return  # Already prepped

    attendees = event.get('attendees', [])
    external = [a for a in attendees if not a.get('self') and a.get('email', '') != MY_EMAIL]

    if not external:
        return  # No one to prep for

    log.info(f"Prepping for: {summary} ({len(external)} attendee(s))")

    dossiers = {}
    for attendee in external:
        email = attendee.get('email', '')
        if not email:
            continue

        # Gather data
        gathered = gather_for_contact(email, days=14)
        existing = read_existing_dossier(email)

        if gathered:
            # Synthesize
            updated = synthesize_dossier(email, gathered, existing)
            if updated:
                save_dossier(email, updated)
                dossiers[email] = updated
            elif existing:
                dossiers[email] = existing
        elif existing:
            dossiers[email] = existing

    if dossiers:
        post_briefing(event, dossiers)

    # Mark as prepped
    state.setdefault('prepped_events', {})[event_id] = datetime.now().isoformat()


def run_loop():
    """Main cron loop."""
    log.info(f"Dossier cron started. Checking every {CHECK_INTERVAL}s.")
    log.info(f"Looking for events {LOOKAHEAD_MIN}-{LOOKAHEAD_MAX} min ahead.")

    while True:
        try:
            state = load_state()
            events = get_upcoming_events()

            if events:
                log.info(f"Found {len(events)} upcoming event(s) in window.")
                for event in events:
                    try:
                        process_event(event, state)
                    except Exception as e:
                        log.error(f"Error processing event: {e}")
                        traceback.print_exc()

            save_state(state)

        except Exception as e:
            log.error(f"Cron loop error: {e}")
            traceback.print_exc()

        time.sleep(CHECK_INTERVAL)


if __name__ == '__main__':
    # Ensure UTF-8
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    if hasattr(sys.stderr, 'reconfigure'):
        sys.stderr.reconfigure(encoding='utf-8', errors='replace')

    run_loop()
