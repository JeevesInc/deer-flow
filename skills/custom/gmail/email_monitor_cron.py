#!/usr/bin/env python3
"""Email Monitor Cron — watches Gmail for new messages and surfaces them via Slack.

Model: watch ALL inbound email, filter OUT known noise (newsletters, automated
notifications, internal Jeeves chatter). Everything that passes the filter gets
surfaced as a Slack DM alert.

Actionable emails (diligence requests, DDQs, data requests) are automatically
dispatched to the DeerFlow agent for autonomous handling.

Behaviors:
  - Every 15 minutes: check for new unread emails in inbox
  - Skip messages from ignored senders/domains/patterns
  - Classify remaining emails: alert-only vs. actionable (diligence, etc.)
  - Alert via Slack DM with sender, subject, and snippet
  - For actionable emails: trigger an autonomous agent run to prepare materials
  - Tracks seen + dispatched message IDs to avoid duplicates

Env vars required:
  - GOOGLE_CLIENT_ID, GOOGLE_CLIENT_SECRET, GOOGLE_REFRESH_TOKEN
  - SLACK_BOT_TOKEN, SLACK_OWNER_USER_ID

Optional:
  - EMAIL_MONITOR_INTERVAL (seconds, default: 900 = 15 min)
  - EMAIL_MONITOR_IGNORE (comma-separated emails/domains to ignore, added to defaults)
  - EMAIL_DISPATCH_ENABLED (set to 'false' to disable autonomous dispatch)
"""

import json
import logging
import os
import re
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
    format='[EmailMonitor %(asctime)s] %(message)s',
    datefmt='%H:%M:%S',
)
log = logging.getLogger('email_monitor')

CHECK_INTERVAL = int(os.environ.get('EMAIL_MONITOR_INTERVAL', '900'))  # 15 min
MY_EMAIL = os.environ.get('GOOGLE_CALENDAR_EMAIL', 'brian.mauck@tryjeeves.com')
DISPATCH_ENABLED = os.environ.get('EMAIL_DISPATCH_ENABLED', 'true').lower() != 'false'

# ---------------------------------------------------------------------------
# Noise filters — messages matching any of these are silently skipped.
# Add to this list as new noise sources appear.
# Override/extend via EMAIL_MONITOR_IGNORE env var (comma-separated).
# ---------------------------------------------------------------------------

_IGNORED_DOMAINS = {
    # Automated / transactional
    'noreply@',
    'no-reply@',
    'notifications@',
    'notify@',
    'mailer-daemon@',
    'postmaster@',
    # Google
    '@google.com',
    '@googlemail.com',
    '@accounts.google.com',
    '@calendar-notification.google.com',
    # Common SaaS notifications
    '@slack.com',
    '@slackbot.com',
    '@linear.app',
    '@notion.so',
    '@github.com',
    '@gitlab.com',
    '@jira.atlassian.com',
    '@atlassian.com',
    '@zoom.us',
    '@zoom.com',
    '@calendly.com',
    '@docusign.net',
    '@hubspot.com',
    '@salesforce.com',
    '@intercom.io',
    '@stripe.com',
    '@brex.com',
    '@ramp.com',
    '@gusto.com',
    '@rippling.com',
    '@deel.com',
    '@lattice.com',
    '@lever.co',
    '@greenhouse.io',
    '@datadog.com',
    '@pagerduty.com',
    '@sentry.io',
    '@figma.com',
    '@loom.com',
    '@dropbox.com',
    '@box.com',
    '@asana.com',
    '@monday.com',
    '@clickup.com',
    '@airtable.com',
    '@mailchimp.com',
    '@sendgrid.net',
    '@amazonses.com',
    '@postmarkapp.com',
    # Marketing / newsletters
    '@substack.com',
    '@medium.com',
    '@linkedin.com',
    '@twitter.com',
    '@x.com',
    '@facebook.com',
    '@facebookmail.com',
    '@instagram.com',
}

