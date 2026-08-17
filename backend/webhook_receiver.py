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

import asyncio
import base64
import hashlib
import hmac
import json
import logging
import os
import re
import sys
import threading
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
GATEWAY_URL = os.environ.get('GATEWAY_URL', 'http://localhost:8001')
ASSISTANT_ID = 'lead_agent'
ANTHROPIC_API_KEY = os.environ.get('ANTHROPIC_API_KEY', '')
SLACK_BOT_TOKEN = os.environ.get('SLACK_BOT_TOKEN', '')
# Owner id via the canonical identity module (P3-5). The old inline default
# here was U09PQTZ5DHC — the BOT's own id — the documented recurring bug.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'skills' / 'custom' / '_shared'))
from identity import owner_slack_id  # noqa: E402
from llm import text_from_response  # noqa: E402 — ThinkingBlock-safe text extraction
SLACK_OWNER_USER_ID = owner_slack_id()
# Approval queue: when set, actionable-email proposals post to this channel
# (e.g. #analyst-queue) instead of DMing the owner. Falls back to the owner DM
# when unset, so the pipeline is unchanged until the channel is configured.
ANALYST_QUEUE_CHANNEL_ID = os.environ.get('ANALYST_QUEUE_CHANNEL_ID', '')
SLACK_SIGNING_SECRET = os.environ.get('SLACK_SIGNING_SECRET', '')
PUBSUB_VERIFICATION_TOKEN = os.environ.get('PUBSUB_VERIFICATION_TOKEN', '')
MY_EMAIL = os.environ.get('GOOGLE_CALENDAR_EMAIL', 'brian.mauck@tryjeeves.com')
CLASSIFIER_MODEL = 'claude-opus-5'  # Opus 5 everywhere (2026-07-24)
DRAFTER_MODEL = 'claude-opus-5'     # Opus 5 everywhere (2026-07-24)
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

# Strong refs for fire-and-forget tasks — asyncio only keeps a weak ref, so an
# unreferenced task can be garbage-collected mid-flight (P3-6 / Chan MED).
_bg_tasks: set = set()


def _spawn_bg(coro) -> None:
    task = asyncio.create_task(coro)
    _bg_tasks.add(task)
    task.add_done_callback(_bg_tasks.discard)


@app.on_event('startup')
async def _on_startup() -> None:
    # At-least-once tail (P3-6): catch up on anything that arrived while the
    # receiver was down (a restart mid-push used to lose the notification —
    # Pub/Sub got its 200 before processing), then keep a periodic sweep
    # running for missed pushes + failed-handling retries.
    threading.Thread(target=_gmail_sweep_loop, daemon=True,
                     name='gmail-sweep').start()
    _spawn_bg(asyncio.to_thread(_process_gmail_notification, None))

# Serializes Gmail state reads/writes across concurrent Pub/Sub pushes.
# `_process_gmail_notification` runs in `asyncio.to_thread` so two near-
# simultaneous pushes (message-arrived + IMPORTANT-label-added) end up
# in different threads from the default pool. Without this lock they
# both read the same `last_history_id`, both fetch the same messageAdded
# record, and both classify + post to Slack.
_gmail_state_lock = threading.Lock()

# ---------------------------------------------------------------------------
# ANALYST CONTEXT — injected into every LLM classification call
# This is what makes classification smart vs keyword matching.
# Stable framework lives here; the VOLATILE business state (active deals,
# counterparty domains) lives in deer-flow/cap_markets_config.json and
# hot-reloads on edit — no restart needed (P3-4).
# ---------------------------------------------------------------------------
_ANALYST_FRAMEWORK_HEAD = """
You are classifying inbound messages for Brian Mauck, Head of Capital Markets at Jeeves Financial Technology.

## Who Brian is
Brian runs capital markets at Jeeves — a B2B fintech providing corporate cards and working capital
to businesses across Latin America and the US. He manages lender relationships, credit facilities,
borrowing bases, and portfolio analytics. He works directly with the CFO (Alex Melikian).
"""

