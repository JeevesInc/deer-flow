#!/usr/bin/env python3
"""Action learner — behavioral cloning from Brian's OWN actions.

Sibling of proposal_learner.py. Where that one learns from how Brian reacts to
*the agent's* proposals, this one learns from what Brian does unprompted:

  * what he replies to (and, just as informative, what he lets die)
  * how he replies — length, structure, opener/closer, directness, how the
    register shifts between lender counsel, Alex, and his own team
  * what he concedes vs holds, escalates vs absorbs, delegates vs does himself

Sources
  1. Gmail sent mail (full bodies), paired with the inbound message that
     triggered the reply and the response latency.
  2. Inbound threads in the same window that Brian never replied to
     (negative triage signal, sampled).
  3. Slack messages sent by Brian (search API, from:<@OWNER>).

Output
  Pattern sentences in mem0 under user_id="brian-behavior", tagged by axis
  ([TRIAGE] / [VOICE] / [SUBSTANCE]) and stored with infer=False so the
  curated sentence survives verbatim. Retrieved by semantic search before
  any drafting or triage turn.

Bloat control
  consolidate() merges duplicates and resolves contradictions (recent wins)
  down to a target ceiling. Called weekly from dreams_cron.

Usage
    python action_learner.py --backfill --since 2026-01-01
    python action_learner.py --backfill --since 2026-01-01 --max 400
    python action_learner.py                      # incremental (since last run)
    python action_learner.py --consolidate
    python action_learner.py --consolidate --dry-run
    python action_learner.py --status

    from action_learner import run_daily, consolidate_if_due
"""

import argparse
import json
import logging
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

# Bootstrap — mirror proposal_learner.py exactly
_HERE = Path(__file__).resolve()
_BACKEND = _HERE.parents[3] / 'backend'
_SHARED = _HERE.parent.parent / '_shared'
if str(_HERE.parent) not in sys.path:
    sys.path.insert(0, str(_HERE.parent))
if _SHARED.exists() and str(_SHARED) not in sys.path:
    sys.path.insert(0, str(_SHARED))
try:
    from env_loader import load_env
    load_env()
except Exception:
    pass

logging.basicConfig(
    level=logging.INFO,
    format='[ActionLearner %(asctime)s] %(message)s',
    datefmt='%H:%M:%S',
)
log = logging.getLogger('action_learner')

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

STATE_PATH = _BACKEND / '.deer-flow' / 'action_learner_state.json'
OBS_LOG_PATH = _BACKEND / '.deer-flow' / 'action_observations.jsonl'

ANTHROPIC_API_KEY = os.environ.get('ANTHROPIC_API_KEY', '')
SYNTHESIZER_MODEL = 'claude-opus-5'

OWNER_EMAIL = 'brian.mauck@tryjeeves.com'
OWNER_LOCAL = OWNER_EMAIL.split('@')[0]
try:
    from identity import owner_slack_id
    SLACK_OWNER_USER_ID = owner_slack_id()
except Exception:
    SLACK_OWNER_USER_ID = os.environ.get('SLACK_OWNER_USER_ID', 'U05B5HGNCN9')

BEHAVIOR_USER_ID = 'brian-behavior'

BATCH_SIZE = 18                  # observations per synthesis call
MAX_PATTERNS_PER_BATCH = 6
BODY_TRUNCATE = 1400             # chars of Brian's own text kept
TRIGGER_TRUNCATE = 900           # chars of the message he was replying to
IGNORED_SAMPLE_PER_RUN = 40      # negative-signal cap per run
CONSOLIDATE_INTERVAL_DAYS = 7
BEHAVIOR_TARGET_MAX = 140        # ceiling after consolidation
CONSOLIDATE_FLOOR = 60           # don't bother below this many facts

AUTOMATED_HINTS = (
    'no-reply', 'noreply', 'donotreply', 'notifications@', 'calendar-notification',
    'mailer-daemon', 'via google drive', 'automated',
)

# ---------------------------------------------------------------------------
# State / IO
# ---------------------------------------------------------------------------


def load_state() -> dict:
    if STATE_PATH.exists():
        try:
            return json.loads(STATE_PATH.read_text(encoding='utf-8'))
        except Exception:
            log.warning('State file unreadable; starting fresh')
    return {
        'processed_ids': [],
        'last_run': None,
        'last_consolidated': None,
        'patterns_written': 0,
        'observations': 0,
    }