_IGNORED_SUBJECT_PATTERNS = [
    re.compile(r'^\s*re:\s*invitation:', re.IGNORECASE),  # Calendar re-invites
    re.compile(r'^invitation:', re.IGNORECASE),
    re.compile(r'^accepted:', re.IGNORECASE),
    re.compile(r'^declined:', re.IGNORECASE),
    re.compile(r'^tentatively accepted:', re.IGNORECASE),
    re.compile(r'^canceled event:', re.IGNORECASE),
    re.compile(r'^updated invitation:', re.IGNORECASE),
    re.compile(r'unsubscribe', re.IGNORECASE),
    re.compile(r'your .* receipt', re.IGNORECASE),
    re.compile(r'your .* invoice', re.IGNORECASE),
    re.compile(r'password reset', re.IGNORECASE),
    re.compile(r'verify your email', re.IGNORECASE),
    re.compile(r'two-factor|2fa|mfa', re.IGNORECASE),
    re.compile(r'sign-in .* new device', re.IGNORECASE),
    re.compile(r'security alert', re.IGNORECASE),
    re.compile(r'out of office', re.IGNORECASE),
    re.compile(r'automatic reply', re.IGNORECASE),
]

# Also ignore own domain's automated senders (but NOT humans at tryjeeves.com)
_IGNORED_JEEVES_SENDERS = {
    'notifications@tryjeeves.com',
    'noreply@tryjeeves.com',
    'no-reply@tryjeeves.com',
    'alerts@tryjeeves.com',
    'system@tryjeeves.com',
}

# Load extra ignores from env var
_extra_ignore = os.environ.get('EMAIL_MONITOR_IGNORE', '')
_EXTRA_IGNORED = {s.strip().lower() for s in _extra_ignore.split(',') if s.strip()}

# Max messages to check per cycle
MAX_RESULTS = 30
# How far back to search on first run (hours)
INITIAL_LOOKBACK_HOURS = 4
# Max seen IDs to keep in state (FIFO)
MAX_SEEN_IDS = 1000
# Max dispatched IDs to keep in state (FIFO)
MAX_DISPATCHED_IDS = 500


# ---------------------------------------------------------------------------
# Dispatch config — loaded from JSON, editable by the agent at runtime
# ---------------------------------------------------------------------------

_DISPATCH_CONFIG_PATH = str(
    Path(__file__).resolve().parent.parent.parent.parent
    / 'backend' / '.deer-flow' / 'dispatch_config.json'
)

_cached_config = None
_config_mtime = 0.0


def _load_dispatch_config() -> dict:
    """Load dispatch config, with mtime-based cache invalidation."""
    global _cached_config, _config_mtime
    try:
        mtime = os.path.getmtime(_DISPATCH_CONFIG_PATH)
        if _cached_config is not None and mtime == _config_mtime:
            return _cached_config
        with open(_DISPATCH_CONFIG_PATH) as f:
            _cached_config = json.load(f)
        _config_mtime = mtime
        log.info("Dispatch config loaded (mtime=%.0f)", mtime)
        return _cached_config
    except FileNotFoundError:
        log.warning("Dispatch config not found at %s, using empty config", _DISPATCH_CONFIG_PATH)
        return {'enabled': False, 'action_types': {}, 'counterparties': {}}
    except Exception as e:
        log.error("Failed to load dispatch config: %s", e)
        return _cached_config or {'enabled': False, 'action_types': {}, 'counterparties': {}}


ALERT_ONLY = 'alert_only'

# Subject markers that promote an email to "important" regardless of sender.
_URGENT_SUBJECT_PATTERNS = [
    re.compile(r'\burgent\b', re.IGNORECASE),
    re.compile(r'\basap\b', re.IGNORECASE),
    re.compile(r'\bemergency\b', re.IGNORECASE),
    re.compile(r'\btime[-\s]sensitive\b', re.IGNORECASE),
    re.compile(r'\bdeadline\b', re.IGNORECASE),
    re.compile(r'\bovers?ight\b', re.IGNORECASE),
    re.compile(r'\baction required\b', re.IGNORECASE),
    re.compile(r'\bplease respond\b', re.IGNORECASE),
    re.compile(r'\bfollow[-\s]?up\b', re.IGNORECASE),
]


def _build_counterparty_map(config: dict) -> dict[str, str]:
    """Build domain → counterparty name mapping from config."""
    result = {}
    for name, info in config.get('counterparties', {}).items():
        for domain in info.get('domains', []):
            result[domain.lower()] = name
    return result