_ANALYST_FRAMEWORK_TAIL = """
## Addressee check (do this BEFORE applying the actionable rules below)
Brian is often CC'd or on a reply-all thread where he is not the one being asked.
Before flagging anything actionable for Brian, check who the message is literally
addressed to: the greeting line ("Hi X and team") and/or the To: field. Brian's
address is brian.mauck@tryjeeves.com — if he appears only in Cc: (or not at all),
he is on the thread for visibility, not as the addressee.
- If a named person other than Brian (outside counsel, another Jeeves exec, a
  counterparty contact) is the addressee, and Brian is only CC'd/on the thread for
  visibility -> NOT actionable, even if the topic is in his domain and even if the
  sender is C-suite. Example: Alex (CFO) emails "Hi Eliza and team" asking Goodwin's
  counsel for a status update, with Brian CC'd -> not actionable for Brian.
- Only treat a message as addressed to Brian if he is named/greeted directly, the
  reply is responding to something Brian himself sent, or no other Jeeves/counsel
  person is a more direct addressee for the ask.

## What IS actionable (worth a Slack card) — only after the addressee check above passes
- Lender/counterparty asks for data, a tape, a report, or documents
- Counterparty asks a specific question that requires Brian's input or pulling data
- Lender follows up on an open checklist item
- Term sheet, amendment, redline, or legal doc sent for Brian's review
- A scheduling ask that needs a written answer (proposed times, "when works for you?")
- C-suite (Alex Melikian, CFO) asks Brian directly for something
- Anything with an explicit deadline that names Brian as the owner

## What is NOT actionable (skip — no Slack post)
- Calendar/Zoom invites, invite updates, and meeting confirmations — Google Calendar
  already surfaces these; a card saying "accept the invite" is noise. Only actionable
  if the message also asks an open question that needs a written reply.
- W&C, Goodwin, or Akin Gump sending docs TO a counterparty on Jeeves' behalf (outbound legal)
- External counsel updating each other without asking Brian for anything
- Automated notifications, receipts, SaaS alerts
- Internal Jeeves emails that aren't from leadership
- Newsletters, marketing, LinkedIn, recruiter emails

## Decision framework
Ask: "Is there something specific here Brian would want flagged, or that an agent
can do on his behalf?"
- If yes → actionable
- If it's just FYI / outbound / automated → not actionable
- If ambiguous → not actionable (avoid noise)

## The proposed action — THE AGENT executes it, not Brian
The proposed_action is a task for Brian's AI agent (which can draft/send email,
pull Redshift data and reports, search Slack/Gmail/Drive, manage the calendar,
and prep briefing docs). It is NOT a to-do item for Brian.

HARD RULE: never restate the email's ask back at Brian. If the email says
"sync with Shalom this morning," the action is NOT "Sync with Shalom" — Brian
read the email; telling him to do his job adds nothing. Ask instead: "what can
the AGENT produce or send that moves this forward?"

- Good actions (agent-executable, verb-led, one step):
    "Pull the May MX SOFOM borrowing base and draft a reply with the file attached"
    "Draft a reply to Jay confirming 3:30pm ET and answering the six business points"
    "Prep a one-page brief on the three Vista side-letter issues with our prior positions"
    "Draft a forward to Goodwin asking for their view on the enforcement waiver by Friday"
    "Search Gmail/Slack for the CIM wind-down plan status and draft a summary reply"
- If every next step requires Brian personally (attend a meeting, make a judgment
  call, talk to a colleague), set proposed_action to "" — the card is then a pure
  heads-up, which is fine. An empty action beats a parroted one.
- Never produce vague actions: "Respond to the email", "Handle this request", "Follow up"

## Priority rubric (most cards are NOT high)
- high: explicit same-day/next-day deadline, a lender is blocked waiting on Jeeves,
  or the CFO marked it urgent
- medium: needs a response this week; normal deal-flow asks
- low: everything else worth surfacing
"""


def _analyst_context() -> str:
    """Framework + hot-reloaded business state from cap_markets_config.json.
    Fails open to the framework alone — a bad config edit must never take
    the classifier down."""
    deals = ''
    try:
        from cap_markets_config import analyst_deals_section  # _shared on sys.path
        deals = analyst_deals_section()
    except Exception as e:
        log.warning('cap_markets_config unavailable — classifying without deal context: %s', e)
    middle = f"\n{deals}\n" if deals else "\n"
    return f"{_ANALYST_FRAMEWORK_HEAD}{middle}{_ANALYST_FRAMEWORK_TAIL}"


# ---------------------------------------------------------------------------
# LLM Classifier
# ---------------------------------------------------------------------------

def classify_with_llm(source: str, sender: str, subject: str, body_preview: str,
                       extra_context: str = '', to: str = '', cc: str = '') -> dict:
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

    # The addressee check in the framework depends on the literal To:/Cc:
    # fields being present — without them the model guesses from the greeting
    # line and misattributes who is being asked (the "Alex asking Brian" when
    # the email was addressed to Aaron failure).
    recipient_lines = ''
    if to:
        recipient_lines += f"To: {to}\n"
    if cc:
        recipient_lines += f"Cc: {cc}\n"

    prompt = f"""You are a classifier for Brian Mauck's inbound communications.

{_analyst_context()}{patterns_block}

---

## Message to classify

Source: {source}
Sender: {sender}
{recipient_lines}Subject: {subject}
Body: {body_preview[:2000]}
{f"Additional context: {extra_context}" if extra_context else ""}

---

Respond with ONLY valid JSON in this exact format:
{{
  "actionable": true or false,
  "priority": "high" | "medium" | "low",
  "category": "2-5 word free-form label naming the counterparty + nature (e.g. 'BBVA data request', 'NB redline', 'CFO ask', 'CIM amendment')",
  "summary": "1-2 sentences describing what the email actually says — the substance, not 'an email about X'. Name the counterparty, what they sent or asked, and any deadline. Do not quote the email verbatim.",
  "proposed_action": "ONE concrete verb-led step THE AGENT can execute on Brian's behalf (draft/send a reply, pull data, prep a brief, search mail/Slack). Empty string if not actionable OR if every next step requires Brian personally — never restate the email's ask back at Brian. Re-read the action guidance above before writing this.",
  "reasoning": "1 sentence: why this classification"
}}

Be decisive. Outbound legal work from Jeeves' counsel and calendar/Zoom invites → actionable=false. Inbound asks from lenders/counterparties/leadership → actionable=true, with a proposed_action only when the agent can genuinely execute it."""

    try:
        response = client.messages.create(
            model=CLASSIFIER_MODEL,
            max_tokens=512,
            messages=[{'role': 'user', 'content': prompt}],
        )
        text = text_from_response(response).strip()
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


