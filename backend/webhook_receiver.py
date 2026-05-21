#!/usr/bin/env python3
"""
Webhook Receiver — Gmail Pub/Sub + Slack Event API → LLM → DeerFlow

Replaces the cron-based keyword classifier with:
  1. Webhook receipt (Gmail push or Slack event)
  2. LLM classification call with full analyst context
  3. If actionable → DeerFlow agent run via LangGraph API
  4. Slack DM notification regardless of outcome

Run with:
  uvicorn webhook_receiver:app --host 0.0.0.0 --port 8080

Tunnel with:
  cloudflared tunnel --url http://localhost:8080
  # or: ngrok http 8080

Required env vars (same .env as the rest of the stack):
  ANTHROPIC_API_KEY       — for LLM classification
  SLACK_BOT_TOKEN         — for notifications
  SLACK_OWNER_USER_ID     — Brian's Slack user ID
  GOOGLE_CLIENT_ID        — Gmail API
  GOOGLE_CLIENT_SECRET
  GOOGLE_REFRESH_TOKEN
  LANGGRAPH_URL           — DeerFlow LangGraph endpoint (default: http://localhost:2024)
  PUBSUB_VERIFICATION_TOKEN — (optional) shared secret set in GCP Pub/Sub push config
  SLACK_SIGNING_SECRET    — for verifying Slack payloads
"""

import base64
import hashlib
import hmac
import json
import logging
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx
from anthropic import Anthropic
from fastapi import FastAPI, Header, HTTPException, Request, Response
from fastapi.responses import JSONResponse

# ---------------------------------------------------------------------------
# Bootstrap — load .env from the DeerFlow backend directory
# ---------------------------------------------------------------------------
_SKILLS_ROOT = Path(__file__).resolve().parent.parent / 'skills' / 'custom' / '_shared'
if _SKILLS_ROOT.exists():
    sys.path.insert(0, str(_SKILLS_ROOT))
    try:
        from env_loader import load_env
        load_env()
    except ImportError:
        pass

# Fallback: manual .env scan
_ENV_FILE = Path(__file__).resolve().parent / '.env'
if _ENV_FILE.exists() and not os.environ.get('ANTHROPIC_API_KEY'):
    for _line in _ENV_FILE.read_text().splitlines():
        _line = _line.strip()
        if _line and not _line.startswith('#') and '=' in _line:
            _k, _, _v = _line.partition('=')
            if _k.strip() not in os.environ:
                os.environ[_k.strip()] = _v.strip().strip('"').strip("'")

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
LANGGRAPH_URL = os.environ.get('LANGGRAPH_URL', 'http://localhost:2024')
ASSISTANT_ID = 'lead_agent'
ANTHROPIC_API_KEY = os.environ.get('ANTHROPIC_API_KEY', '')
SLACK_BOT_TOKEN = os.environ.get('SLACK_BOT_TOKEN', '')
SLACK_OWNER_USER_ID = os.environ.get('SLACK_OWNER_USER_ID', 'U09PQTZ5DHC')
SLACK_SIGNING_SECRET = os.environ.get('SLACK_SIGNING_SECRET', '')
PUBSUB_VERIFICATION_TOKEN = os.environ.get('PUBSUB_VERIFICATION_TOKEN', '')
MY_EMAIL = os.environ.get('GOOGLE_CALENDAR_EMAIL', 'brian.mauck@tryjeeves.com')
CLASSIFIER_MODEL = 'claude-haiku-4-5-20251001'  # fast + cheap for classification
RUN_TIMEOUT = 20 * 60  # seconds

# Proposal feedback loop — paths and config
PROPOSAL_LOG_PATH = Path(__file__).resolve().parent / '.deer-flow' / 'proposal_log.jsonl'
PROPOSAL_PATTERNS_USER_ID = 'proposal-patterns'  # mem0 user_id namespace
PROPOSAL_PATTERN_INJECTION_LIMIT = 5  # top-N patterns injected per classify call

logging.basicConfig(
    level=logging.INFO,
    format='[Webhook %(asctime)s] %(levelname)s %(message)s',
    datefmt='%H:%M:%S',
)
log = logging.getLogger('webhook')

app = FastAPI(title='DeerFlow Webhook Receiver')

