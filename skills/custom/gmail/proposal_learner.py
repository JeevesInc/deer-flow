#!/usr/bin/env python3
"""Proposal feedback learner.

Once a day, pairs every proposal the webhook posted to Slack with what
Brian actually did in that thread, labels the outcome, and uses Sonnet to
synthesize learnings that get written to mem0 under user_id="proposal-patterns".

The classifier in webhook_receiver.py queries those patterns at classify
time and injects the most relevant ones into the prompt, so future proposals
get smarter over time.

Pipeline:
  1. Read proposal_log.jsonl entries posted ≥24h ago and not yet in
     proposal_outcomes.jsonl.
  2. For each: pull the Slack thread via conversations.replies(ts=...).
  3. Haiku labels the outcome (approved/redirected/ignored/rejected).
  4. Append labeled pair to proposal_outcomes.jsonl.
  5. Sonnet reads the day's labeled pairs and produces 1-5 patterns.
  6. Each pattern is written to mem0.

Designed to be idempotent — already-labeled proposals are skipped, and
mem0's own dedup merges semantically duplicate patterns.

Run as a library (preferred — called from eod_review_cron.run_eod_review):
    from proposal_learner import run_daily
    run_daily()

Or standalone for testing:
    python proposal_learner.py
    python proposal_learner.py --dry-run
"""

import json
import logging
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

# Bootstrap — load .env from the DeerFlow backend directory
_HERE = Path(__file__).resolve()
_BACKEND = _HERE.parents[3] / 'backend'
_SHARED = _HERE.parent.parent / '_shared'
if _SHARED.exists() and str(_SHARED) not in sys.path:
    sys.path.insert(0, str(_SHARED))
try:
    from env_loader import load_env
    load_env()
except Exception:
    pass

logging.basicConfig(
    level=logging.INFO,
    format='[Learner %(asctime)s] %(message)s',
    datefmt='%H:%M:%S',
)
log = logging.getLogger('proposal_learner')

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

PROPOSAL_LOG_PATH = _BACKEND / '.deer-flow' / 'proposal_log.jsonl'
OUTCOMES_LOG_PATH = _BACKEND / '.deer-flow' / 'proposal_outcomes.jsonl'

ANTHROPIC_API_KEY = os.environ.get('ANTHROPIC_API_KEY', '')
SLACK_BOT_TOKEN = os.environ.get('SLACK_BOT_TOKEN', '')
SLACK_OWNER_USER_ID = os.environ.get('SLACK_OWNER_USER_ID', 'U05B5HGNCN9')

LABELER_MODEL = 'claude-haiku-4-5-20251001'
SYNTHESIZER_MODEL = 'claude-sonnet-4-6-20251001'

MIN_PROPOSAL_AGE_HOURS = 24          # only label proposals at least this old
MAX_PATTERNS_PER_DAY = 5             # synthesizer cap
PROPOSAL_PATTERNS_USER_ID = 'proposal-patterns'

# ---------------------------------------------------------------------------
# I/O — read/write the append-only logs
# ---------------------------------------------------------------------------


def _read_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    out = []
    with path.open('r', encoding='utf-8') as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except Exception:
                log.warning('Skipping malformed line in %s', path.name)
    return out