def _detect_counterparty(sender_email: str, subject: str) -> str | None:
    """Try to identify the counterparty from sender domain or subject line."""
    config = _load_dispatch_config()
    cp_map = _build_counterparty_map(config)

    domain = sender_email.split('@')[-1].lower() if '@' in sender_email else ''
    if domain in cp_map:
        return cp_map[domain]
    subj_lower = subject.lower()
    for name in config.get('counterparties', {}):
        if name.lower() in subj_lower:
            return name
    return None


def _get_counterparty_folders(counterparty: str) -> dict:
    """Get Drive folder IDs for a counterparty from config."""
    config = _load_dispatch_config()
    return config.get('counterparties', {}).get(counterparty, {}).get('folders', {})


def _classify_email(sender_email: str, subject: str, snippet: str) -> str:
    """Classify an email into an action category using dispatch config.

    Returns an action type string or ALERT_ONLY.
    """
    config = _load_dispatch_config()
    if not config.get('enabled', True):
        return ALERT_ONLY

    subj_lower = (subject or '').lower()
    text_lower = f'{subject} {snippet}'.lower()
    counterparty = _detect_counterparty(sender_email, subject)

    for action_name, action_cfg in config.get('action_types', {}).items():
        if not action_cfg.get('enabled', True):
            continue

        # Strong signal: subject keywords
        for kw in action_cfg.get('subject_keywords', []):
            if kw.lower() in subj_lower:
                return action_name

        # Medium signal: body keywords + known counterparty
        body_keywords = action_cfg.get('body_keywords', [])
        if counterparty:
            for kw in body_keywords:
                if kw.lower() in text_lower:
                    return action_name
            # Known counterparty + multiple substantive words
            sub_words = action_cfg.get('counterparty_substantive_words', [])
            sub_thresh = action_cfg.get('counterparty_substantive_threshold', 2)
            if sub_words:
                matches = sum(1 for w in sub_words if w.lower() in text_lower)
                if matches >= sub_thresh:
                    return action_name

        # Weak signal: multiple body keywords
        body_thresh = action_cfg.get('body_keyword_threshold', 2)
        body_hits = sum(1 for kw in body_keywords if kw.lower() in text_lower)
        if body_hits >= body_thresh:
            return action_name

    return ALERT_ONLY


def _build_diligence_prompt(alert: dict) -> str:
    """Build a structured agent prompt for handling a diligence request."""
    msg_id = alert['id']
    sender = alert['from']
    sender_email = alert['sender_email']
    subject = alert['subject'] or '(no subject)'
    snippet = alert['snippet']
    date = alert.get('date', 'unknown')

    counterparty = _detect_counterparty(sender_email, subject)

    # Build counterparty context section
    cp_context = ''
    if counterparty:
        folders = _get_counterparty_folders(counterparty)
        cp_context = f"\nIdentified counterparty: **{counterparty}**\n"
        if folders:
            cp_context += "Known Drive folders:\n"
            for label, fid in folders.items():
                cp_context += f"  - {label}: {fid}\n"

    return f"""AUTONOMOUS DILIGENCE TASK — proactive handling, Brian will review when ready.

A new email just arrived that appears to be a diligence-related request.

From: {sender}
Subject: {subject}
Date: {date}
Preview: {snippet}
Gmail Message ID: {msg_id}
{cp_context}
---

## Instructions

Follow these steps to prepare diligence materials:

1. **Read the full email**:
   ```bash
   python /mnt/skills/custom/gmail/gmail_tool.py read {msg_id}
   ```

2. **Download any attachments** (DDQ docs, question lists, etc.):
   ```bash
   python /mnt/skills/custom/gmail/gmail_tool.py download {msg_id}
   ```

3. **Identify what is being requested** — categorize as:
   - DDQ (due diligence questionnaire with specific questions)
   - Data package request (portfolio metrics, tape, etc.)
   - Document request (specific policies, legal docs, etc.)
   - General information request

4. **Pull the latest portfolio data**:
   ```bash
   python /mnt/skills/custom/jeeves-diligence/diligence_tool.py gather-portfolio
   ```

5. **If DDQ questions are present**, extract them to a text file and scaffold responses:
   ```bash
   python /mnt/skills/custom/jeeves-diligence/diligence_tool.py ddq-scaffold --input /mnt/user-data/outputs/questions.txt
   ```

6. **Check Google Drive** for relevant existing documents{f' in the {counterparty} diligence folder' if counterparty else ''}

7. **Organize all prepared materials** in /mnt/user-data/outputs/ with a clear structure

8. **Write a summary** at the end of your response with:
   - What was requested (one-line description)
   - What you prepared (list of files/data with paths)
   - What still needs Brian's input or approval
   - Any red flags or concerns
   - Suggested reply draft (if appropriate)

## Rules
- Follow ALL rules from the jeeves-diligence skill
- NEVER fabricate data — every claim needs a verified source
- Data is only available through yesterday — never query today's date
- If you cannot complete a step, note what's missing and move on
- This is a draft for Brian's review — mark anything uncertain as [Needs Confirmation]
"""