# ---------------------------------------------------------------------------
# ANALYST CONTEXT — injected into every LLM classification call
# This is what makes classification smart vs keyword matching.
# Update this as priorities/deals change.
# ---------------------------------------------------------------------------
ANALYST_CONTEXT = """
You are classifying inbound messages for Brian Mauck, Head of Capital Markets at Jeeves Financial Technology.

## Who Brian is
Brian runs capital markets at Jeeves — a B2B fintech providing corporate cards and working capital 
to businesses across Latin America and the US. He manages lender relationships, credit facilities, 
borrowing bases, and portfolio analytics. He works directly with the CFO (Alex Melikian).

## Active deals and workstreams (as of May 2026)
- **BBVA** — SBLC ($14MM) + Fideicomiso Trust Agreement under negotiation. W&C (White & Case) 
  is Jeeves' counsel. Documents exchanged May 14–19. Aforo amount still TBD. Ball is in BBVA's 
  court after May 19 revisions. CONTACT: David García (BBVA Spark).
- **Neuberger Berman (NB)** — $100MM facility (expandable to $150MM), SOFR+7.5%, 24mo. Term 
  sheet executed April 2026. Legal docs in progress via Akin Gump (NB counsel) + Goodwin (Jeeves).
- **CIM** — 5th Amendment active. Section 7b concentration/cap dispute ongoing. CIM trying to push 
  Colombian portfolio into an SPV.
- **Gramercy, Francisco Partners, Vista Credit** — active lender relationships, periodic reporting.
- **Daily borrowing bases** — US Bridge and MX SOFOM produced daily.
- **BBVA diligence** — 40-item checklist, ~9 items still outstanding as of late April.

## Key counterparties (emails from these domains are higher priority)
- bbva.com — BBVA lender
- nb.com, neuberger.com — Neuberger Berman
- cim-llc.com — CIM
- gramercyfunds.com — Gramercy
- franciscopartners.com — Francisco Partners
- vistaequity.com, vistacredit.com — Vista Credit
- whitecase.com — W&C (Jeeves' counsel — outbound legal work, NOT inbound requests)
- akingump.com — Akin Gump (NB's counsel)
- goodwinlaw.com — Goodwin (Jeeves' counsel)
- tryjeeves.com — Internal Jeeves (usually not actionable unless from Alex Melikian or leadership)

## What IS actionable (Brian needs to do something, or have the agent do it)
- Lender/counterparty asks for data, a tape, a report, or documents
- Counterparty asks a specific question that requires Brian's input or pulling data
- Lender follows up on an open checklist item
- Term sheet, amendment, redline, or legal doc sent for Brian's review
- Meeting request, scheduling ask, or proposed time from a counterparty
- C-suite (Alex Melikian, CFO) asks for something
- Anything with an explicit deadline

## What is NOT actionable (skip — no Slack post)
- W&C, Goodwin, or Akin Gump sending docs TO a counterparty on Jeeves' behalf (outbound legal)
- External counsel updating each other without asking Brian for anything
- Automated notifications, receipts, calendar invites, SaaS alerts
- Internal Jeeves emails that aren't from leadership
- Newsletters, marketing, LinkedIn, recruiter emails

## Decision framework
Ask: "Is there something specific Brian (or an agent on his behalf) needs to do?"
- If yes → actionable, and you MUST name the concrete next step
- If it's just FYI / outbound / automated → not actionable
- If ambiguous → not actionable (avoid noise)

## When actionable, the proposed action must be:
- ONE concrete next step, starting with a verb
- Specific enough that the agent (or Brian) could execute it immediately
- Examples of good actions:
    "Pull the May MX SOFOM borrowing base and reply with the file attached"
    "Confirm Tuesday 2pm meeting and add to calendar"
    "Review the redlined SBLC and reply to David García with comments"
    "Forward to Goodwin for review and ask for turnaround by Friday"
    "Draft a short reply confirming the Aforo amount is still pending"
- Bad actions (too vague — never produce these):
    "Respond to the email"
    "Handle this request"
    "Follow up"
"""

# ---------------------------------------------------------------------------
# LLM Classifier
# ---------------------------------------------------------------------------