# --- Peer-aware drafting (PRD §7 H1) ---------------------------------------
# The Honcho peer card knows how this correspondent prefers to be spoken to
# and what's open between them and Brian; condition the draft on it.

_pct_module = None  # honcho-peers module, or False after a failed load


def _peer_card_module():
    global _pct_module
    if _pct_module is not None:
        return _pct_module or None
    try:
        import importlib.util
        path = Path(__file__).resolve().parent.parent / 'skills' / 'custom' / 'honcho-peers' / 'peer_card_tool.py'
        spec = importlib.util.spec_from_file_location('peer_card_tool_webhook', path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        _pct_module = mod
    except Exception as e:
        log.warning('peer_card_tool unavailable — peer-aware drafting off: %s', e)
        _pct_module = False
    return _pct_module or None


def _extract_email(sender: str) -> str:
    """'Name <a@b.com>' -> 'a@b.com'; bare addresses pass through."""
    m = re.search(r'<([^<>\s]+@[^<>\s]+)>', sender or '')
    if m:
        return m.group(1).strip().lower()
    s = (sender or '').strip().lower()
    return s if ('@' in s and ' ' not in s) else ''


def _peer_context_for(sender: str) -> str:
    """Drafting context for this correspondent, or '' (always fail-open)."""
    email = _extract_email(sender)
    if not email:
        return ''
    mod = _peer_card_module()
    if not mod:
        return ''
    try:
        return mod.peer_context(email) or ''
    except Exception as e:
        log.warning('peer_context failed for %s: %s', email, e)
        return ''


def draft_reply(sender: str, subject: str, body: str, summary: str,
                proposed_action: str, source: str = 'email') -> str:
    """Draft a reply in Brian's voice for an inbound message that warrants one.

    Returns the draft body text, or '' if a written reply isn't the right next
    step (e.g. the action is to pull data, review internally, or schedule) or on
    any error. Used for both inbound email and inbound Slack so the queue card
    can show a ready-to-send draft Brian approves/edits inline.
    """
    if not ANTHROPIC_API_KEY:
        return ''
    channel_hint = (
        "This is a SLACK message — keep the reply short, conversational, and Slack-appropriate "
        "(no email greeting/sign-off)." if source == 'slack' else
        "This is an EMAIL — write just the reply body: no subject line, no signature block."
    )
    peer_ctx = _peer_context_for(sender)
    peer_block = (
        f"\n## What we know about this correspondent (from relationship history)\n"
        f"{peer_ctx}\n"
        f"Match their preferred style and, where natural, acknowledge open items — "
        f"but do NOT invent specifics beyond the message and this context.\n"
    ) if peer_ctx else ''
    try:
        client = Anthropic(api_key=ANTHROPIC_API_KEY)
        prompt = f"""{_analyst_context()}{peer_block}

You are drafting a reply ON BEHALF OF Brian Mauck to the message below.

Only draft a reply if a written response FROM BRIAN is genuinely the right next step
(i.e. the proposed action is to reply / respond / confirm / answer / acknowledge). If the
right next step is something else — pull data, review a document internally, schedule,
forward to counsel, or nothing — respond with exactly: NONE

Write in Brian's voice: concise, professional, warm but direct, no filler. {channel_hint}
Do NOT invent facts, numbers, dates, or commitments not supported by the message/context.
Where you'd need info Brian hasn't given, leave a clearly bracketed placeholder like
[confirm Aforo amount]. Keep it tight.

## Message being replied to
From: {sender}
Subject: {subject}
Body:
{(body or '')[:3000]}

## What Brian intends to do
{proposed_action}

Respond with ONLY the draft reply text, or exactly NONE."""
        resp = client.messages.create(
            model=DRAFTER_MODEL,
            max_tokens=700,
            messages=[{'role': 'user', 'content': prompt}],
        )
        text = text_from_response(resp).strip()
        if not text or text.upper().startswith('NONE') or len(text) < 5:
            return ''
        return text
    except Exception as e:
        log.warning('draft_reply failed: %s', e)
        return ''


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
        'draft_reply': classification.get('draft_reply', ''),
        'reasoning': classification.get('reasoning', ''),
    }

    try:
        PROPOSAL_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        with PROPOSAL_LOG_PATH.open('a', encoding='utf-8') as f:
            f.write(json.dumps(entry) + '\n')
    except Exception as e:
        log.error('Failed to write proposal_log entry: %s', e)