# ---------------------------------------------------------------------------
# Filtering
# ---------------------------------------------------------------------------

def _is_counterparty_sender(sender_email: str) -> bool:
    """Return True if the sender's domain matches a known counterparty."""
    if '@' not in sender_email:
        return False
    domain = sender_email.split('@')[-1].lower()
    cp_map = _build_counterparty_map(_load_dispatch_config())
    return domain in cp_map


def _is_directly_addressed(to_header: str, cc_header: str) -> bool:
    """True if MY_EMAIL appears in To (not just Cc) and the email isn't blast-mailed.

    Direct = recipient list contains me AND total recipient count is small (<=3).
    """
    my = MY_EMAIL.lower()
    to_addrs = [a.strip().lower() for a in re.findall(r'[\w.+-]+@[\w.-]+', to_header or '')]
    cc_addrs = [a.strip().lower() for a in re.findall(r'[\w.+-]+@[\w.-]+', cc_header or '')]
    if my not in to_addrs:
        return False
    return len(to_addrs) + len(cc_addrs) <= 3


def _llm_is_important(alert: dict) -> bool:
    """Use an LLM to determine if an email was written by a human and warrants Brian's attention.

    Returns True if the model judges it important, False if automated/noise.
    Falls back to True (notify) on any error, to avoid silent drops.
    """
    try:
        import anthropic
        client = anthropic.Anthropic(api_key=os.environ.get('ANTHROPIC_API_KEY'))

        prompt = f"""You are a filter for Brian Mauck's inbox. Brian is Head of Capital Markets at Jeeves Financial Technology.
He only wants to be notified about emails that were written by a real human specifically intending to communicate with him — lenders, counterparties, colleagues, vendors, etc.

He does NOT want notifications for:
- Automated system emails (bank statements, account alerts, trust reports, scheduled notifications)
- Newsletter / marketing emails
- Automated receipts or invoices
- Calendar notifications
- Any email where the sender is clearly a system, bot, or automated mailer

Here is the email metadata:
From: {alert.get('from', '')}
Subject: {alert.get('subject', '')}
Snippet: {alert.get('snippet', '')}

Is this email from a real human intentionally writing to Brian (not automated)?
Answer with exactly one word: YES or NO."""

        response = client.messages.create(
            model='claude-haiku-4-5-20251001',
            max_tokens=5,
            messages=[{'role': 'user', 'content': prompt}],
        )
        answer = response.content[0].text.strip().upper()
        log.info("LLM importance check for '%s': %s", alert.get('subject', '')[:60], answer)
        return answer.startswith('YES')
    except Exception as e:
        log.error("LLM importance check failed, defaulting to notify: %s", e)
        return True


def _is_important(alert: dict) -> bool:
    """Decide whether an email is worth notifying about.

    Fast-path: always surface actionable emails (diligence dispatches, etc.).
    Everything else goes through LLM classification — model decides if it's
    a real human writing to Brian vs. an automated system.
    """
    # Always surface actionable emails (diligence, etc.)
    if alert.get('action', ALERT_ONLY) != ALERT_ONLY:
        return True

    # LLM decides everything else
    return _llm_is_important(alert)