def classify_with_llm(source: str, sender: str, subject: str, body_preview: str,
                       extra_context: str = '') -> dict:
    """
    Call Claude to classify the message. Returns:
    {
      "actionable": bool,
      "priority": "high" | "medium" | "low",
      "category": str,          # free-form short label, 2-5 words (e.g. "BBVA data request", "NB legal review")
      "summary": str,           # 1-2 sentences of what the email actually says
      "proposed_action": str,   # ONE concrete verb-led next step (empty if not actionable)
      "reasoning": str,         # brief explanation of the classification
    }
    """
    if not ANTHROPIC_API_KEY:
        log.error('No ANTHROPIC_API_KEY — cannot classify, defaulting to non-actionable')
        return _default_classification('No API key configured')

    client = Anthropic(api_key=ANTHROPIC_API_KEY)

    # Pull mem0-learned proposal patterns relevant to this sender/subject.
    import re
    sender_match = re.search(r'<([^>]+)>', sender)
    sender_email = sender_match.group(1).lower() if sender_match else sender.strip().lower()
    sender_domain = sender_email.rsplit('@', 1)[-1] if '@' in sender_email else ''
    learned_patterns = _get_proposal_patterns(sender_email, sender_domain, subject)
    patterns_block = ''
    if learned_patterns:
        bullets = '\n'.join(f'- {p}' for p in learned_patterns)
        patterns_block = (
            "\n\n## Learned patterns from past proposals (most relevant first)\n"
            "These come from labeled feedback on prior proposals. Apply them when relevant:\n"
            f"{bullets}\n"
        )

    prompt = f"""You are a classifier for Brian Mauck's inbound communications.

{ANALYST_CONTEXT}{patterns_block}

---

## Message to classify

Source: {source}
Sender: {sender}
Subject: {subject}
Body: {body_preview[:2000]}
{f"Additional context: {extra_context}" if extra_context else ""}

---

Respond with ONLY valid JSON in this exact format:
{{
  "actionable": true or false,
  "priority": "high" | "medium" | "low",
  "category": "2-5 word free-form label naming the counterparty + nature (e.g. 'BBVA data request', 'NB redline', 'CFO ask', 'CIM amendment')",
  "summary": "1-2 sentences describing what the email actually says — the substance, not 'an email about X'. Name the counterparty, what they sent or asked, and any deadline. Do not quote the email verbatim.",
  "proposed_action": "ONE concrete verb-led next step that Brian or the agent should take. Empty string if not actionable. Must be specific enough to execute immediately. Re-read the action guidance above before writing this.",
  "reasoning": "1 sentence: why this classification"
}}

Be decisive. Outbound legal work from Jeeves' counsel → actionable=false. Inbound asks from lenders/counterparties/leadership → actionable=true with a concrete proposed_action."""

    try:
        response = client.messages.create(
            model=CLASSIFIER_MODEL,
            max_tokens=512,
            messages=[{'role': 'user', 'content': prompt}],
        )
        text = response.content[0].text.strip()
        # Strip markdown code fences if present
        if text.startswith('```'):
            text = text.split('```')[1]
            if text.startswith('json'):
                text = text[4:]
        result = json.loads(text.strip())
        log.info('LLM classification: actionable=%s | priority=%s | category=%s | %s',
                 result.get('actionable'), result.get('priority'),
                 result.get('category', ''), result.get('reasoning', '')[:80])
        return result
    except Exception as e:
        log.error('LLM classification failed: %s', e)
        return _default_classification(str(e))


def _default_classification(reason: str) -> dict:
    return {
        'actionable': False,
        'priority': 'low',
        'category': 'unclassified',
        'summary': '',
        'proposed_action': '',
        'reasoning': f'Classification failed: {reason}',
    }


# ---------------------------------------------------------------------------
# Proposal feedback loop — logging + mem0 pattern injection
# ---------------------------------------------------------------------------