def save_state(state: dict) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(state, indent=2, default=str), encoding='utf-8')


def _append_jsonl(path: Path, entry: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('a', encoding='utf-8') as f:
        f.write(json.dumps(entry, default=str) + '\n')


def _now() -> datetime:
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# Gmail helpers — reuse gmail_tool's body extraction
# ---------------------------------------------------------------------------


def _gmail():
    from gmail_tool import _get_service
    return _get_service()


def _body_text(payload) -> str:
    from gmail_tool import _get_body
    try:
        return (_get_body(payload) or '').strip()
    except Exception:
        return ''


def _hdr(headers: list, name: str) -> str:
    for h in headers or []:
        if h.get('name', '').lower() == name.lower():
            return h.get('value', '')
    return ''


def _strip_quoted(text: str) -> str:
    """Drop quoted history so we learn Brian's own words, not the chain."""
    if not text:
        return ''
    lines = []
    for line in text.splitlines():
        s = line.strip()
        if s.startswith('>'):
            continue
        if re.match(r'^On .{5,80} wrote:$', s):
            break
        if re.match(r'^-{2,}\s*(Original Message|Forwarded message)', s, re.I):
            break
        if re.match(r'^From:\s*.+@', s):
            break
        lines.append(line)
    return '\n'.join(lines).strip()


def _is_automated(sender: str) -> bool:
    s = (sender or '').lower()
    return any(h in s for h in AUTOMATED_HINTS)


def _addr_domain(addr: str) -> str:
    m = re.search(r'[\w\.\-\+]+@([\w\.\-]+)', addr or '')
    return m.group(1).lower() if m else ''


def _internal_dt(msg: dict):
    try:
        return datetime.fromtimestamp(int(msg.get('internalDate', 0)) / 1000, tz=timezone.utc)
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Collection — sent mail paired with its trigger
# ---------------------------------------------------------------------------


def collect_sent(since: str, max_messages: int, processed: set) -> list:
    """Fetch Brian's sent mail since `since` (YYYY-MM-DD), paired with trigger."""
    svc = _gmail()
    gq_since = since.replace('-', '/')
    query = 'in:sent after:' + gq_since
    observations = []
    thread_cache = {}
    page_token = None
    scanned = 0

    while len(observations) < max_messages:
        try:
            resp = svc.users().messages().list(
                userId='me', q=query, maxResults=100, pageToken=page_token,
            ).execute()
        except Exception as e:
            log.error('Sent list failed: %s', e)
            break

        refs = resp.get('messages', []) or []
        if not refs:
            break

        for ref in refs:
            if len(observations) >= max_messages:
                break
            mid = ref['id']
            if mid in processed:
                continue
            scanned += 1
            try:
                msg = svc.users().messages().get(userId='me', id=mid, format='full').execute()
            except Exception:
                continue

            headers = msg.get('payload', {}).get('headers', [])
            to = _hdr(headers, 'To')
            cc = _hdr(headers, 'Cc')
            subject = _hdr(headers, 'Subject')
            sent_at = _internal_dt(msg)
            body = _strip_quoted(_body_text(msg.get('payload', {})))
            if not body:
                continue

            tid = msg.get('threadId')
            trigger = {}
            if tid:
                if tid not in thread_cache:
                    try:
                        th = svc.users().threads().get(userId='me', id=tid, format='full').execute()
                        thread_cache[tid] = th.get('messages', []) or []
                    except Exception:
                        thread_cache[tid] = []
                    time.sleep(0.12)
                prior = []
                for tm in thread_cache[tid]:
                    tdt = _internal_dt(tm)
                    if tdt and sent_at and tdt < sent_at:
                        prior.append((tdt, tm))
                prior.sort(key=lambda x: x[0])
                for tdt, tm in reversed(prior):
                    th_headers = tm.get('payload', {}).get('headers', [])
                    frm = _hdr(th_headers, 'From')
                    if OWNER_LOCAL in frm.lower():
                        continue
                    trigger = {
                        'from': frm,
                        'from_domain': _addr_domain(frm),
                        'subject': _hdr(th_headers, 'Subject'),
                        'body': _strip_quoted(_body_text(tm.get('payload', {})))[:TRIGGER_TRUNCATE],
                        'received_at': tdt.isoformat(),
                        'latency_minutes': round((sent_at - tdt).total_seconds() / 60, 1) if sent_at else None,
                    }
                    break

            observations.append({
                'kind': 'sent_email',
                'id': mid,
                'thread_id': tid,
                'sent_at': sent_at.isoformat() if sent_at else '',
                'to': to,
                'cc': cc,
                'recipient_domains': sorted({_addr_domain(a) for a in (to + ',' + cc).split(',') if '@' in a}),
                'subject': subject,
                'body': body[:BODY_TRUNCATE],
                'body_chars': len(body),
                'is_reply': bool(trigger),
                'trigger': trigger,
            })
            time.sleep(0.08)

        page_token = resp.get('nextPageToken')
        if not page_token:
            break
        log.info('  collected %d sent observations (scanned %d)...', len(observations), scanned)

    log.info('Collected %d sent-mail observations', len(observations))
    return observations


def collect_ignored(since: str, cap: int, processed: set) -> list:
    """Inbound threads Brian never replied to — negative triage signal."""
    svc = _gmail()
    gq_since = since.replace('-', '/')
    query = 'in:inbox -in:sent -in:chats -category:promotions -category:social after:' + gq_since
    out = []
    page_token = None

    while len(out) < cap:
        try:
            resp = svc.users().threads().list(
                userId='me', q=query, maxResults=100, pageToken=page_token,
            ).execute()
        except Exception as e:
            log.error('Ignored-thread list failed: %s', e)
            break
        threads = resp.get('threads', []) or []
        if not threads:
            break

        for tref in threads:
            if len(out) >= cap:
                break
            tid = tref['id']
            key = 'ignored:' + tid
            if key in processed:
                continue
            try:
                th = svc.users().threads().get(userId='me', id=tid, format='full').execute()
            except Exception:
                continue
            msgs = th.get('messages', []) or []
            if any(OWNER_LOCAL in _hdr(m.get('payload', {}).get('headers', []), 'From').lower()
                   for m in msgs):
                continue  # Brian participated — not an ignore
            first = msgs[0] if msgs else None
            if not first:
                continue
            fh = first.get('payload', {}).get('headers', [])
            frm = _hdr(fh, 'From')
            if _is_automated(frm):
                continue
            body = _strip_quoted(_body_text(first.get('payload', {})))
            if len(body) < 60:
                continue
            dt = _internal_dt(first)
            out.append({
                'kind': 'no_reply',
                'id': key,
                'thread_id': tid,
                'received_at': dt.isoformat() if dt else '',
                'from': frm,
                'from_domain': _addr_domain(frm),
                'to': _hdr(fh, 'To'),
                'cc': _hdr(fh, 'Cc'),
                'subject': _hdr(fh, 'Subject'),
                'body': body[:TRIGGER_TRUNCATE],
                'thread_len': len(msgs),
            })
            time.sleep(0.1)

        page_token = resp.get('nextPageToken')
        if not page_token:
            break

    log.info('Collected %d no-reply observations', len(out))
    return out


def collect_slack(since: str, cap: int, processed: set) -> list:
    """Messages Brian sent in Slack (search API, user token)."""
    out = []
    try:
        sys.path.insert(0, str(_HERE.parents[1] / 'slack-search'))
        from slack_tool import _get_client
        client = _get_client()
    except Exception as e:
        log.warning('Slack client unavailable (%s) — skipping Slack source', e)
        return out

    query = 'from:<@' + SLACK_OWNER_USER_ID + '> after:' + since
    page = 1
    while len(out) < cap and page <= 10:
        try:
            resp = client.search_messages(query=query, count=100, page=page,
                                          sort='timestamp', sort_dir='desc')
        except Exception as e:
            log.warning('Slack search failed: %s', e)
            break
        matches = (resp.get('messages', {}) or {}).get('matches', []) or []
        if not matches:
            break
        for m in matches:
            if len(out) >= cap:
                break
            key = 'slack:' + str(m.get('ts', ''))
            if key in processed or not m.get('text'):
                continue
            try:
                sent_at = datetime.fromtimestamp(float(m.get('ts', 0)), tz=timezone.utc).isoformat()
            except Exception:
                sent_at = ''
            out.append({
                'kind': 'slack_sent',
                'id': key,
                'sent_at': sent_at,
                'channel': (m.get('channel', {}) or {}).get('name', ''),
                'is_dm': (m.get('channel', {}) or {}).get('is_im', False),
                'body': (m.get('text') or '')[:BODY_TRUNCATE],
                'body_chars': len(m.get('text') or ''),
                'permalink': m.get('permalink', ''),
            })
        page += 1
        time.sleep(0.4)

    log.info('Collected %d Slack observations', len(out))
    return out


# ---------------------------------------------------------------------------
# Synthesis
# ---------------------------------------------------------------------------


def _render_observation(i: int, o: dict) -> str:
    if o['kind'] == 'sent_email':
        t = o.get('trigger') or {}
        head = (
            "### Case %d — EMAIL BRIAN SENT (%s)\n" % (i, 'reply' if o['is_reply'] else 'unprompted') +
            "Sent: %s  |  length: %s chars\n" % (o.get('sent_at', ''), o.get('body_chars', 0)) +
            "To: %s\nCc: %s\n" % (o.get('to', '')[:200], o.get('cc', '')[:200]) +
            "Subject: %s\n" % o.get('subject', '')
        )
        if t:
            head += (
                "-- He was replying to --\nFrom: %s\n" % t.get('from', '') +
                "Latency: %s minutes\n" % t.get('latency_minutes') +
                "Their message: %s\n" % t.get('body', '')
            )
        return head + "-- HIS WORDS (verbatim) --\n" + o.get('body', '')
    if o['kind'] == 'no_reply':
        return (
            "### Case %d — INBOUND BRIAN NEVER ANSWERED\n" % i +
            "From: %s\nTo: %s\n" % (o.get('from', ''), o.get('to', '')[:160]) +
            "Subject: %s\nThread length: %s\n" % (o.get('subject', ''), o.get('thread_len')) +
            "Message: %s" % o.get('body', '')
        )
    return (
        "### Case %d — SLACK MESSAGE BRIAN SENT\n" % i +
        "Channel: %s (DM=%s)  |  %s\n" % (o.get('channel', ''), o.get('is_dm'), o.get('sent_at', '')) +
        "length: %s chars\n-- HIS WORDS (verbatim) --\n%s" % (o.get('body_chars', 0), o.get('body', ''))
    )


SYNTH_PROMPT = """You are building a behavioral model of Brian Mauck (Head of Capital
Markets at Jeeves) so an AI analyst can triage and draft the way he does.

Below are real, unprompted actions Brian took — emails he sent, the messages that
triggered them, inbound mail he chose NOT to answer, and Slack messages he sent.

{cases}

---

Extract at most {maxp} patterns across these three axes. Tag each with its axis.

TRIAGE — what earns a response, how fast, from whom, and what he lets die.
  Use latency and the no-reply cases. Bind the pattern to the TYPE of message,
  never to a person wholesale.
VOICE — how he actually writes: length, whether he leads with the answer,
  openers/closers, bullets vs prose, hedging vs directness, and how the register
  shifts by audience (lender counsel vs Alex vs his own team vs Slack).
SUBSTANCE — what he concedes vs holds, escalates vs absorbs, delegates vs does
  himself, what caveats he always attaches, what he refuses to send without a tie-out.

RULES
- Each pattern is ONE actionable sentence an agent can follow when drafting or triaging.
- Bind pattern to CONTEXT (audience type + message type), not to a person alone.
- Quote or paraphrase his actual phrasing when it is the point.
- Prefer patterns supported by 2+ cases. If a single case is striking, say "observed once".
- Do not restate generic email etiquette. Only what is distinctively HIM.

GOOD
  - "[VOICE] To lender counsel Brian answers item-by-item in the sender's own numbering, one line each, no preamble."
  - "[TRIAGE] Vendor/marketing intros addressed to him directly get no reply at all — zero responses across 6 cases."
  - "[SUBSTANCE] He will not send a number externally until it ties to a source system, and says so explicitly rather than sending a caveated figure."
BAD
  - "[VOICE] Brian is direct."                      <- not actionable
  - "[TRIAGE] Brian ignores emails from Kroll."     <- blankets a counterparty
  - "[VOICE] He uses professional language."        <- generic

If nothing distinctive emerges, return {{"patterns": []}}.

Respond with ONLY valid JSON:
{{"patterns": [{{"axis": "TRIAGE|VOICE|SUBSTANCE", "text": "the sentence", "support": "n cases"}}]}}
"""


def _text_from_response(resp) -> str:
    parts = []
    for block in getattr(resp, 'content', []) or []:
        if getattr(block, 'type', '') == 'text':
            parts.append(block.text)
    return '\n'.join(parts).strip()


def _parse_json(text: str):
    if not text:
        return None
    t = text.strip()
    if t.startswith('```'):
        t = re.sub(r'^```[a-zA-Z]*\n', '', t)
        t = re.sub(r'\n```$', '', t).strip()
    try:
        return json.loads(t)
    except Exception:
        m = re.search(r'\{.*\}', t, re.S)
        if m:
            try:
                return json.loads(m.group(0))
            except Exception:
                return None
    return None


def synthesize(observations: list) -> list:
    if not observations or not ANTHROPIC_API_KEY:
        return []
    from anthropic import Anthropic
    client = Anthropic(api_key=ANTHROPIC_API_KEY)

    cases = '\n\n'.join(_render_observation(i, o) for i, o in enumerate(observations, 1))
    prompt = SYNTH_PROMPT.format(cases=cases, maxp=MAX_PATTERNS_PER_BATCH)
    try:
        resp = client.messages.create(
            model=SYNTHESIZER_MODEL, max_tokens=4000,
            messages=[{'role': 'user', 'content': prompt}],
        )
        data = _parse_json(_text_from_response(resp)) or {}
        out = []
        for p in data.get('patterns', []) or []:
            txt = (p.get('text') or '').strip()
            if not txt:
                continue
            axis = (p.get('axis') or '').strip().upper()
            if axis and not txt.startswith('['):
                txt = '[' + axis + '] ' + txt
            out.append(txt)
        return out
    except Exception as e:
        log.error('Synthesis call failed: %s', e)
        return []


# ---------------------------------------------------------------------------
# mem0
# ---------------------------------------------------------------------------


def _get_mem0():
    harness = _BACKEND / 'packages' / 'harness'
    if harness.exists() and str(harness) not in sys.path:
        sys.path.insert(0, str(harness))
    from deerflow.agents.memory.mem0_store import get_mem0
    return get_mem0()


def write_pattern(text: str) -> bool:
    if not text.strip():
        return False
    try:
        _get_mem0().add(text, user_id=BEHAVIOR_USER_ID, infer=False)
        return True
    except Exception as e:
        log.error('mem0.add failed for %r: %s', text[:80], e)
        return False


def _all_behavior_memories() -> list:
    """Normalized [{id, text, created_at}] for the brian-behavior namespace."""
    # Read straight from Qdrant. mem0's get_all() silently caps at ~20 rows for
    # this namespace regardless of limit/version kwargs (verified 2026-08-03:
    # true count 295, get_all returned 20), which would make consolidation
    # operate on a sliver of the model. Point id == mem0 memory id, so these
    # ids are still valid for mem0.delete().
    _get_mem0()  # ensure the store is initialized
    harness = _BACKEND / 'packages' / 'harness'
    if harness.exists() and str(harness) not in sys.path:
        sys.path.insert(0, str(harness))
    from deerflow.agents.memory import mem0_store as ms
    try:
        client = ms._qdrant_client()
        flt = ms._user_filter(BEHAVIOR_USER_ID)
        points, offset = [], None
        while True:
            batch, offset = client.scroll(
                collection_name=ms.MEM0_COLLECTION, scroll_filter=flt,
                with_payload=True, with_vectors=False, limit=1000, offset=offset,
            )
            points.extend(batch)
            if offset is None:
                break
    except Exception as e:
        log.warning('Qdrant scroll failed (%s)', e)
        return []
    raw = [{
        'id': str(p.id),
        'memory': ((p.payload or {}).get('data') or '').strip(),
        'created_at': (p.payload or {}).get('created_at', ''),
    } for p in points]
    if isinstance(raw, dict):
        raw = raw.get('results', raw.get('memories', [])) or []
    out = []
    for r in raw or []:
        if isinstance(r, str):
            out.append({'id': None, 'text': r, 'created_at': ''})
        elif isinstance(r, dict):
            out.append({
                'id': r.get('id') or r.get('memory_id'),
                'text': r.get('memory') or r.get('text') or r.get('data') or '',
                'created_at': r.get('created_at') or r.get('updated_at') or '',
            })
    return [o for o in out if o['text']]


# ---------------------------------------------------------------------------
# Consolidation — the anti-bloat / anti-contradiction pass
# ---------------------------------------------------------------------------

CONSOLIDATE_PROMPT = """You are consolidating the behavioral model of Brian Mauck.
Below are the accumulated pattern sentences, oldest first, each with an index.
They have drifted: duplicates, near-duplicates, and outright contradictions.

{items}

---

Produce a clean canonical set of AT MOST {target} patterns.

RULES
1. MERGE near-duplicates into one sharper sentence. List every index merged.
2. RESOLVE contradictions — when two patterns disagree about the same situation,
   the MORE RECENT (higher index) wins. If they only look contradictory but are
   actually context-dependent, rewrite as one sentence that names both contexts.
3. DROP patterns that are generic, unfalsifiable, or blanket a person/counterparty
   wholesale rather than binding to a message type.
4. PRESERVE specificity. Never merge two specific patterns into a vague one.
5. Keep the axis tag prefix [TRIAGE] / [VOICE] / [SUBSTANCE] on every output.
6. Every input index must appear exactly once in either "replaces" or "drop".
7. Keep every output sentence under 45 words. Be terse; think briefly.

Respond with ONLY valid JSON:
{{"canonical": [{{"text": "[AXIS] sentence", "replaces": [1, 7]}}], "drop": [3, 9]}}
"""


CHUNK_TRIGGER = 60         # above this, pre-consolidate in chunks first
CONSOLIDATE_CHUNK = 40     # patterns per chunk pass (output-token bound)
MAX_CONSOLIDATE_ROUNDS = 4


def consolidate(target_max: int = BEHAVIOR_TARGET_MAX, dry_run: bool = False,
                force: bool = False) -> dict:
    """Merge duplicates + resolve contradictions in the brian-behavior namespace.

    Staged: a backfill can leave 700+ raw patterns, far too many for one
    reliable plan (the model has to account for every index), so an oversized
    namespace is pre-consolidated in chunks and then finished with one global
    pass.
    """
    mems = _all_behavior_memories()
    total = {'before': len(mems), 'after': len(mems), 'merged': 0, 'dropped': 0,
             'skipped': False, 'passes': 0}
    if not mems or not ANTHROPIC_API_KEY:
        total['skipped'] = True
        return total
    if len(mems) < CONSOLIDATE_FLOOR and not force:
        log.info('Only %d behavior facts (< %d floor) — skipping consolidation',
                 len(mems), CONSOLIDATE_FLOOR)
        total['skipped'] = True
        return total

    if dry_run:
        mems.sort(key=lambda x: str(x.get('created_at') or ''))
        preview = mems[:CONSOLIDATE_CHUNK]
        res = _consolidate_pass(preview, max(8, int(len(preview) * 0.3)), dry_run=True)
        total.update({'passes': 1, 'merged': res.get('merged', 0),
                      'dropped': res.get('dropped', 0)})
        return total

    # Iterative rounds. Chunks stay small because the synthesizer must account
    # for every index and its own thinking tokens count against max_tokens —
    # 110-item chunks truncated mid-JSON and produced no usable plan
    # (verified 2026-08-03: 8.1k output tokens for a 40-item chunk).
    rounds = 0
    while rounds < MAX_CONSOLIDATE_ROUNDS:
        mems = _all_behavior_memories()
        mems.sort(key=lambda x: str(x.get('created_at') or ''))
        if len(mems) <= target_max:
            break
        rounds += 1
        if len(mems) > CHUNK_TRIGGER:
            for i in range(0, len(mems), CONSOLIDATE_CHUNK):
                chunk = mems[i:i + CONSOLIDATE_CHUNK]
                if len(chunk) < 8:
                    continue
                res = _consolidate_pass(chunk, max(8, int(len(chunk) * 0.3)))
                total['passes'] += 1
                total['merged'] += res.get('merged', 0)
                total['dropped'] += res.get('dropped', 0)
                log.info('  round %d pass %d: %d -> %d', rounds, total['passes'],
                         res.get('before', 0), res.get('after', 0))
            log.info('Round %d done: %d facts remain', rounds,
                     len(_all_behavior_memories()))
        else:
            res = _consolidate_pass(mems, target_max)
            total['passes'] += 1
            total['merged'] += res.get('merged', 0)
            total['dropped'] += res.get('dropped', 0)
            break

    total['after'] = len(_all_behavior_memories())
    log.info('Consolidated brian-behavior: %d -> %d over %d passes',
             total['before'], total['after'], total['passes'])
    return total


def _consolidate_pass(mems: list, target_max: int, dry_run: bool = False) -> dict:
    """One LLM merge/resolve plan over `mems`, applied to mem0."""
    summary = {'before': len(mems), 'after': len(mems), 'merged': 0, 'dropped': 0,
               'skipped': False}
    if not mems or not ANTHROPIC_API_KEY:
        summary['skipped'] = True
        return summary

    items = '\n'.join('%d. %s' % (i, m['text']) for i, m in enumerate(mems, 1))

    from anthropic import Anthropic
    client = Anthropic(api_key=ANTHROPIC_API_KEY)
    try:
        resp = client.messages.create(
            model=SYNTHESIZER_MODEL, max_tokens=16000,
            messages=[{'role': 'user', 'content':
                       CONSOLIDATE_PROMPT.format(items=items, target=target_max)}],
        )
        plan = _parse_json(_text_from_response(resp))
    except Exception as e:
        log.error('Consolidation call failed: %s', e)
        summary['skipped'] = True
        return summary

    if not plan or 'canonical' not in plan:
        log.error('Consolidation returned no usable plan')
        summary['skipped'] = True
        return summary

    canonical = plan.get('canonical', []) or []
    drop_idx = set(plan.get('drop', []) or [])
    replaced_idx = set()
    for c in canonical:
        replaced_idx.update(c.get('replaces', []) or [])

    # Safety rail — never let a bad plan wipe the namespace.
    keep_ratio = len(canonical) / max(len(mems), 1)
    if keep_ratio < 0.1:
        log.error('Plan would shrink %d -> %d (<10%%) — refusing to apply',
                  len(mems), len(canonical))
        summary['skipped'] = True
        return summary

    summary['after'] = len(canonical)
    summary['merged'] = len(replaced_idx)
    summary['dropped'] = len(drop_idx)

    if dry_run:
        print('\n--- CONSOLIDATION PLAN (dry run): %d -> %d ---' % (len(mems), len(canonical)))
        for c in canonical:
            print('  KEEP  %s   <- merges %s' % (c.get('text', ''), c.get('replaces', [])))
        for d in sorted(drop_idx):
            if 1 <= d <= len(mems):
                print('  DROP  %s' % mems[d - 1]['text'][:110])
        return summary

    for c in canonical:
        write_pattern(c.get('text', ''))

    m = _get_mem0()
    for idx in sorted(replaced_idx | drop_idx):
        if not (1 <= idx <= len(mems)):
            continue
        mid = mems[idx - 1].get('id')
        if not mid:
            continue
        try:
            m.delete(mid)
        except Exception as e:
            log.warning('delete %s failed: %s', mid, e)

    log.info('Consolidated brian-behavior: %d -> %d (merged %d, dropped %d)',
             summary['before'], summary['after'], summary['merged'], summary['dropped'])
    return summary


def consolidate_if_due(force: bool = False) -> dict:
    """Weekly gate — called from dreams_cron."""
    state = load_state()
    last = state.get('last_consolidated')
    if last and not force:
        try:
            if _now() - datetime.fromisoformat(last) < timedelta(days=CONSOLIDATE_INTERVAL_DAYS):
                return {'skipped': True, 'reason': 'not due'}
        except Exception:
            pass
    res = consolidate(force=force)
    if not res.get('skipped'):
        state = load_state()
        state['last_consolidated'] = _now().isoformat()
        save_state(state)
    return res


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------


def run(since=None, max_messages: int = 150, include_slack: bool = True,
        include_ignored: bool = True, dry_run: bool = False) -> dict:
    state = load_state()
    processed = set(state.get('processed_ids', []))

    if not since:
        last = state.get('last_run')
        since = (datetime.fromisoformat(last).date().isoformat() if last
                 else (_now() - timedelta(days=2)).date().isoformat())

    log.info("Learning from Brian's actions since %s (max %d)", since, max_messages)

    obs = collect_sent(since, max_messages, processed)
    if include_ignored:
        obs += collect_ignored(since, IGNORED_SAMPLE_PER_RUN, processed)
    if include_slack:
        obs += collect_slack(since, min(150, max_messages), processed)

    summary = {'observations': len(obs), 'patterns_added': 0, 'batches': 0, 'since': since}
    if not obs:
        log.info('No new observations')
        state['last_run'] = _now().isoformat()
        save_state(state)
        return summary

    for o in obs:
        entry = {'logged_at': _now().isoformat()}
        entry.update(o)
        _append_jsonl(OBS_LOG_PATH, entry)

    for i in range(0, len(obs), BATCH_SIZE):
        batch = obs[i:i + BATCH_SIZE]
        patterns = synthesize(batch)
        summary['batches'] += 1
        log.info('  batch %d: %d observations -> %d patterns',
                 summary['batches'], len(batch), len(patterns))
        for p in patterns:
            if dry_run:
                print('  PATTERN:', p)
                summary['patterns_added'] += 1
            elif write_pattern(p):
                summary['patterns_added'] += 1

    if not dry_run:
        processed.update(o['id'] for o in obs)
        state['processed_ids'] = sorted(processed)[-20000:]
        state['last_run'] = _now().isoformat()
        state['patterns_written'] = state.get('patterns_written', 0) + summary['patterns_added']
        state['observations'] = state.get('observations', 0) + len(obs)
        save_state(state)

    log.info('Done: %d observations -> %d patterns', len(obs), summary['patterns_added'])
    return summary


def run_daily() -> dict:
    """Incremental pass for cron — small window, then weekly consolidation gate."""
    res = run(max_messages=60)
    try:
        res['consolidation'] = consolidate_if_due()
    except Exception as e:
        log.error('consolidate_if_due failed: %s', e)
    return res


def print_status() -> None:
    state = load_state()
    print('\nAction learner status')
    print('  last run          : %s' % state.get('last_run'))
    print('  last consolidated : %s' % state.get('last_consolidated'))
    print('  observations seen : %s' % state.get('observations', 0))
    print('  patterns written  : %s' % state.get('patterns_written', 0))
    print('  processed ids     : %s' % len(state.get('processed_ids', [])))
    try:
        mems = _all_behavior_memories()
        print('  mem0 brian-behavior facts: %d' % len(mems))
        axes = {}
        for m in mems:
            t = m['text']
            a = t[1:t.index(']')] if t.startswith('[') and ']' in t else 'UNTAGGED'
            axes[a] = axes.get(a, 0) + 1
        for a, n in sorted(axes.items(), key=lambda x: -x[1]):
            print('    %-10s: %d' % (a, n))
    except Exception as e:
        print('  mem0 lookup failed: %s' % e)


def main():
    ap = argparse.ArgumentParser(description="Learn behavior patterns from Brian's own actions")
    ap.add_argument('--backfill', action='store_true', help='Historical pass')
    ap.add_argument('--since', help='YYYY-MM-DD start date')
    ap.add_argument('--max', type=int, default=150, help='Max sent messages this run')
    ap.add_argument('--no-slack', action='store_true')
    ap.add_argument('--no-ignored', action='store_true')
    ap.add_argument('--dry-run', action='store_true')
    ap.add_argument('--consolidate', action='store_true')
    ap.add_argument('--force', action='store_true')
    ap.add_argument('--status', action='store_true')
    args = ap.parse_args()

    if args.status:
        print_status()
        return
    if args.consolidate:
        res = consolidate(dry_run=args.dry_run, force=args.force)
        print(json.dumps(res, indent=2))
        return

    res = run(
        since=args.since,
        max_messages=args.max if not args.backfill else max(args.max, 400),
        include_slack=not args.no_slack,
        include_ignored=not args.no_ignored,
        dry_run=args.dry_run,
    )
    print(json.dumps(res, indent=2))


if __name__ == '__main__':
    main()