def _is_noise(sender_email: str, subject: str) -> bool:
    """Return True if this message should be silently skipped."""
    email_lower = sender_email.lower()

    # Own email
    if email_lower == MY_EMAIL.lower():
        return True

    # Specific ignored addresses
    if email_lower in _IGNORED_JEEVES_SENDERS or email_lower in _EXTRA_IGNORED:
        return True

    # Dynamic ignored senders from dispatch config (domains or full addresses)
    try:
        _dyn_ignored = _load_dispatch_config().get('ignored_senders', [])
        for pattern in _dyn_ignored:
            pattern_lower = pattern.lower()
            if (email_lower == pattern_lower
                    or email_lower.endswith('@' + pattern_lower)
                    or email_lower.endswith('.' + pattern_lower)):
                return True
    except Exception:
        pass

    # Domain/prefix matches
    for pattern in _IGNORED_DOMAINS:
        if pattern.startswith('@'):
            # Domain match: sender ends with @domain.com
            if email_lower.endswith(pattern.lower()):
                return True
        else:
            # Prefix match: sender starts with noreply@, etc.
            if email_lower.startswith(pattern.lower()):
                return True

    # Extra ignores from env (can be domains or full addresses)
    for pattern in _EXTRA_IGNORED:
        if pattern.startswith('@') and email_lower.endswith(pattern):
            return True
        if email_lower == pattern:
            return True

    # Subject patterns
    for pat in _IGNORED_SUBJECT_PATTERNS:
        if pat.search(subject or ''):
            return True

    return False


# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------

def _state_path():
    backend = Path(__file__).resolve().parent.parent.parent.parent / 'backend' / '.deer-flow'
    os.makedirs(backend, exist_ok=True)
    return str(backend / '_email_monitor_state.json')


def load_state():
    path = _state_path()
    if os.path.exists(path):
        with open(path) as f:
            return json.load(f)
    return {'seen_ids': [], 'dispatched_ids': [], 'last_check': None}


def save_state(state):
    if len(state.get('seen_ids', [])) > MAX_SEEN_IDS:
        state['seen_ids'] = state['seen_ids'][-MAX_SEEN_IDS:]
    if len(state.get('dispatched_ids', [])) > MAX_DISPATCHED_IDS:
        state['dispatched_ids'] = state['dispatched_ids'][-MAX_DISPATCHED_IDS:]
    state['last_check'] = datetime.now().isoformat()
    with open(_state_path(), 'w') as f:
        json.dump(state, f, indent=2)


# ---------------------------------------------------------------------------
# Gmail
# ---------------------------------------------------------------------------

def _get_service():
    from google_auth import get_credentials
    from googleapiclient.discovery import build
    creds = get_credentials(required=False)
    if not creds:
        return None
    return build('gmail', 'v1', credentials=creds)


def _extract_header(headers, name):
    for h in headers:
        if h.get('name', '').lower() == name.lower():
            return h.get('value', '')
    return ''


def _extract_sender_email(from_header):
    match = re.search(r'<([^>]+)>', from_header)
    if match:
        return match.group(1).lower()
    return from_header.strip().lower()


def _clean_snippet(snippet):
    import html
    return html.unescape(snippet).strip()