def _log_proposal(classification: dict, from_header: str, subject: str,
                  gmail_msg_id: str, slack_post: dict) -> None:
    """Append a record of this proposal to proposal_log.jsonl.

    Joined later by proposal_learner.py with the Slack thread replies to
    label outcomes and synthesize patterns.
    """
    import re
    if not slack_post.get('ts'):
        # No Slack post means nothing to pair with later — skip logging.
        return

    sender_match = re.search(r'<([^>]+)>', from_header)
    sender_email = sender_match.group(1).lower() if sender_match else from_header.strip().lower()
    sender_domain = sender_email.rsplit('@', 1)[-1] if '@' in sender_email else ''
    sender_display = from_header.split('<')[0].strip() or sender_email

    entry = {
        'posted_at': datetime.now(timezone.utc).isoformat(),
        'gmail_msg_id': gmail_msg_id,
        'slack_channel': slack_post.get('channel', ''),
        'slack_ts': slack_post.get('ts', ''),
        'sender_email': sender_email,
        'sender_domain': sender_domain,
        'sender_display': sender_display,
        'subject': subject,
        'category': classification.get('category', ''),
        'priority': classification.get('priority', ''),
        'actionable': bool(classification.get('actionable')),
        'summary': classification.get('summary', ''),
        'proposed_action': classification.get('proposed_action', ''),
        'reasoning': classification.get('reasoning', ''),
    }

    try:
        PROPOSAL_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        with PROPOSAL_LOG_PATH.open('a', encoding='utf-8') as f:
            f.write(json.dumps(entry) + '\n')
    except Exception as e:
        log.error('Failed to write proposal_log entry: %s', e)


def _get_proposal_patterns(sender_email: str, sender_domain: str, subject: str) -> list[str]:
    """Query mem0 for proposal_pattern facts matching this sender/subject.

    Returns top-N pattern strings, or empty list on any error (graceful degradation).
    """
    try:
        # Add harness package to path so we can import the shared mem0 client.
        _harness = Path(__file__).resolve().parent / 'packages' / 'harness'
        if _harness.exists() and str(_harness) not in sys.path:
            sys.path.insert(0, str(_harness))

        from deerflow.agents.memory.mem0_store import get_mem0

        m = get_mem0()
        query = f"email from {sender_email} ({sender_domain}) re: {subject}"
        result = m.search(
            query=query,
            filters={'user_id': PROPOSAL_PATTERNS_USER_ID},
            limit=PROPOSAL_PATTERN_INJECTION_LIMIT,
        )
        hits = result.get('results', []) if isinstance(result, dict) else (result or [])
        patterns = [h.get('memory', '').strip() for h in hits if h.get('memory')]
        return [p for p in patterns if p]
    except Exception as e:
        log.warning('mem0 pattern lookup failed (continuing without): %s', e)
        return []


# ---------------------------------------------------------------------------
# DeerFlow Dispatch
# ---------------------------------------------------------------------------

def dispatch_to_deerflow(classification: dict, source: str, sender: str,
                          subject: str, raw_context: str) -> bool:
    """Fire a DeerFlow agent run for an actionable message."""
    priority = classification.get('priority', 'medium')
    category = classification.get('category', 'inbound')
    summary = classification.get('summary', '')
    proposed_action = classification.get('proposed_action', '')

    prompt = f"""AUTONOMOUS TASK — {category} ({priority.upper()} PRIORITY)

Source: {source}
From: {sender}
Subject: {subject}
Received: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}

## Summary
{summary}

## Proposed action
{proposed_action}

## Raw message context
{raw_context[:3000]}

---

Handle this autonomously. Read the full message/thread first, gather any needed data,
then produce the output. Post a summary to Slack when done.
"""

    notification = (
        f"*{category}* ({priority} priority)\n"
        f"From: {sender}\n"
        f"Subject: {subject}\n"
        f"_{proposed_action[:140]}_\n\n"
        f"_Working on it..._"
    )

    try:
        with httpx.Client(timeout=httpx.Timeout(connect=10, read=30, write=30, pool=10)) as http:
            # Create thread
            resp = http.post(f'{LANGGRAPH_URL}/threads', json={})
            resp.raise_for_status()
            thread_id = resp.json()['thread_id']

            # Submit run (non-blocking — don't wait for completion)
            run_body = {
                'assistant_id': ASSISTANT_ID,
                'input': {'messages': [{'role': 'human', 'content': prompt}]},
                'config': {
                    'recursion_limit': 500,
                    'configurable': {
                        'thinking_enabled': True,
                        'is_plan_mode': False,
                        'subagent_enabled': False,
                        'thread_id': thread_id,
                    },
                },
            }
            resp = http.post(
                f'{LANGGRAPH_URL}/threads/{thread_id}/runs',
                json=run_body,
                timeout=30,
            )
            resp.raise_for_status()
            log.info('Dispatched to DeerFlow thread %s', thread_id)
            _post_slack(notification)
            return True
    except Exception as e:
        log.error('DeerFlow dispatch failed: %s', e)
        return False