# Escalation state for the pattern lookup. A lookup that fails on every call is
# an outage, not a degradation — the pre-2026-07-08 version warned 501 times in
# a row (embedded-Qdrant lock, then a poisoned import) and nobody noticed until
# proposal quality collapsed. After PATTERN_LOOKUP_ALERT_THRESHOLD consecutive
# failures we log.error and DM the owner (re-alerting at most once per day).
PATTERN_LOOKUP_ALERT_THRESHOLD = 10
PATTERN_LOOKUP_ALERT_INTERVAL = 24 * 3600  # seconds between owner alerts
_pattern_lookup_failures = 0
_pattern_lookup_last_alert = 0.0


def _pattern_lookup_failed(reason: str) -> None:
    global _pattern_lookup_failures, _pattern_lookup_last_alert
    _pattern_lookup_failures += 1
    if _pattern_lookup_failures < PATTERN_LOOKUP_ALERT_THRESHOLD:
        log.warning('proposal pattern lookup failed (%d consecutive): %s',
                    _pattern_lookup_failures, reason)
        return
    log.error('proposal pattern lookup failed %d times in a row — feedback loop '
              'is severed, classifier running without learned patterns: %s',
              _pattern_lookup_failures, reason)
    now = time.time()
    if now - _pattern_lookup_last_alert >= PATTERN_LOOKUP_ALERT_INTERVAL:
        _pattern_lookup_last_alert = now
        _post_slack(
            f":warning: *Proposal feedback loop severed* — the classifier's learned-pattern "
            f"lookup has failed {_pattern_lookup_failures} times in a row "
            f"(latest: {reason[:200]}). Proposals are running without your past feedback. "
            f"Check the gateway `/api/memory/mem0` endpoint."
        )


def _get_proposal_patterns(sender_email: str, sender_domain: str, subject: str) -> list[str]:
    """Fetch learned proposal patterns for this sender/subject via the Gateway.

    Goes over HTTP because mem0's embedded Qdrant allows exactly ONE process —
    the Gateway holds that lock, so importing mem0 here can never work (that
    exact mistake silently disabled the feedback loop for weeks).

    Returns top-N pattern strings; empty list on error, but errors escalate
    after PATTERN_LOOKUP_ALERT_THRESHOLD consecutive failures.
    """
    global _pattern_lookup_failures
    query = f"email from {sender_email} ({sender_domain}) re: {subject}"
    try:
        with httpx.Client(timeout=httpx.Timeout(10)) as http:
            resp = http.get(
                f'{GATEWAY_URL}/api/memory/mem0',
                params={
                    'q': query,
                    'limit': PROPOSAL_PATTERN_INJECTION_LIMIT,
                    'user_id': PROPOSAL_PATTERNS_USER_ID,
                },
            )
            resp.raise_for_status()
            data = resp.json()
        if data.get('error'):
            _pattern_lookup_failed(str(data['error']))
            return []
        hits = data.get('results', []) or []
        patterns = [h.get('memory', '').strip() for h in hits if isinstance(h, dict) and h.get('memory')]
        _pattern_lookup_failures = 0
        return [p for p in patterns if p]
    except Exception as e:
        _pattern_lookup_failed(str(e))
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

def _post_slack(text: str, blocks: list | None = None, channel: str | None = None,
                thread_ts: str | None = None) -> dict:
    """Post a Slack message. Returns {channel, ts} on success, empty dict on failure.

    If ``channel`` is given (e.g. the approval queue channel), posts there.
    Otherwise opens and posts to the owner's DM (default for notifications).
    ``thread_ts`` nests the message under an existing card.
    """
    if not SLACK_BOT_TOKEN:
        log.warning('Slack not configured')
        return {}
    try:
        from slack_sdk import WebClient
        client = WebClient(token=SLACK_BOT_TOKEN)
        if channel:
            channel_id = channel
        else:
            if not SLACK_OWNER_USER_ID:
                log.warning('Slack owner not configured')
                return {}
            dm = client.conversations_open(users=[SLACK_OWNER_USER_ID])
            channel_id = dm['channel']['id']
        resp = client.chat_postMessage(channel=channel_id, text=text, blocks=blocks,
                                       thread_ts=thread_ts)
        return {'channel': channel_id, 'ts': resp.get('ts', '')}
    except Exception as e:
        log.error('Slack post failed: %s', e)
        return {}


# ---------------------------------------------------------------------------
# Proposal thread dedup — one card per Gmail thread
# ---------------------------------------------------------------------------
# Every reply in an email thread used to spawn a fresh top-level card ("Vista -
# Issues List" produced 3 near-identical cards in one day). Follow-ups within
# the freshness window now nest under the original card instead.

PROPOSAL_THREAD_FRESH_SECONDS = 48 * 3600


def _proposal_threads_path() -> Path:
    return Path(__file__).resolve().parent / '.deer-flow' / 'proposal_threads.json'


