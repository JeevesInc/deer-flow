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

## What IS actionable (Brian needs an agent to handle this)
- A lender requesting data, a portfolio tape, a report, or documents → DISPATCH as "diligence"
- A counterparty asking a specific question that requires pulling data → DISPATCH as "diligence"
- A lender following up on an open item from a checklist → DISPATCH as "diligence"
- A term sheet, amendment, or legal document sent FOR BRIAN'S REVIEW → DISPATCH as "legal_review"
- A meeting request or scheduling ask from a counterparty → DISPATCH as "scheduling"
- An urgent item with a deadline mentioned → DISPATCH with high priority

## What is NOT actionable (alert Brian but don't dispatch an agent)
- W&C, Goodwin, or Akin Gump sending documents TO a counterparty on Jeeves' behalf (outbound legal)
- Automated notifications, receipts, calendar invites, SaaS alerts
- Internal Jeeves emails that aren't from C-suite
- Newsletters, marketing, LinkedIn, recruiter emails
- NB / any counsel sending revised docs back to a counterparty (not requesting anything from Brian)
- Jeeves' own counsel updating each other (not requesting anything from Brian)

## Decision framework
Ask yourself: "Does Brian need to DO something, or does someone need to DO something for Brian?"
- If Brian (or an agent on his behalf) needs to produce something → DISPATCH
- If it's just FYI / outbound / automated → ALERT ONLY
- If genuinely ambiguous → lean toward ALERT ONLY (don't waste agent capacity)
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
      "action_type": "diligence" | "legal_review" | "scheduling" | "alert_only",
      "priority": "high" | "medium" | "low",
      "task_description": str,   # what the agent should do (if actionable)
      "reasoning": str,           # brief explanation
    }
    """
    if not ANTHROPIC_API_KEY:
        log.error('No ANTHROPIC_API_KEY — cannot classify, defaulting to alert_only')
        return _default_classification('No API key configured')

    client = Anthropic(api_key=ANTHROPIC_API_KEY)

    prompt = f"""You are a classifier for Brian Mauck's inbound communications.

{ANALYST_CONTEXT}

---

## Message to classify

Source: {source}
Sender: {sender}
Subject: {subject}
Preview: {body_preview[:1000]}
{f"Additional context: {extra_context}" if extra_context else ""}

---

Classify this message and respond with ONLY valid JSON in this exact format:
{{
  "actionable": true or false,
  "action_type": "diligence" | "legal_review" | "scheduling" | "alert_only",
  "priority": "high" | "medium" | "low",
  "task_description": "specific description of what the DeerFlow agent should do (leave empty string if not actionable)",
  "reasoning": "1-2 sentence explanation"
}}

Be decisive. If it's outbound legal work (counsel sending docs TO a counterparty), set actionable=false.
If a lender is requesting something from Brian, set actionable=true."""

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
        log.info('LLM classification: %s | priority=%s | %s',
                 result.get('action_type'), result.get('priority'), result.get('reasoning', '')[:80])
        return result
    except Exception as e:
        log.error('LLM classification failed: %s', e)
        return _default_classification(str(e))


def _default_classification(reason: str) -> dict:
    return {
        'actionable': False,
        'action_type': 'alert_only',
        'priority': 'low',
        'task_description': '',
        'reasoning': f'Classification failed: {reason}',
    }


# ---------------------------------------------------------------------------
# DeerFlow Dispatch
# ---------------------------------------------------------------------------

def dispatch_to_deerflow(classification: dict, source: str, sender: str, 
                          subject: str, raw_context: str) -> bool:
    """Fire a DeerFlow agent run for an actionable message."""
    action_type = classification.get('action_type', 'general')
    priority = classification.get('priority', 'medium')
    task_desc = classification.get('task_description', '')

    prompt = f"""AUTONOMOUS TASK — {action_type.upper()} ({priority.upper()} PRIORITY)

Source: {source}
From: {sender}
Subject: {subject}
Received: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}

## What the classifier determined
{task_desc}

## Raw message context
{raw_context[:3000]}

---

Handle this autonomously. Read the full message/thread first, gather any needed data, 
then produce the output. Post a summary to Slack when done.
"""

    notification = (
        f"*{action_type.replace('_', ' ').title()} detected* ({priority} priority)\n"
        f"From: {sender}\n"
        f"Subject: {subject}\n"
        f"_{task_desc[:120]}_\n\n"
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

def _post_slack(text: str, blocks: list | None = None) -> None:
    if not SLACK_BOT_TOKEN or not SLACK_OWNER_USER_ID:
        log.warning('Slack not configured')
        return
    try:
        from slack_sdk import WebClient
        client = WebClient(token=SLACK_BOT_TOKEN)
        dm = client.conversations_open(users=[SLACK_OWNER_USER_ID])
        channel_id = dm['channel']['id']
        client.chat_postMessage(channel=channel_id, text=text, blocks=blocks)
    except Exception as e:
        log.error('Slack post failed: %s', e)


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


def _post_action_proposal(from_header: str, subject: str, date: str,
                          snippet: str, body_text: str, classification: dict,
                          raw_context: str) -> None:
    """
    Post an actionable email to Slack as a *proposal* — no auto-dispatch.
    Brian replies in the thread to direct the agent (or just ignores it).
    The full email body is included so the agent has context if Brian replies "go" etc.
    """
    action_type = classification.get('action_type', 'general')
    priority = classification.get('priority', 'medium')
    task_desc = classification.get('task_description', '').strip()
    reasoning = classification.get('reasoning', '').strip()

    pri_icon = {'high': ':rotating_light:', 'medium': ':bell:', 'low': ':envelope:'}.get(priority, ':envelope:')
    body_clip = (body_text or snippet)[:1500].strip()

    text = (
        f"{pri_icon} *Actionable email* — _{action_type.replace('_', ' ')} / {priority}_\n"
        f"*From:* {from_header}\n"
        f"*Subject:* {subject}\n"
        f"*Date:* {date}\n\n"
        f"*Proposed action:*\n>{task_desc or '(classifier did not propose a specific action)'}\n"
    )
    if reasoning:
        text += f"_Why:_ {reasoning}\n"
    text += (
        f"\n*Email body:*\n```\n{body_clip}\n```\n"
        f"_Reply in this thread with what you want done — e.g. \"go\", \"draft a reply\", \"pull the data first\"._"
    )
    _post_slack(text)


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
    raw_context = (
        f"From: {from_header}\nSubject: {subject}\nDate: {date}\n\n"
        f"{snippet}\n\n{body_text[:3000]}"
    )
    _post_action_proposal(from_header, subject, date, snippet, body_text, classification, raw_context)


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