# ---------------------------------------------------------------------------
# Slack notifications
# ---------------------------------------------------------------------------

def _post_slack(text: str, blocks: list | None = None) -> dict:
    """Post to Brian's DM. Returns {channel, ts} on success, empty dict on failure."""
    if not SLACK_BOT_TOKEN or not SLACK_OWNER_USER_ID:
        log.warning('Slack not configured')
        return {}
    try:
        from slack_sdk import WebClient
        client = WebClient(token=SLACK_BOT_TOKEN)
        dm = client.conversations_open(users=[SLACK_OWNER_USER_ID])
        channel_id = dm['channel']['id']
        resp = client.chat_postMessage(channel=channel_id, text=text, blocks=blocks)
        return {'channel': channel_id, 'ts': resp.get('ts', '')}
    except Exception as e:
        log.error('Slack post failed: %s', e)
        return {}


def _post_email_alert(sender: str, subject: str, snippet: str, 
                       classification: dict) -> None:
    action_type = classification.get('action_type', 'alert_only')
    priority = classification.get('priority', 'low')
    reasoning = classification.get('reasoning', '')

    if action_type == 'alert_only':
        tag = ''
    else:
        tag = f'  :robot_face: _{action_type.replace("_", " ").title()} — auto-handling ({priority})_\n'

    text = (
        f"*New email*\n"
        f"*{subject}*\n{tag}"
        f"From: {sender}\n"
        f">{snippet[:200]}"
    )
    if action_type == 'alert_only' and reasoning:
        text += f"\n_{reasoning}_"
    _post_slack(text)


def _post_action_proposal(from_header: str, subject: str, classification: dict,
                          gmail_msg_id: str = '') -> dict:
    """
    Post an actionable email to Slack as a *proposal* — no auto-dispatch.
    Brian replies in the thread to direct the agent.

    Format is intentionally tight: summary + one concrete proposed action.
    The full email body is NOT included — if Brian approves, the agent can
    fetch the body via the Gmail tool using the message id in the footer.
    """
    priority = classification.get('priority', 'medium')
    category = classification.get('category', '').strip() or 'inbound'
    summary = classification.get('summary', '').strip()
    proposed_action = classification.get('proposed_action', '').strip()
    reasoning = classification.get('reasoning', '').strip()

    pri_icon = {'high': ':rotating_light:', 'medium': ':bell:', 'low': ':envelope:'}.get(priority, ':envelope:')

    lines = [
        f"{pri_icon} *{category}* — _{priority}_",
        f"*From:* {from_header} — _{subject}_",
        "",
    ]
    if summary:
        lines.append(summary)
        lines.append("")
    if proposed_action:
        lines.append(f"*Proposed action:* {proposed_action}")
    else:
        lines.append("*Proposed action:* _(classifier could not name a concrete step — reply with direction)_")
    if reasoning and not proposed_action:
        lines.append(f"_Why surfaced:_ {reasoning}")
    lines.append("")
    lines.append("_Reply in thread to direct the agent (e.g. \"go\", \"draft the reply\", \"not now\")._")
    if gmail_msg_id:
        lines.append(f"_gmail_msg_id: {gmail_msg_id}_")

    return _post_slack("\n".join(lines))


# ---------------------------------------------------------------------------
# Gmail Pub/Sub Webhook
# ---------------------------------------------------------------------------