def _append_jsonl(path: Path, entry: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('a', encoding='utf-8') as f:
        f.write(json.dumps(entry) + '\n')


def _parse_iso(ts: str) -> datetime | None:
    try:
        return datetime.fromisoformat(ts.replace('Z', '+00:00'))
    except Exception:
        return None


def load_unlabeled_proposals() -> list[dict]:
    """Return proposals posted ≥MIN_PROPOSAL_AGE_HOURS ago and not yet labeled."""
    proposals = _read_jsonl(PROPOSAL_LOG_PATH)
    outcomes = _read_jsonl(OUTCOMES_LOG_PATH)
    labeled_ts = {o.get('proposal_slack_ts') for o in outcomes if o.get('proposal_slack_ts')}

    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(hours=MIN_PROPOSAL_AGE_HOURS)

    unlabeled = []
    for p in proposals:
        ts = p.get('slack_ts')
        if not ts or ts in labeled_ts:
            continue
        posted = _parse_iso(p.get('posted_at', ''))
        if posted is None or posted > cutoff:
            continue
        unlabeled.append(p)
    return unlabeled


# ---------------------------------------------------------------------------
# Slack — fetch thread replies
# ---------------------------------------------------------------------------


def fetch_slack_thread(channel: str, parent_ts: str) -> list[dict]:
    """Return the thread's replies (including the parent message) as a list."""
    if not SLACK_BOT_TOKEN or not channel or not parent_ts:
        return []
    try:
        from slack_sdk import WebClient
        client = WebClient(token=SLACK_BOT_TOKEN)
        resp = client.conversations_replies(channel=channel, ts=parent_ts, limit=50)
        return resp.get('messages', []) or []
    except Exception as e:
        log.warning('conversations_replies(channel=%s, ts=%s) failed: %s',
                    channel, parent_ts, e)
        return []


def _summarize_thread_for_labeler(messages: list[dict]) -> tuple[str, str, str, int]:
    """
    Render the thread for the labeler. Returns:
      (rendered_thread, brian_reply_text, brian_reply_ts, lag_seconds)
    """
    rendered = []
    brian_reply = ''
    brian_reply_ts = ''
    lag_seconds = 0
    parent_ts_float = None

    for i, m in enumerate(messages):
        author = ''
        text = m.get('text', '') or ''
        user = m.get('user', '')
        if m.get('bot_id') or m.get('subtype') == 'bot_message':
            author = 'bot'
        elif user == SLACK_OWNER_USER_ID:
            author = 'brian'
        elif user:
            author = f'user[{user}]'
        else:
            author = 'unknown'

        ts = m.get('ts', '')
        if i == 0:
            try:
                parent_ts_float = float(ts)
            except Exception:
                parent_ts_float = None

        if author == 'brian' and not brian_reply:
            brian_reply = text.strip()
            brian_reply_ts = ts
            if parent_ts_float is not None:
                try:
                    lag_seconds = int(float(ts) - parent_ts_float)
                except Exception:
                    lag_seconds = 0

        # Truncate per-message text for the labeler prompt
        snippet = text.strip()
        if len(snippet) > 800:
            snippet = snippet[:800] + '...'
        rendered.append(f'[{author}] {snippet}')

    return '\n\n'.join(rendered), brian_reply, brian_reply_ts, lag_seconds


# ---------------------------------------------------------------------------
# Labeling — Haiku call per proposal
# ---------------------------------------------------------------------------


def label_outcome(proposal: dict, thread_messages: list[dict]) -> dict:
    """
    Returns an outcome dict matching the proposal_outcomes.jsonl schema.
    """
    rendered_thread, brian_reply, brian_reply_ts, lag = _summarize_thread_for_labeler(thread_messages)

    # If thread has ONLY the parent (no replies), it's ignored — skip the LLM call.
    if len(thread_messages) <= 1:
        return {
            'proposal_slack_ts': proposal.get('slack_ts'),
            'labeled_at': datetime.now(timezone.utc).isoformat(),
            'outcome': 'ignored',
            'brian_reply': '',
            'brian_reply_ts': '',
            'agent_response': '',
            'lag_seconds': 0,
            'label_reasoning': 'No replies in thread.',
        }

    if not ANTHROPIC_API_KEY:
        return {
            'proposal_slack_ts': proposal.get('slack_ts'),
            'labeled_at': datetime.now(timezone.utc).isoformat(),
            'outcome': 'unknown',
            'brian_reply': brian_reply,
            'brian_reply_ts': brian_reply_ts,
            'agent_response': '',
            'lag_seconds': lag,
            'label_reasoning': 'No ANTHROPIC_API_KEY configured.',
        }

    from anthropic import Anthropic
    client = Anthropic(api_key=ANTHROPIC_API_KEY)

    prompt = f"""You are labeling the outcome of a proposed email action that an automated
classifier posted to Slack for Brian Mauck's review.

## The proposal
- Category: {proposal.get('category')}
- Priority: {proposal.get('priority')}
- Summary: {proposal.get('summary')}
- Proposed action: {proposal.get('proposed_action')}

## The Slack thread (chronological — [bot] = proposal/agent, [brian] = Brian)
{rendered_thread}

---

Label the outcome with EXACTLY one of:
  approved   — Brian's reply approves the proposed action (e.g. "go", "yes", "do it",
               or paraphrases the proposed action affirmatively)
  redirected — Brian's reply gives different OR additional instructions; the agent
               should NOT just execute the proposed_action as-is
  rejected   — Brian's reply declines or says the email shouldn't have been surfaced.
               Examples: "skip", "not now", "no", "ignore", "this isn't for me",
               "this doesn't require a response", "fyi only", "not actionable",
               "wrong person", "ignore this", "I don't need to act on this"
  ignored    — Brian never replied in the thread

Also extract:
  - agent_response: a short paraphrase of any [bot] message AFTER Brian's reply
                    (the work the agent did). Empty string if none.

Respond with ONLY valid JSON in this exact format:
{{
  "outcome": "approved" | "redirected" | "rejected" | "ignored",
  "agent_response": "short paraphrase or empty string",
  "label_reasoning": "1 sentence explaining the label"
}}"""

    try:
        resp = client.messages.create(
            model=LABELER_MODEL,
            max_tokens=400,
            messages=[{'role': 'user', 'content': prompt}],
        )
        text = resp.content[0].text.strip()
        if text.startswith('```'):
            text = text.split('```')[1]
            if text.startswith('json'):
                text = text[4:]
        labeled = json.loads(text.strip())
        outcome_value = labeled.get('outcome', 'unknown')
        if outcome_value not in ('approved', 'redirected', 'rejected', 'ignored'):
            outcome_value = 'unknown'
        return {
            'proposal_slack_ts': proposal.get('slack_ts'),
            'labeled_at': datetime.now(timezone.utc).isoformat(),
            'outcome': outcome_value,
            'brian_reply': brian_reply,
            'brian_reply_ts': brian_reply_ts,
            'agent_response': labeled.get('agent_response', ''),
            'lag_seconds': lag,
            'label_reasoning': labeled.get('label_reasoning', ''),
        }
    except Exception as e:
        log.error('Label call failed for ts=%s: %s', proposal.get('slack_ts'), e)
        return {
            'proposal_slack_ts': proposal.get('slack_ts'),
            'labeled_at': datetime.now(timezone.utc).isoformat(),
            'outcome': 'unknown',
            'brian_reply': brian_reply,
            'brian_reply_ts': brian_reply_ts,
            'agent_response': '',
            'lag_seconds': lag,
            'label_reasoning': f'Labeler error: {e}',
        }


# ---------------------------------------------------------------------------
# Synthesis — one Sonnet call on the day's labeled pairs
# ---------------------------------------------------------------------------


def _build_synth_pairs(proposals: list[dict], outcomes: list[dict]) -> list[dict]:
    by_ts = {p.get('slack_ts'): p for p in proposals if p.get('slack_ts')}
    pairs = []
    for o in outcomes:
        p = by_ts.get(o.get('proposal_slack_ts'))
        if not p:
            continue
        pairs.append({'proposal': p, 'outcome': o})
    return pairs


def synthesize_patterns(pairs: list[dict]) -> list[str]:
    """Call Sonnet on the day's pairs → list of pattern sentences (or empty)."""
    if not pairs or not ANTHROPIC_API_KEY:
        return []

    from anthropic import Anthropic
    client = Anthropic(api_key=ANTHROPIC_API_KEY)

    rendered = []
    for i, pair in enumerate(pairs, 1):
        p, o = pair['proposal'], pair['outcome']
        rendered.append(
            f"### Case {i} — outcome: {o.get('outcome', '?').upper()}\n"
            f"From: {p.get('sender_display', '')} <{p.get('sender_email', '')}> ({p.get('sender_domain', '')})\n"
            f"Subject: {p.get('subject', '')}\n"
            f"Category: {p.get('category', '')}\n"
            f"Proposed action: {p.get('proposed_action', '')}\n"
            f"Brian's reply: {o.get('brian_reply', '')[:300]}\n"
            f"Label reason: {o.get('label_reasoning', '')}"
        )
    cases = '\n\n'.join(rendered)

    prompt = f"""You are refining the classifier that proposes actions on Brian Mauck's
inbound email. Here are recent labeled outcomes — what the classifier
proposed vs what Brian actually wanted.

{cases}

---

Focus on REJECTED, REDIRECTED, and IGNORED cases. They tell you where the
classifier got it wrong. Extract at most {MAX_PATTERNS_PER_DAY} concrete patterns
to apply next time.

CRITICAL — patterns must bind the EMAIL CONTEXT (topic, content type, what's
being asked), not just the sender. A pattern that says "ignore emails from X"
will blanket-block real future asks from X. Always pair sender with the
specific kind of email being rejected/redirected.

Look at Brian's actual reply to understand WHY he rejected/redirected:
  - "this isn't for me"           → the wrong person was on the email; pattern: sender + topic
  - "doesn't require a response"  → email is informational; pattern: sender + email TYPE
  - "fyi only"                    → CC/BCC notification; pattern: sender + delivery shape
  - "actually just acknowledge"   → action was over-scoped; pattern: sender + topic + lighter action

Each pattern must be ONE sentence, specific enough to act on. Good patterns:
  - "Status update CCs from David García at BBVA (no direct ask) are FYI, not actionable."
  - "Calendar-share emails from neuberger.com domain are not actionable even when Brian is the recipient."
  - "When BBVA mentions Aforo without a deadline, Brian wants a holding reply, not a data pull."
  - "Counsel-to-counsel CCs on the NB facility (akingump.com → goodwinlaw.com) are not actionable."
  - "Internal Jeeves stand-up summaries that mention Brian are FYI even when phrased as questions."

BAD patterns (never produce these — too blunt):
  - "Emails from David García are not actionable."                  ← blankets the sender
  - "BBVA emails are FYI."                                          ← blankets the counterparty
  - "Be more careful with low-priority emails."                     ← not concrete
  - "Read the email carefully."                                     ← not actionable

If Brian rejected one email from a sender who usually IS actionable, the
pattern should describe the rejected SUB-TYPE (e.g. "CC updates with no ask"),
not the sender as a whole.

If no clear pattern emerges from this batch, output exactly: NONE

Respond with ONLY valid JSON in this exact format:
{{
  "patterns": [
    {{"text": "the pattern sentence", "sender_domain": "<domain or empty>", "sender_email": "<email or empty>", "category": "<category or empty>"}}
  ]
}}

If no patterns, return: {{"patterns": []}}
"""

    try:
        resp = client.messages.create(
            model=SYNTHESIZER_MODEL,
            max_tokens=1500,
            messages=[{'role': 'user', 'content': prompt}],
        )
        text = resp.content[0].text.strip()
        if text.upper().startswith('NONE'):
            return []
        if text.startswith('```'):
            text = text.split('```')[1]
            if text.startswith('json'):
                text = text[4:]
        data = json.loads(text.strip())
        patterns = data.get('patterns', []) or []
        # Return just the text strings; metadata is currently informational only
        # (mem0 dedup is text-based via its add() pipeline).
        return [p.get('text', '').strip() for p in patterns if p.get('text')]
    except Exception as e:
        log.error('Synthesizer call failed: %s', e)
        return []


# ---------------------------------------------------------------------------
# mem0 — write patterns under a dedicated user_id namespace
# ---------------------------------------------------------------------------


def _get_mem0():
    """Import the harness mem0 instance (shared with the agent)."""
    harness = _BACKEND / 'packages' / 'harness'
    if harness.exists() and str(harness) not in sys.path:
        sys.path.insert(0, str(harness))
    from deerflow.agents.memory.mem0_store import get_mem0
    return get_mem0()


def write_pattern_to_mem0(text: str) -> None:
    """Add a single pattern fact under the proposal-patterns user_id."""
    if not text.strip():
        return
    try:
        m = _get_mem0()
        m.add(text, user_id=PROPOSAL_PATTERNS_USER_ID)
    except Exception as e:
        log.error('mem0.add failed for pattern %r: %s', text[:80], e)


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------


def run_daily(dry_run: bool = False) -> dict:
    """Label any unlabeled proposals ≥24h old, then synthesize+store patterns.

    Returns a small summary dict for the caller to log.
    """
    summary = {
        'labeled': 0,
        'patterns_added': 0,
        'errors': [],
    }

    unlabeled = load_unlabeled_proposals()
    log.info('Found %d unlabeled proposals ≥%dh old', len(unlabeled), MIN_PROPOSAL_AGE_HOURS)

    new_outcomes: list[dict] = []
    for prop in unlabeled:
        ch = prop.get('slack_channel', '')
        ts = prop.get('slack_ts', '')
        if not ch or not ts:
            continue
        try:
            messages = fetch_slack_thread(ch, ts)
            outcome = label_outcome(prop, messages)
            new_outcomes.append(outcome)
            if not dry_run:
                _append_jsonl(OUTCOMES_LOG_PATH, outcome)
            summary['labeled'] += 1
            log.info('Labeled ts=%s → %s', ts, outcome.get('outcome'))
            time.sleep(0.2)  # be nice to Slack API
        except Exception as e:
            log.error('Failed to label proposal ts=%s: %s', ts, e)
            summary['errors'].append(f'label {ts}: {e}')

    # Synthesis runs on the proposals + outcomes from THIS labeling batch.
    if new_outcomes:
        all_proposals = _read_jsonl(PROPOSAL_LOG_PATH)
        pairs = _build_synth_pairs(all_proposals, new_outcomes)
        patterns = synthesize_patterns(pairs)
        log.info('Synthesizer produced %d patterns', len(patterns))
        for pat in patterns:
            if dry_run:
                log.info('[dry-run] would add pattern: %s', pat)
            else:
                write_pattern_to_mem0(pat)
                summary['patterns_added'] += 1
                log.info('Added pattern: %s', pat[:140])
    else:
        log.info('No new outcomes to synthesize from; skipping.')

    log.info('Daily run summary: %s', summary)
    return summary


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser(description='Proposal feedback learner')
    parser.add_argument('--dry-run', action='store_true',
                        help='Label proposals and print synthesized patterns but do not write outcomes or mem0')
    args = parser.parse_args()
    run_daily(dry_run=args.dry_run)