def _load_proposal_threads() -> dict:
    try:
        return json.loads(_proposal_threads_path().read_text(encoding='utf-8'))
    except Exception:
        return {}


def _get_proposal_thread(gmail_thread_id: str) -> dict:
    """Return {'channel','ts','posted_at'} for a fresh existing card, else {}."""
    if not gmail_thread_id:
        return {}
    entry = _load_proposal_threads().get(gmail_thread_id)
    if not entry or not entry.get('ts'):
        return {}
    try:
        posted = datetime.fromisoformat(entry.get('posted_at', ''))
        if (datetime.now(timezone.utc) - posted).total_seconds() > PROPOSAL_THREAD_FRESH_SECONDS:
            return {}
    except Exception:
        return {}
    return entry


def _record_proposal_thread(gmail_thread_id: str, slack_post: dict) -> None:
    if not gmail_thread_id or not slack_post.get('ts'):
        return
    try:
        threads = _load_proposal_threads()
        now = datetime.now(timezone.utc)
        threads[gmail_thread_id] = {
            'channel': slack_post.get('channel', ''),
            'ts': slack_post['ts'],
            'posted_at': now.isoformat(),
        }
        # Prune stale entries so the file stays small.
        def _fresh(e):
            try:
                return (now - datetime.fromisoformat(e.get('posted_at', ''))).total_seconds() <= PROPOSAL_THREAD_FRESH_SECONDS
            except Exception:
                return False
        threads = {k: v for k, v in threads.items() if _fresh(v)}
        path = _proposal_threads_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(threads, indent=1), encoding='utf-8')
    except Exception as e:
        log.warning('Failed to record proposal thread mapping: %s', e)


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
                          gmail_msg_id: str = '', thread_ts: str = '',
                          thread_channel: str = '') -> dict:
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
        lines.append("*Heads-up only* — nothing here the agent can usefully do; reply in thread if you want something.")
    if reasoning and not proposed_action:
        lines.append(f"_Why surfaced:_ {reasoning}")

    draft = classification.get('draft_reply', '').strip()
    if draft:
        lines.append("")
        lines.append("*Draft reply* (review before it goes out):")
        lines.append("```")
        lines.append(draft)
        lines.append("```")

    lines.append("")
    if draft:
        lines.append("_Reply in thread to direct me — *send it* to send as-is, or paste your edits. (:+1:/:-1: also work once enabled.)_")
    else:
        lines.append("_Reply in thread to direct me (e.g. \"go\", \"draft a reply\", \"not now\")._")
    if gmail_msg_id:
        lines.append(f"_gmail_msg_id: {gmail_msg_id}_")

    if thread_ts:
        # Follow-up on an email thread that already has a card — nest under it.
        lines.insert(0, ":thread: *Update on this thread*")
        return _post_slack("\n".join(lines),
                           channel=thread_channel or ANALYST_QUEUE_CHANNEL_ID or None,
                           thread_ts=thread_ts)
    return _post_slack("\n".join(lines), channel=ANALYST_QUEUE_CHANNEL_ID or None)


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

    # Hand off all blocking work (Gmail API, mem0/Qdrant lookups, classifier
    # LLM call, LangGraph dispatch) to a background task so the event loop
    # stays free for /health probes. Pub/Sub only needs a 200 within ~10s and
    # doesn't care about the processed count. Holding the loop here was the
    # root cause of the /health flap that the supervisor kept restarting.
    _spawn_bg(asyncio.to_thread(_process_gmail_notification, history_id))
    return JSONResponse({'status': 'accepted'})


def _process_gmail_notification(history_id: str | None) -> None:
    """At-least-once processing (P3-6): a message is marked seen ONLY after
    its handler succeeds; failures go to a bounded pending-retry queue that
    the sweep loop drains. history_id=None means catch-up (boot / sweep)."""
    try:
        new_messages = _fetch_new_gmail_messages(history_id)
        for msg in new_messages or []:
            msg_id = (msg.get('meta') or {}).get('id') or (msg.get('full') or {}).get('id') or ''
            try:
                _handle_gmail_message(msg)
                if msg_id:
                    _mark_gmail_seen(msg_id)
            except Exception:
                log.exception('Gmail message handler failed for %s — queued for retry', msg_id)
                if msg_id:
                    _add_gmail_pending_retry(msg_id)
    except Exception:
        log.exception('Gmail notification processing failed')


def _get_gmail_service():
    # Add the shared dir for google_auth
    _shared = Path(__file__).resolve().parent.parent / 'skills' / 'custom' / '_shared'
    if _shared.exists() and str(_shared) not in sys.path:
        sys.path.insert(0, str(_shared))
    from google_auth import get_credentials
    from googleapiclient.discovery import build
    creds = get_credentials(required=False)
    if not creds:
        return None
    return build('gmail', 'v1', credentials=creds)