@app.post('/webhook/gmail')
async def gmail_webhook(request: Request, x_goog_channel_token: str = Header(default='')):
    """
    Receives Gmail push notifications via Google Cloud Pub/Sub.
    
    Setup:
      1. gcloud pubsub topics create gmail-push
      2. gcloud pubsub subscriptions create gmail-push-sub \
           --topic gmail-push \
           --push-endpoint https://YOUR-TUNNEL/webhook/gmail \
           --push-auth-service-account YOUR-SA@project.iam.gserviceaccount.com
      3. Call Gmail watch API to publish to the topic:
         POST https://gmail.googleapis.com/gmail/v1/users/me/watch
         {"topicName": "projects/YOUR-PROJECT/topics/gmail-push", "labelIds": ["INBOX"]}
    """
    # Verify shared token if configured
    if PUBSUB_VERIFICATION_TOKEN and x_goog_channel_token != PUBSUB_VERIFICATION_TOKEN:
        log.warning('Gmail webhook: invalid verification token')
        raise HTTPException(status_code=403, detail='Invalid token')

    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail='Invalid JSON')

    # Pub/Sub push format: {"message": {"data": "<base64>", "messageId": "..."}}
    message = body.get('message', {})
    encoded_data = message.get('data', '')
    if not encoded_data:
        return JSONResponse({'status': 'no_data'})

    try:
        decoded = base64.b64decode(encoded_data).decode('utf-8')
        notification = json.loads(decoded)
    except Exception as e:
        log.error('Failed to decode Pub/Sub message: %s', e)
        return JSONResponse({'status': 'decode_error'})

    # Notification contains historyId — we need to fetch new messages from Gmail
    history_id = notification.get('historyId')
    email_address = notification.get('emailAddress', MY_EMAIL)
    log.info('Gmail push: historyId=%s email=%s', history_id, email_address)

    # Fetch the new message(s) using Gmail API
    new_messages = _fetch_new_gmail_messages(history_id)
    if not new_messages:
        return JSONResponse({'status': 'no_new_messages'})

    for msg in new_messages:
        _handle_gmail_message(msg)

    return JSONResponse({'status': 'ok', 'processed': len(new_messages)})


def _fetch_new_gmail_messages(history_id: str | None) -> list[dict]:
    """Fetch new messages from Gmail using history API."""
    try:
        # Add parent dir to path for google_auth
        _shared = Path(__file__).resolve().parent / 'skills' / 'custom' / '_shared'
        if _shared.exists() and str(_shared) not in sys.path:
            sys.path.insert(0, str(_shared))

        from google_auth import get_credentials
        from googleapiclient.discovery import build

        creds = get_credentials(required=False)
        if not creds:
            log.error('Gmail: no credentials')
            return []

        service = build('gmail', 'v1', credentials=creds)

        # Load last known historyId from state
        state = _load_gmail_state()
        last_history_id = state.get('last_history_id')

        new_messages = []

        if last_history_id and history_id:
            # Use history API to get only new messages since last check
            try:
                history_resp = service.users().history().list(
                    userId='me',
                    startHistoryId=last_history_id,
                    historyTypes=['messageAdded'],
                    labelId='INBOX',
                ).execute()

                for record in history_resp.get('history', []):
                    for added in record.get('messagesAdded', []):
                        msg_id = added['message']['id']
                        if msg_id not in state.get('seen_ids', []):
                            try:
                                msg = service.users().messages().get(
                                    userId='me', id=msg_id, format='metadata',
                                    metadataHeaders=['From', 'Subject', 'Date', 'To'],
                                ).execute()
                                full = service.users().messages().get(
                                    userId='me', id=msg_id, format='full',
                                ).execute()
                                new_messages.append({'meta': msg, 'full': full})
                                state.setdefault('seen_ids', []).append(msg_id)
                            except Exception as e:
                                log.error('Failed to fetch message %s: %s', msg_id, e)
            except Exception as e:
                log.warning('History API failed (%s), falling back to recent search', e)

        # Update state
        if history_id:
            state['last_history_id'] = history_id
        _save_gmail_state(state)
        return new_messages

    except Exception as e:
        log.error('_fetch_new_gmail_messages failed: %s', e)
        return []