def check_new_emails(state):
    """Check for new unread inbox emails. Returns list of alerts (noise filtered out)."""
    service = _get_service()
    if service is None:
        log.warning("Gmail credentials not configured, skipping.")
        return []

    # Time-bounded query
    last_check = state.get('last_check')
    if last_check:
        after_dt = datetime.fromisoformat(last_check) - timedelta(minutes=5)
    else:
        after_dt = datetime.now() - timedelta(hours=INITIAL_LOOKBACK_HOURS)

    after_epoch = str(int(after_dt.timestamp()))
    query = f'is:unread in:inbox after:{after_epoch}'

    log.info(f"Gmail query: {query}")

    try:
        results = service.users().messages().list(
            userId='me', q=query, maxResults=MAX_RESULTS
        ).execute()
    except Exception as e:
        log.error(f"Gmail search failed: {e}")
        return []

    messages = results.get('messages', [])
    if not messages:
        log.info("No new unread messages.")
        return []

    log.info(f"Found {len(messages)} unread message(s), filtering noise...")

    seen_ids = set(state.get('seen_ids', []))
    alerts = []
    noise_count = 0

    for msg_stub in messages:
        msg_id = msg_stub['id']
        if msg_id in seen_ids:
            continue

        try:
            msg = service.users().messages().get(
                userId='me', id=msg_id, format='metadata',
                metadataHeaders=['From', 'Subject', 'Date', 'To', 'Cc'],
            ).execute()
        except Exception as e:
            log.error(f"Failed to read message {msg_id}: {e}")
            continue

        headers = msg.get('payload', {}).get('headers', [])
        from_header = _extract_header(headers, 'From')
        subject = _extract_header(headers, 'Subject')
        date = _extract_header(headers, 'Date')
        to_header = _extract_header(headers, 'To')
        cc_header = _extract_header(headers, 'Cc')
        snippet = _clean_snippet(msg.get('snippet', ''))
        sender_email = _extract_sender_email(from_header)

        # Mark as seen regardless of filter result
        seen_ids.add(msg_id)

        if _is_noise(sender_email, subject):
            noise_count += 1
            continue

        action = _classify_email(sender_email, subject, snippet)

        alerts.append({
            'id': msg_id,
            'from': from_header,
            'sender_email': sender_email,
            'subject': subject,
            'date': date,
            'to': to_header,
            'cc': cc_header,
            'snippet': snippet,
            'action': action,
        })

    if noise_count:
        log.info(f"Filtered {noise_count} noise message(s).")

    state['seen_ids'] = list(seen_ids)
    return alerts


# ---------------------------------------------------------------------------
# Slack
# ---------------------------------------------------------------------------

def _post_slack(text, blocks=None):
    token = os.environ.get('SLACK_BOT_TOKEN')
    owner_id = os.environ.get('SLACK_OWNER_USER_ID')
    if not token or not owner_id:
        log.warning("Slack not configured.")
        return False
    try:
        from slack_sdk import WebClient
        client = WebClient(token=token)
        dm = client.conversations_open(users=[owner_id])
        channel_id = dm['channel']['id']
        client.chat_postMessage(channel=channel_id, text=text, blocks=blocks)
        return True
    except Exception as e:
        log.error(f"Slack post failed: {e}")
        return False


def post_email_alerts(alerts):
    """Post email alerts to Slack DM."""
    if not alerts:
        return

    blocks = [
        {"type": "header", "text": {"type": "plain_text", "text": f"New Email{'s' if len(alerts) > 1 else ''}"}},
        {"type": "divider"},
    ]

    for alert in alerts[:10]:
        sender = alert['from']
        subject = alert['subject'] or '(no subject)'
        snippet = alert['snippet'][:200] if alert['snippet'] else ''
        date = alert['date']
        action = alert.get('action', ALERT_ONLY)

        # Tag diligence-classified emails so Brian sees what's being auto-handled
        action_tag = ''
        if action != ALERT_ONLY:
            counterparty = _detect_counterparty(alert['sender_email'], alert['subject'] or '')
            cp_label = f' ({counterparty})' if counterparty else ''
            action_tag = f'  [DILIGENCE{cp_label} — auto-handling]\n'

        section_text = f"*{subject}*{action_tag}\nFrom: {sender}"
        if date:
            section_text += f"\n{date}"
        if snippet:
            section_text += f"\n>{snippet}"

        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": section_text}})
        blocks.append({"type": "divider"})

    remaining = len(alerts) - 10
    if remaining > 0:
        blocks.append({
            "type": "context",
            "elements": [{"type": "mrkdwn", "text": f"_+{remaining} more message(s)_"}]
        })

    _post_slack(
        text=f"{len(alerts)} new email(s)",
        blocks=blocks,
    )
    log.info(f"Posted {len(alerts)} email alert(s) to Slack.")


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------