def _list_new_message_ids(service, start_history_id: str) -> tuple[list[str], str | None]:
    """All INBOX messageAdded ids since start_history_id, following
    nextPageToken (the old single-page call silently truncated bursts).
    Returns (ids, latest_history_id_from_response). Raises on API error."""
    ids: list[str] = []
    latest_hid = None
    page_token = None
    while True:
        kwargs = dict(userId='me', startHistoryId=start_history_id,
                      historyTypes=['messageAdded'], labelId='INBOX')
        if page_token:
            kwargs['pageToken'] = page_token
        resp = service.users().history().list(**kwargs).execute()
        latest_hid = resp.get('historyId') or latest_hid
        for record in resp.get('history', []):
            for added in record.get('messagesAdded', []):
                mid = added.get('message', {}).get('id')
                if mid and mid not in ids:
                    ids.append(mid)
        page_token = resp.get('nextPageToken')
        if not page_token:
            return ids, latest_hid


def _fetch_gmail_message(service, msg_id: str) -> dict:
    meta = service.users().messages().get(
        userId='me', id=msg_id, format='metadata',
        metadataHeaders=['From', 'Subject', 'Date', 'To', 'Cc'],
    ).execute()
    full = service.users().messages().get(userId='me', id=msg_id, format='full').execute()
    return {'meta': meta, 'full': full}


def _fetch_new_gmail_messages(history_id: str | None) -> list[dict]:
    """Fetch new messages via the Gmail history API.

    Serialized by `_gmail_state_lock` so two concurrent Pub/Sub pushes
    (e.g. message-arrived + Gmail's IMPORTANT label-added, which fire
    ~1s apart) don't both read the same `last_history_id` and double-
    classify the same message.

    At-least-once invariants (P3-6):
      - `last_history_id` advances ONLY when the history listing succeeded
        (a transient failure used to silently skip the gap forever). The one
        exception is HTTP 404 = startHistoryId too old, where the only
        option is to accept the gap loudly and re-anchor.
      - A per-message fetch failure queues that id for the retry sweep
        instead of dropping it.
      - Callers mark ids seen only after successful handling.
    """
    with _gmail_state_lock:
        try:
            service = _get_gmail_service()
            if not service:
                log.error('Gmail: no credentials')
                return []

            state = _load_gmail_state()
            last_history_id = state.get('last_history_id')

            if not last_history_id:
                # First push ever: anchor and start from here.
                if history_id:
                    state['last_history_id'] = _as_int_history_id(history_id)
                    _save_gmail_state(state)
                return []

            try:
                msg_ids, latest_hid = _list_new_message_ids(service, last_history_id)
            except Exception as e:
                if '404' in str(e):
                    # startHistoryId older than Gmail's retention — the gap is
                    # unrecoverable; re-anchor loudly instead of wedging forever.
                    log.error('Gmail history 404 (start id too old) — re-anchoring to %s; '
                              'messages in the gap were NOT classified: %s', history_id, e)
                    if history_id:
                        state['last_history_id'] = _as_int_history_id(history_id)
                        _save_gmail_state(state)
                else:
                    log.warning('Gmail history listing failed — NOT advancing '
                                'last_history_id; next push/sweep catches up: %s', e)
                return []

            new_messages = []
            seen = set(state.get('seen_ids', []))
            pending = {p.get('id') for p in state.get('pending_retry', [])}
            for msg_id in msg_ids:
                if msg_id in seen:
                    log.info('Gmail: skipping already-seen msg %s', msg_id)
                    continue
                if msg_id in pending:
                    continue  # the sweep owns it
                try:
                    new_messages.append(_fetch_gmail_message(service, msg_id))
                except Exception as e:
                    log.error('Failed to fetch message %s — queued for retry: %s', msg_id, e)
                    state.setdefault('pending_retry', []).append(
                        {'id': msg_id, 'attempts': 0,
                         'first_seen': datetime.now(timezone.utc).isoformat()})

            # Listing succeeded — safe to advance past this window (failed
            # individual messages live on in pending_retry).
            state['last_history_id'] = _as_int_history_id(latest_hid or history_id or last_history_id)
            _save_gmail_state(state)
            return new_messages

        except Exception as e:
            log.error('_fetch_new_gmail_messages failed: %s', e)
            return []


def _as_int_history_id(val):
    """Gmail returns historyId as a STRING in history.list responses but a
    number in push notifications; the state file must hold one type (int) or
    downstream comparisons break (the 2026-07-02 watch-renewal str>int bug)."""
    try:
        return int(val)
    except (TypeError, ValueError):
        return val


def _mark_gmail_seen(msg_id: str) -> None:
    with _gmail_state_lock:
        state = _load_gmail_state()
        if msg_id not in state.get('seen_ids', []):
            state.setdefault('seen_ids', []).append(msg_id)
        state['pending_retry'] = [p for p in state.get('pending_retry', [])
                                  if p.get('id') != msg_id]
        _save_gmail_state(state)