def _handle_gmail_message(msg: dict) -> None:
    """Classify and dispatch (or alert) for a single Gmail message."""
    import html
    import re

    meta = msg.get('meta', {})
    full = msg.get('full', {})

    headers = meta.get('payload', {}).get('headers', [])
    def _h(name):
        for h in headers:
            if h.get('name', '').lower() == name.lower():
                return h.get('value', '')
        return ''

    from_header = _h('From')
    subject = _h('Subject') or '(no subject)'
    date = _h('Date')
    snippet = html.unescape(meta.get('snippet', '')).strip()

    # Extract sender email
    m = re.search(r'<([^>]+)>', from_header)
    sender_email = m.group(1).lower() if m else from_header.strip().lower()

    # Skip emails Brian sent himself — his own outbound messages land in INBOX
    # when he replies in a thread, and he doesn't want a Slack notification
    # for his own sends.
    if sender_email == MY_EMAIL.lower() or sender_email.endswith('<' + MY_EMAIL.lower() + '>'):
        log.info('Skipping owner outbound (sender=%s)', sender_email)
        return

    # Noise check — skip automated senders
    noise_patterns = [
        'noreply@', 'no-reply@', 'notifications@', '@slack.com',
        '@github.com', '@linear.app', '@zoom.us', '@calendly.com',
        'mailer-daemon@', 'postmaster@',
    ]
    if any(p in sender_email for p in noise_patterns):
        log.info('Skipping noise: %s', sender_email)
        return

    # Extract body text for richer context
    body_text = _extract_gmail_body(full)

    # LLM classification
    classification = classify_with_llm(
        source='gmail',
        sender=from_header,
        subject=subject,
        body_preview=f"{snippet}\n\n{body_text[:2000]}",
        extra_context=f"Date: {date}",
    )

    # Non-actionable: silent — no Slack post, no dispatch.
    if not classification.get('actionable'):
        log.info('Non-actionable, no alert: %s | %s',
                 classification.get('action_type'), classification.get('reasoning', '')[:80])
        return

    # Actionable: post a PROPOSAL to Slack so Brian can approve/redirect before any dispatch.
    # No auto-dispatch — the agent runs only when Brian replies in the proposal thread.
    gmail_msg_id = full.get('id', '') or meta.get('id', '')
    slack_post = _post_action_proposal(from_header, subject, classification, gmail_msg_id)
    _log_proposal(classification, from_header, subject, gmail_msg_id, slack_post)


def _extract_gmail_body(full_msg: dict) -> str:
    """Extract plain text body from Gmail full message."""
    import base64 as b64
    import quopri

    def _decode_part(part: dict) -> str:
        data = part.get('body', {}).get('data', '')
        if not data:
            return ''
        try:
            decoded = b64.urlsafe_b64decode(data + '==').decode('utf-8', errors='replace')
            return decoded
        except Exception:
            return ''

    def _walk(payload: dict) -> str:
        mime = payload.get('mimeType', '')
        if mime == 'text/plain':
            return _decode_part(payload)
        if mime.startswith('multipart/'):
            for part in payload.get('parts', []):
                text = _walk(part)
                if text:
                    return text
        return ''

    return _walk(full_msg.get('payload', {}))


def _gmail_state_path() -> Path:
    return Path(__file__).resolve().parent / '.webhook_gmail_state.json'


def _load_gmail_state() -> dict:
    p = _gmail_state_path()
    if p.exists():
        try:
            return json.loads(p.read_text())
        except Exception:
            pass
    return {'last_history_id': None, 'seen_ids': []}


def _save_gmail_state(state: dict) -> None:
    # Keep seen_ids bounded
    if len(state.get('seen_ids', [])) > 1000:
        state['seen_ids'] = state['seen_ids'][-1000:]
    _gmail_state_path().write_text(json.dumps(state, indent=2))


# ---------------------------------------------------------------------------
# Slack Event API Webhook
# ---------------------------------------------------------------------------

@app.post('/webhook/slack')
async def slack_webhook(request: Request, x_slack_signature: str = Header(default=''),
                        x_slack_request_timestamp: str = Header(default='')):
    """
    Receives Slack Event API payloads.

    Setup in api.slack.com/apps → Event Subscriptions:
      Request URL: https://YOUR-TUNNEL/webhook/slack
      Subscribe to bot events: message.im, app_mention
    
    The bot needs to be in channels/DMs where you want it to listen.
    """
    body_bytes = await request.body()

    # Verify Slack signature
    if SLACK_SIGNING_SECRET:
        if not _verify_slack_signature(body_bytes, x_slack_signature, x_slack_request_timestamp):
            log.warning('Slack webhook: signature verification failed')
            raise HTTPException(status_code=403, detail='Invalid signature')

    try:
        body = json.loads(body_bytes)
    except Exception:
        raise HTTPException(status_code=400, detail='Invalid JSON')

    # Slack URL verification challenge (one-time setup)
    if body.get('type') == 'url_verification':
        return JSONResponse({'challenge': body.get('challenge', '')})

    event = body.get('event', {})
    event_type = event.get('type', '')

    # Handle DMs and app mentions
    if event_type in ('message', 'app_mention'):
        _handle_slack_event(event, body)

    # Always return 200 quickly — Slack retries if we're slow
    return JSONResponse({'status': 'ok'})