def _dispatch_actionable_emails(alerts: list[dict], state: dict) -> None:
    """Dispatch actionable emails to the DeerFlow agent."""
    if not DISPATCH_ENABLED:
        return

    config = _load_dispatch_config()
    if not config.get('enabled', True):
        return

    dispatched_ids = set(state.get('dispatched_ids', []))
    new_dispatches = []

    # Prompt builders per action type (extensible — add new types here)
    prompt_builders = {
        'diligence': _build_diligence_prompt,
    }

    for alert in alerts:
        action = alert.get('action', ALERT_ONLY)
        if action == ALERT_ONLY:
            continue
        if alert['id'] in dispatched_ids:
            continue

        builder = prompt_builders.get(action)
        if not builder:
            log.warning("No prompt builder for action type '%s', skipping msg %s", action, alert['id'])
            continue

        counterparty = _detect_counterparty(alert['sender_email'], alert['subject'] or '')
        cp_label = f' from {counterparty}' if counterparty else ''
        category = action.replace('_', ' ').title()

        prompt = builder(alert)
        notification = (
            f"*{category} request detected{cp_label}*\n"
            f"From: {alert['from']}\n"
            f"Subject: {alert['subject'] or '(no subject)'}\n\n"
            f"_Working on it — I'll post a summary when done._"
        )

        try:
            from autonomous_dispatch import dispatch
            source_metadata = {
                "from": alert.get("from"),
                "sender_email": alert.get("sender_email"),
                "subject": alert.get("subject"),
                "counterparty": counterparty,
                "action": action,
            }
            if dispatch(
                prompt,
                notification=notification,
                category=category,
                source_id=alert["id"],
                source_metadata=source_metadata,
            ):
                new_dispatches.append(alert['id'])
                log.info("Dispatched %s task for msg %s%s", action, alert['id'], cp_label)
            else:
                log.warning("Dispatch at capacity, skipping msg %s", alert['id'])
        except Exception as e:
            log.error("Failed to dispatch %s task: %s", action, e)

    if new_dispatches:
        dispatched_ids.update(new_dispatches)
        state['dispatched_ids'] = list(dispatched_ids)


def run_loop():
    log.info(f"Email monitor started. Checking every {CHECK_INTERVAL}s.")
    log.info(f"Noise filter: {len(_IGNORED_DOMAINS)} domains, "
             f"{len(_IGNORED_SUBJECT_PATTERNS)} subject patterns, "
             f"{len(_IGNORED_JEEVES_SENDERS)} internal senders")
    log.info(f"Autonomous dispatch: {'ENABLED' if DISPATCH_ENABLED else 'DISABLED'}")

    while True:
        try:
            state = load_state()
            alerts = check_new_emails(state)
            if alerts:
                important = [a for a in alerts if _is_important(a)]
                skipped = len(alerts) - len(important)
                if skipped:
                    log.info(f"Skipped {skipped} non-important alert(s).")
                if important:
                    post_email_alerts(important)
                _dispatch_actionable_emails(alerts, state)
            save_state(state)
        except Exception as e:
            log.error(f"Monitor loop error: {e}")
            traceback.print_exc()

        time.sleep(CHECK_INTERVAL)


if __name__ == '__main__':
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    if hasattr(sys.stderr, 'reconfigure'):
        sys.stderr.reconfigure(encoding='utf-8', errors='replace')

    import argparse
    parser = argparse.ArgumentParser(description='Email Monitor Cron')
    parser.add_argument('mode', nargs='?', choices=['check'],
                        help='Run one check instead of looping')
    args = parser.parse_args()

    if args.mode == 'check':
        state = load_state()
        alerts = check_new_emails(state)
        if alerts:
            actionable = [a for a in alerts if a.get('action') != ALERT_ONLY]
            alert_only = [a for a in alerts if a.get('action') == ALERT_ONLY]
            important = [a for a in alerts if _is_important(a)]
            if important:
                post_email_alerts(important)
            print(f"Found {len(alerts)} new email(s): "
                  f"{len(actionable)} actionable, {len(alert_only)} alert-only, "
                  f"{len(important)} important (posted), {len(alerts) - len(important)} skipped")
            for a in actionable:
                cp = _detect_counterparty(a['sender_email'], a['subject'] or '')
                print(f"  {a['action'].upper()}: {a['subject']} (from {a['sender_email']}"
                      f"{f', counterparty={cp}' if cp else ''})")
            if DISPATCH_ENABLED and actionable:
                _dispatch_actionable_emails(alerts, state)
                print(f"Dispatched {len(actionable)} task(s)")
        else:
            print("No new emails after filtering.")
        save_state(state)
    else:
        run_loop()