def _add_gmail_pending_retry(msg_id: str) -> None:
    with _gmail_state_lock:
        state = _load_gmail_state()
        if msg_id in state.get('seen_ids', []):
            return
        pending = state.setdefault('pending_retry', [])
        for p in pending:
            if p.get('id') == msg_id:
                p['attempts'] = int(p.get('attempts', 0)) + 1
                break
        else:
            pending.append({'id': msg_id, 'attempts': 1,
                            'first_seen': datetime.now(timezone.utc).isoformat()})
        _save_gmail_state(state)


GMAIL_MAX_RETRY_ATTEMPTS = 5
GMAIL_SWEEP_INTERVAL_SECONDS = int(os.environ.get('GMAIL_SWEEP_INTERVAL', '600'))


def _sweep_gmail_pending() -> None:
    """Retry messages whose handling failed earlier (at-least-once tail)."""
    with _gmail_state_lock:
        state = _load_gmail_state()
        pending = list(state.get('pending_retry', []))
    if not pending:
        return
    service = _get_gmail_service()
    if not service:
        return
    for entry in pending:
        msg_id = entry.get('id')
        if not msg_id:
            continue
        if int(entry.get('attempts', 0)) >= GMAIL_MAX_RETRY_ATTEMPTS:
            log.error('Gmail msg %s dropped after %s failed attempts (first seen %s) — '
                      'check it manually in the inbox', msg_id, entry.get('attempts'),
                      entry.get('first_seen'))
            # A log line is not an alert -- Brian only finds these by grepping.
            # If the dropped message is the SOFOM distribution trigger, that's a
            # business-critical daily deliverable silently dying; post a loud
            # Slack alert so it gets manual attention instead of vanishing into
            # seen_ids forever. (Root-caused 2026-07-24: msg 19f940dae821bd8d
            # was marked seen via this exact path without ever reaching the
            # SOFOM fast-path, and nobody knew until Brian asked why it hadn't fired.)
            try:
                subj = ''
                try:
                    meta = service.users().messages().get(
                        userId='me', id=msg_id, format='metadata',
                        metadataHeaders=['Subject']).execute()
                    headers = {h['name']: h['value'] for h in meta.get('payload', {}).get('headers', [])}
                    subj = headers.get('Subject', '')
                except Exception:
                    pass
                if _SOFOM_SUBJECT_RE.search(subj or ''):
                    _post_slack(
                        f':rotating_light: SOFOM distribution email dropped after '
                        f'{entry.get("attempts")} failed processing attempts and was '
                        f'never run — gmail msg `{msg_id}` (first seen {entry.get("first_seen")}). '
                        f'Manual tape generation needed: '
                        f'`python report_scheduler_cron.py sofom-distribution`.'
                    )
            except Exception:
                log.exception('Failed to post SOFOM-drop Slack alert for %s', msg_id)
            _mark_gmail_seen(msg_id)  # stop retrying; alert above (if SOFOM) or log is the record
            continue
        try:
            msg = _fetch_gmail_message(service, msg_id)
            _handle_gmail_message(msg)
            _mark_gmail_seen(msg_id)
            log.info('Gmail retry sweep: %s handled successfully', msg_id)
        except Exception as e:
            log.warning('Gmail retry sweep: %s failed again: %s', msg_id, e)
            _add_gmail_pending_retry(msg_id)


def _gmail_sweep_loop() -> None:
    """Daemon: periodic catch-up (missed pushes) + pending-retry drain."""
    while True:
        time.sleep(GMAIL_SWEEP_INTERVAL_SECONDS)
        try:
            _process_gmail_notification(None)  # catch-up from last_history_id
            _sweep_gmail_pending()
        except Exception:
            log.exception('Gmail sweep iteration failed')


# ---------------------------------------------------------------------------
# SOFOM distribution fast-path -- pubsub-driven, bypasses LLM classification
# ---------------------------------------------------------------------------
# Subject line is stable regardless of sender. Previously report_scheduler_cron.py
# polled Gmail hourly with a from:axel@tryjeeves.com-only search; when William Filho
# covered for Axel (2026-07-23) that search silently missed the day's email, stalling
# the tape until manual intervention. This fast-path fires on the FIRST push of the
# day that matches the subject -- no sender check, no waiting for the next poll --
# and shares report_scheduler_cron's state file so the two never double-run.
_SOFOM_SUBJECT_RE = re.compile(r'F/12339\s*>\s*F/12340\s*Distribution', re.IGNORECASE)