def _verify_slack_signature(body: bytes, signature: str, timestamp: str) -> bool:
    if not SLACK_SIGNING_SECRET or not signature or not timestamp:
        return True  # Skip verification if not configured

    # Reject stale requests (> 5 min old)
    try:
        if abs(time.time() - int(timestamp)) > 300:
            return False
    except ValueError:
        return False

    sig_basestring = f'v0:{timestamp}:{body.decode("utf-8")}'
    computed = 'v0=' + hmac.new(
        SLACK_SIGNING_SECRET.encode(),
        sig_basestring.encode(),
        hashlib.sha256,
    ).hexdigest()
    return hmac.compare_digest(computed, signature)


def _handle_slack_event(event: dict, body: dict) -> None:
    """Classify and handle an inbound Slack message."""
    text = event.get('text', '')
    user_id = event.get('user', '')
    channel = event.get('channel', '')
    ts = event.get('ts', '')
    subtype = event.get('subtype', '')

    # Skip bot messages and message edits
    if subtype in ('bot_message', 'message_changed', 'message_deleted'):
        return
    if event.get('bot_id'):
        return

    # Skip Brian's own messages
    if user_id == SLACK_OWNER_USER_ID:
        return

    if not text.strip():
        return

    # Resolve user name if possible
    sender_name = _resolve_slack_user(user_id)
    log.info('Slack event: user=%s channel=%s text=%.80s', sender_name, channel, text)

    # LLM classification
    classification = classify_with_llm(
        source='slack',
        sender=sender_name,
        subject=f'Slack message in {channel}',
        body_preview=text,
        extra_context=f'Channel: {channel}, Timestamp: {ts}',
    )

    log.info('Slack classification: %s | actionable=%s',
             classification.get('action_type'), classification.get('actionable'))

    # Dispatch if actionable
    if classification.get('actionable'):
        raw_context = f"From: {sender_name}\nChannel: {channel}\n\n{text}"
        dispatch_to_deerflow(classification, 'Slack', sender_name,
                             f'Slack: {text[:60]}...', raw_context)
    else:
        # For non-actionable Slack messages, just log (Slack itself handles the notification)
        log.info('Slack message not actionable: %s', classification.get('reasoning', ''))


def _resolve_slack_user(user_id: str) -> str:
    """Resolve Slack user ID to display name."""
    if not SLACK_BOT_TOKEN or not user_id:
        return user_id
    try:
        from slack_sdk import WebClient
        client = WebClient(token=SLACK_BOT_TOKEN)
        resp = client.users_info(user=user_id)
        profile = resp['user'].get('profile', {})
        return profile.get('real_name') or profile.get('display_name') or user_id
    except Exception:
        return user_id


# ---------------------------------------------------------------------------
# Health check
# ---------------------------------------------------------------------------

@app.get('/health')
async def health():
    return {
        'status': 'ok',
        'langgraph_url': LANGGRAPH_URL,
        'anthropic_configured': bool(ANTHROPIC_API_KEY),
        'slack_configured': bool(SLACK_BOT_TOKEN),
        'timestamp': datetime.now(timezone.utc).isoformat(),
    }


@app.get('/')
async def root():
    return {'service': 'DeerFlow Webhook Receiver', 'routes': ['/webhook/gmail', '/webhook/slack', '/health']}


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == '__main__':
    import uvicorn
    port = int(os.environ.get('WEBHOOK_PORT', '8080'))
    log.info('Starting webhook receiver on port %d', port)
    uvicorn.run(app, host='0.0.0.0', port=port)