def _try_trigger_sofom_distribution(gmail_msg_id: str, subject: str) -> bool:
    """Returns True if this message was the SOFOM distribution email and was
    handled (fired just now, or already done today via an earlier push/the
    hourly poll) -- caller should skip LLM classification entirely in that
    case. Returns False (falls through to normal classification) if the
    borrowing-base module can't be reached, so a bad import never eats the
    email silently."""
    if not _SOFOM_SUBJECT_RE.search(subject or ''):
        return False
    try:
        _bb_skill = Path(__file__).resolve().parent.parent / 'skills' / 'custom' / 'jeeves-borrowing-base'
        if str(_bb_skill) not in sys.path:
            sys.path.insert(0, str(_bb_skill))
        import report_scheduler_cron as rsc

        today_str = datetime.now(timezone.utc).astimezone().strftime('%Y-%m-%d')
        key = 'last_sofom_distribution'
        msg_key = f'{key}_msg_id'
        with rsc._state_lock:
            state = rsc.load_state()
            if rsc._state_completed_today(state, key, today_str):
                # Only a genuine duplicate push of the SAME message may skip
                # classification. Anything else sharing this subject is a reply
                # or a resend and must go through the normal pipeline.
                #
                # 2026-08-17: this returned True for every subject match, so
                # Gustavo Villarreal's reply confirming the day's distribution
                # amount ($110,210,423.47) was dropped before the classifier
                # ever saw it -- no proposal, no Slack, nothing. The reply from
                # the trustee is arguably the most important message on the
                # thread, and it was the one guaranteed to be swallowed.
                if state.get(msg_key) == gmail_msg_id:
                    log.info('SOFOM distribution already handled today -- skipping duplicate push for %s', gmail_msg_id)
                    return True
                log.info('SOFOM tape already done today; %s is a different message on the '
                         'thread (reply/resend) -- passing to normal classification', gmail_msg_id)
                return False
            state[key] = f'{today_str}_running'
            state[msg_key] = gmail_msg_id
            rsc.save_state(state)
        ok = False
        try:
            log.info('SOFOM fast-path: triggering distribution tape for %s (%s)', gmail_msg_id, subject)
            ok = bool(rsc.run_sofom_distribution(gmail_msg_id, subject))
        finally:
            with rsc._state_lock:
                state = rsc.load_state()
                # Record the real outcome. This used to stamp the bare date
                # unconditionally, so a failed run looked identical to a
                # successful one: on 2026-08-17 the 07:03 run died on a
                # ZPA-deauthed Redshift connection, stamped the day complete,
                # and silently disabled the hourly backstop for the rest of
                # the day. '_failed' still blocks duplicate push-driven runs
                # here, but lets the hourly poll retry.
                state[key] = today_str if ok else f'{today_str}_failed'
                rsc.save_state(state)
        if not ok:
            log.warning('SOFOM fast-path run failed — hourly report_scheduler will retry today.')
        return True
    except Exception as e:
        log.error('SOFOM fast-path failed, falling back to normal classification: %s', e)
        return False


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
    to_header = _h('To')
    cc_header = _h('Cc')
    subject = _h('Subject') or '(no subject)'
    date = _h('Date')
    snippet = html.unescape(meta.get('snippet', '')).strip()

    # SOFOM distribution fast-path — check before any noise/self-send skip so
    # a same-day resend or reply-all doesn't get lost, and before the LLM call
    # so the daily tape never waits on classification latency or quota.
    _sofom_msg_id = full.get('id', '') or meta.get('id', '')
    if _try_trigger_sofom_distribution(_sofom_msg_id, subject):
        return

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
        to=to_header,
        cc=cc_header,
    )

    # Non-actionable: silent — no Slack post, no dispatch.
    if not classification.get('actionable'):
        log.info('Non-actionable, no alert: %s | %s',
                 classification.get('action_type'), classification.get('reasoning', '')[:80])
        return

    # Proactively draft a reply (when a written response is the right move) so Brian
    # can approve/edit inline without a round-trip. Empty string when N/A.
    draft = draft_reply(from_header, subject, body_text or snippet,
                        classification.get('summary', ''),
                        classification.get('proposed_action', ''), source='email')
    if draft:
        classification['draft_reply'] = draft

    # Actionable: post a PROPOSAL to Slack so Brian can approve/redirect before any dispatch.
    # No auto-dispatch — the agent runs only when Brian replies in the proposal thread.
    gmail_msg_id = full.get('id', '') or meta.get('id', '')
    gmail_thread_id = full.get('threadId', '') or meta.get('threadId', '')
    existing = _get_proposal_thread(gmail_thread_id)
    slack_post = _post_action_proposal(
        from_header, subject, classification, gmail_msg_id,
        thread_ts=existing.get('ts', ''), thread_channel=existing.get('channel', ''))
    if not existing:
        _record_proposal_thread(gmail_thread_id, slack_post)
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
    return {'last_history_id': None, 'seen_ids': [], 'pending_retry': []}


def _save_gmail_state(state: dict) -> None:
    # Keep seen_ids and the retry queue bounded
    if len(state.get('seen_ids', [])) > 1000:
        state['seen_ids'] = state['seen_ids'][-1000:]
    if len(state.get('pending_retry', [])) > 50:
        state['pending_retry'] = state['pending_retry'][-50:]
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

    # Handle DMs and app mentions off the event loop. Slack retries if we're
    # slow (3s budget), and the handler chain calls into mem0/Qdrant + the
    # classifier LLM — synchronous work that would otherwise stall /health.
    if event_type in ('message', 'app_mention'):
        _spawn_bg(asyncio.to_thread(_handle_slack_event, event, body))

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
