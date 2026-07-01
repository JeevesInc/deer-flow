#!/usr/bin/env python3
"""Dreams Cron — nightly reflection and consolidation for DeerFlow-Analyst.

Inspired by Anthropic's concept of Claude having "dreams" — structured
reflection periods where the agent consolidates recent experience, patches
skill gaps, and improves its own behavior.

Runs ONCE A DAY, overnight Pacific time (default 02:00–08:00 PST window).

What happens during a dream (reworked 2026-06-10 per Brian's feedback —
dreams improve memory + behavior, they do NOT brief Brian):
  1. Behavior & infra review of the previous day's work: full Slack-thread
     conversations (including Brian's corrections), tool errors, logged
     episodes, and SLL scores are injected as a "day review pack". The agent
     identifies problems, fixes what it can itself (skill patches, episodes,
     artifact-library updates), and collects infra suggestions it cannot
     apply alone.
  2. Memory consolidation of STRATEGIC_CONTEXT.md — silent.
  3. Comms scan for strategic-context updates — silent.
  4. ONE short Slack DM to Brian ONLY if there are concrete problems +
     improvement suggestions. No consolidation stats, no insights, no links.
     Nothing notable -> no message at all.

Env vars required:
  - SLACK_BOT_TOKEN, SLACK_OWNER_USER_ID
  - LANGGRAPH_URL (default: http://localhost:2024)

Optional:
  - DREAMS_HOUR_PST (default: 2 — start of the overnight run window)
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
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / '_shared'))
from env_loader import load_env
load_env()

logging.basicConfig(
    level=logging.INFO,
    format='[Dreams %(asctime)s] %(message)s',
    datefmt='%H:%M:%S',
)
log = logging.getLogger('dreams')

PACIFIC = ZoneInfo('America/Los_Angeles')
DREAM_HOUR_PST = int(os.environ.get('DREAMS_HOUR_PST', '2'))
WINDOW_END_HOUR_PST = 8   # if the gateway was down all night, skip to next night
CHECK_INTERVAL_SECS = 600

# ------------------------------------------------------------------ #
# State                                                                #
# ------------------------------------------------------------------ #

def _state_path() -> Path:
    here = Path(__file__).resolve()
    return here.parents[3] / 'backend' / '.deer-flow' / '_dreams_state.json'


def load_state() -> dict:
    p = _state_path()
    if p.exists():
        with open(p) as f:
            return json.load(f)
    return {'last_dream': None, 'dream_count': 0}


def save_state(state: dict) -> None:
    p = _state_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    state['last_dream'] = datetime.now().isoformat()
    with open(p, 'w') as f:
        json.dump(state, f, indent=2)


# ------------------------------------------------------------------ #
# Day review pack — yesterday's work, extracted for behavior review    #
# ------------------------------------------------------------------ #

_CURRENT_DATE_TAG = re.compile(r'<current_date>[^<]*</current_date>\s*')
_ERROR_MARKERS = ('error', 'traceback', 'exception', 'failed', 'permission denied')


def _deer_flow_dir() -> Path:
    return Path(__file__).resolve().parents[3] / 'backend' / '.deer-flow'


def _msg_text(content) -> str:
    """Flatten a message content (str or block list) to plain text."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for b in content:
            if isinstance(b, dict) and b.get('type') == 'text':
                parts.append(b.get('text', ''))
        return '\n'.join(parts)
    return str(content)


def _extract_thread_review(saver, thread_id: str) -> dict | None:
    """Pull user messages, AI turn count, and tool errors from one thread."""
    try:
        tup = saver.get_tuple({"configurable": {"thread_id": thread_id}})
        if not tup:
            return None
        msgs = tup.checkpoint.get('channel_values', {}).get('messages', [])
    except Exception:
        return None

    user_msgs, tool_errors, ai_turns = [], [], 0
    for m in msgs:
        mtype = getattr(m, 'type', None)
        if mtype == 'human':
            text = _CURRENT_DATE_TAG.sub('', _msg_text(m.content)).strip()
            # Skip injected conversation summaries
            if text.startswith('Here is a summary of the conversation'):
                continue
            if text:
                user_msgs.append(text[:300])
        elif mtype == 'ai':
            ai_turns += 1
        elif mtype == 'tool':
            head = _msg_text(m.content)[:200]
            if any(k in head.lower() for k in _ERROR_MARKERS):
                tool_errors.append(head[:140])

    if not user_msgs:
        return None
    return {
        'thread_id': thread_id,
        'user_msgs': user_msgs[:30],
        'ai_turns': ai_turns,
        'tool_errors': tool_errors[:6],
        'n_tool_errors': len(tool_errors),
    }


def _yesterdays_slack_threads(hours: int = 26) -> list[str]:
    """Thread IDs for Slack conversations created in the last N hours."""
    store_path = _deer_flow_dir() / 'channels' / 'store.json'
    try:
        with open(store_path, encoding='utf-8') as f:
            store = json.load(f)
    except Exception:
        return []
    cutoff = time.time() - hours * 3600
    out = []
    for v in store.values():
        if isinstance(v, dict) and v.get('thread_id') and (v.get('updated_at') or v.get('created_at') or 0) >= cutoff:
            out.append(v['thread_id'])
    return out[-15:]


def _recent_episodes(hours: int = 26) -> list[str]:
    """Episodes logged in the last N hours (self-improving-agent store)."""
    ep_dir = Path(__file__).resolve().parents[2] / 'public' / 'self-improving-agent' / 'memory' / 'episodic'
    if not ep_dir.exists():
        return []
    cutoff = time.time() - hours * 3600
    lines = []
    try:
        for f in sorted(ep_dir.glob('*.json')):
            if f.stat().st_mtime < cutoff:
                continue
            ep = json.loads(f.read_text(encoding='utf-8', errors='replace'))
            lines.append(f"[{ep.get('skill', '?')}] {ep.get('situation', '')[:100]} -> {ep.get('lesson', '')[:220]}")
    except Exception:
        pass
    return lines[-15:]


def _recent_sll(hours: int = 26) -> list[str]:
    """SLL-scored turns from the last N hours (task + Brian's reply)."""
    log_path = _deer_flow_dir() / 'sll' / 'log.jsonl'
    if not log_path.exists():
        return []
    cutoff = datetime.now().astimezone() - timedelta(hours=hours)
    lines = []
    try:
        with open(log_path, encoding='utf-8', errors='replace') as f:
            for line in f:
                try:
                    rec = json.loads(line)
                    ts = datetime.fromisoformat(rec.get('scored_at', '').replace('Z', '+00:00'))
                    if ts < cutoff:
                        continue
                    entry = f"task: {rec.get('task', '')[:120]}"
                    if rec.get('composite') is not None:
                        entry += f" | score: {rec['composite']}"
                    if rec.get('user_reply'):
                        entry += f" | Brian replied: {str(rec['user_reply'])[:200]}"
                    lines.append(entry)
                except Exception:
                    continue
    except Exception:
        pass
    return lines[-15:]


def _build_day_review() -> str:
    """Assemble the previous day's work into a review pack for the dream.

    Mirrors the manual review process: full user-side conversation (Brian's
    corrections verbatim), tool error patterns, episodes logged, SLL signal.
    Best-effort — any missing source degrades to a note, never an exception.
    """
    sections = []

    # 1. Slack-thread conversations from checkpoints.db (read-only)
    try:
        import sqlite3
        from langgraph.checkpoint.sqlite import SqliteSaver
        db = _deer_flow_dir() / 'checkpoints.db'
        conn = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True, check_same_thread=False)
        try:
            saver = SqliteSaver(conn)
            reviews = []
            for tid in _yesterdays_slack_threads():
                r = _extract_thread_review(saver, tid)
                if r:
                    reviews.append(r)
        finally:
            conn.close()

        if reviews:
            parts = []
            for r in reviews:
                parts.append(f"--- Thread {r['thread_id'][:8]} ({r['ai_turns']} AI turns, {r['n_tool_errors']} tool errors) ---")
                for um in r['user_msgs']:
                    parts.append(f"  BRIAN: {um}")
                for te in r['tool_errors']:
                    parts.append(f"  [tool error] {te}")
            sections.append("### Yesterday's Slack conversations (Brian's messages verbatim + tool errors)\n" + '\n'.join(parts))
        else:
            sections.append("### Yesterday's Slack conversations\n(none found in the last 26h)")
    except Exception as e:
        sections.append(f"### Yesterday's Slack conversations\n(unavailable: {e})")

    eps = _recent_episodes()
    sections.append("### Episodes logged yesterday (lessons the agent already wrote down)\n"
                    + ('\n'.join(f"  - {e}" for e in eps) if eps else "(none)"))

    sll = _recent_sll()
    sections.append("### SLL-scored turns yesterday\n"
                    + ('\n'.join(f"  - {s}" for s in sll) if sll else "(none)"))

    pack = '\n\n'.join(sections)
    if len(pack) > 14000:
        pack = pack[:14000] + '\n... (pack truncated at 14k chars)'
    return pack


# ------------------------------------------------------------------ #
# Session transcript fetcher                                           #
# ------------------------------------------------------------------ #

def _fetch_recent_transcripts(n: int = 10) -> list[dict]:
    # Fetch the last N completed LangGraph session transcripts for consolidation.
    import httpx
    LG = os.environ.get('LANGGRAPH_URL', 'http://localhost:2024')
    transcripts = []
    try:
        r = httpx.post(
            LG + "/threads/search",
            json={"limit": n, "status": "idle"},
            timeout=15,
        )
        if r.status_code != 200:
            return []
        threads = sorted(r.json(), key=lambda x: x.get('updated_at', ''), reverse=True)
        for t in threads[:n]:
            tid = t.get('thread_id')
            try:
                sr = httpx.get(LG + f"/threads/{tid}/state", timeout=15)
                if sr.status_code != 200:
                    continue
                msgs = sr.json().get('values', {}).get('messages', [])
                # Grab first human message (topic) and last AI message (outcome)
                first_human = next(
                    (m.get('content', '') for m in msgs
                     if (m.get('type') or m.get('role')) == 'human'),
                    ''
                )
                last_ai = ''
                for m in reversed(msgs):
                    if (m.get('type') or m.get('role')) == 'ai':
                        c = m.get('content', '')
                        if isinstance(c, list):
                            c = ' '.join(p.get('text', '') for p in c if isinstance(p, dict))
                        if c.strip():
                            last_ai = c.strip()
                            break
                if first_human or last_ai:
                    transcripts.append({
                        'thread_id': tid,
                        'updated_at': t.get('updated_at', '')[:19],
                        'topic': str(first_human)[:300],
                        'outcome': str(last_ai)[:600],
                    })
            except Exception:
                continue
    except Exception as e:
        log.warning("Could not fetch transcripts: %s", e)
    return transcripts


# ------------------------------------------------------------------ #
# Audit log reader                                                     #
# ------------------------------------------------------------------ #

def _read_recent_audit(hours: int = 24) -> list[dict]:
    """Read dispatch audit events from the last N hours."""
    here = Path(__file__).resolve()
    audit_path = here.parents[3] / 'backend' / '.deer-flow' / 'dispatch_audit.jsonl'
    if not audit_path.exists():
        return []

    cutoff = datetime.now() - timedelta(hours=hours)
    events = []
    try:
        with open(audit_path) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                    ts_str = record.get('ts', '')
                    if ts_str:
                        ts = datetime.fromisoformat(ts_str.replace('Z', '+00:00'))
                        # naive comparison — strip tz if present
                        ts_naive = ts.replace(tzinfo=None)
                        if ts_naive >= cutoff:
                            events.append(record)
                except Exception:
                    pass
    except Exception as e:
        log.warning("Could not read audit log: %s", e)
    return events


# ------------------------------------------------------------------ #
# Prompt builder                                                       #
# ------------------------------------------------------------------ #

def _build_dream_prompt(state: dict, audit_events: list[dict], transcripts: list[dict] | None = None, day_review: str = '') -> str:
    from datetime import datetime, timedelta
    dream_number = state.get('dream_count', 0) + 1
    last_dream = state.get('last_dream', 'never')
    lookback_date = (datetime.now() - timedelta(hours=24)).strftime('%Y/%m/%d')

    # Summarize recent dispatch activity
    completed = [e for e in audit_events if e.get('event') == 'completed']
    failed    = [e for e in audit_events if e.get('event') == 'failed']
    rejected  = [e for e in audit_events if e.get('event') == 'rejected_capacity']
    accepted  = [e for e in audit_events if e.get('event') == 'accepted']

    audit_summary = (
        f"- {len(accepted)} tasks dispatched\n"
        f"- {len(completed)} completed\n"
        f"- {len(failed)} failed\n"
        f"- {len(rejected)} rejected (capacity)\n"
    )

    failed_details = ''
    if failed:
        failed_details = 'Failed tasks:\n'
        for e in failed[:5]:
            failed_details += (
                f"  - [{e.get('category','?')}] {e.get('error','unknown error')[:120]}\n"
            )

    transcript_summary = ''
    if transcripts:
        transcript_summary = f'Recent sessions ({len(transcripts)}):\n'
        for t in transcripts[:8]:
            topic_short = t.get('topic', '')[:120].replace('\n', ' ')
            outcome_short = t.get('outcome', '')[:200].replace('\n', ' ')
            transcript_summary += f"  [{t.get('updated_at','?')[:10]}] {topic_short}\n"
            if outcome_short:
                transcript_summary += f"    -> {outcome_short}\n"
    else:
        transcript_summary = 'No session transcripts available.'
    n_transcripts = len(transcripts) if transcripts else 0
    today_date = datetime.now().strftime('%Y%m%d')

    return f"""DREAM SESSION #{dream_number} — {datetime.now().strftime('%A, %B %d %Y at %H:%M')} (nightly)

This is a scheduled reflection and consolidation session. You are DeerFlow-Analyst,
Brian Mauck's Capital Markets AI at Jeeves. You are not responding to an external
request — this is your own introspection time.

Last dream: {last_dream}

**Brian's standing feedback on dreams (2026-06-10): dreams exist to improve YOUR
memory and behavior, not to brief him. He stopped reading the old dream summaries
because they were noise. The ONLY thing you may send him is the short
problems/improvements message in the final step — and only if you actually found
something. Everything else in this session is silent self-maintenance.**

## A. Day review pack — yesterday's work (conversations, corrections, errors)
{day_review or '(no day review pack available)'}

## B. Recent dispatch activity (last 24h)
{audit_summary}{failed_details}

## C. Recent session transcripts (memory source for consolidation)
{transcript_summary}

---

## Dream instructions

Work through these steps thoughtfully. This is low-urgency background work — quality
over speed.

### 1. BEHAVIOR & INFRA REVIEW (most important step — do it first, carefully)

Review pack A like a staff engineer auditing yesterday's agent work. Look for:

- **Correction cycles**: places where Brian had to correct you, especially more than
  once on the same artifact, or where his tone escalated. For each, name the root
  cause: tool defect, missing/ignored knowledge, or behavior (e.g. point-fixing one
  instance instead of sweeping the whole artifact, not verifying before "done").
- **Tool error patterns**: recurring exceptions, path problems, API misuse, retries
  that burned turns. A tool you had to fight is an infra problem, not your problem.
- **Lessons that did not stick**: compare the episodes/SLL lessons in pack A against
  what actually happened — if a logged lesson was violated again, the lesson text or
  its injection point needs fixing, not another copy of the same lesson.

Then split your findings in two:

**(a) Fix-it-yourself items** — apply NOW, silently:
  - Patch the relevant SKILL.md (hard rules section) when a rule was missing or vague.
  - Log a self-improving-agent episode only if it is genuinely NEW — never re-log a
    lesson that already exists; improve the existing one instead.
  - If a delivery format was corrected (e.g. how an artifact should look), update the
    canonical definition in cfo-org-kb `diligence/ARTIFACTS.md` and push:
    `cd /mnt/skills/custom/cfo-org-kb && git add -A && git commit -m "..." && git push origin main`

**(b) Infra suggestions for Brian** — things you cannot or should not change alone:
  harness/gateway code changes, new tool capabilities, cron changes, scope additions,
  process decisions. Be concrete: name the file/component, the change, and what
  failure it would have prevented yesterday. These go in the final message.

### 2. Memory consolidation pass (SILENT — nothing from this step goes to Brian)

This is the core of the dream. Read the current memory store, cross-reference it
against recent session transcripts, and produce a clean consolidated version.

**2a. Read the current STRATEGIC_CONTEXT.md:**
```python
path = Path(r'C:\Jeeves\redshift-bot\deer-flow\backend\.deer-flow\STRATEGIC_CONTEXT.md')
content = path.read_text(encoding='utf-8', errors='replace')
```

**2b. Review the recent session transcripts provided above.**
These are summaries of the last {n_transcripts} agent sessions — what was asked and what was done.

**2c. Run the consolidation pass. Apply these rules in order:**

1. **Deduplicate** — the Recent Learnings section has many entries for the same entity
   (NB, BBVA, CIM, Francisco Partners etc.) added incrementally over days. Merge all
   entries for the same counterparty/topic into a single canonical entry that contains
   all unique facts.

2. **Resolve contradictions** — where two entries say different things about the same
   fact, keep the most recent one. Flag it with [Updated YYYY-MM-DD].

3. **Absolutize dates** — convert any relative references ("last week", "yesterday",
   "recently", "a few days ago") to absolute dates based on context clues from the
   surrounding entries.

4. **Prune stale entries** — remove facts that are clearly superseded (e.g. if a deal
   closed, remove the "in negotiation" entry). Keep the final state, not the history.

5. **Promote to correct sections** — if something in Recent Learnings is now stable
   enough to be a standing fact, move it to Active Facilities, Key Stakeholders, or
   Business Context. Recent Learnings should be truly recent, not a permanent archive.

6. **Fill blanks** — Current Priorities and Strategic Initiatives are empty. Based on
   what you know from session history, populate them with Brian's actual current focus.
   Mark anything you inferred as [Inferred — confirm].

7. **Keep it under 300 lines** — trim where possible without losing facts.

**2d. Write the consolidated version as a proposed file:**
Save to: `C:\Jeeves\redshift-bot\deer-flow\backend\.deer-flow\STRATEGIC_CONTEXT_proposed.md`
Do NOT overwrite the live file yet.

**2e. Apply it:** review your own proposed version once, then apply the consolidation
directly to STRATEGIC_CONTEXT.md. Do NOT post any diff summary, stats, or Drive link
to Slack — this step is silent self-maintenance.

### 3. Comms scan -> strategic context updates (SILENT)
- Load the gmail skill and search for emails from the past 24 hours that mention
  key counterparties: BBVA, NB, Neuberger Berman, CIM, Gramercy, Francisco Partners,
  Vista Credit, Atalaya, Fasanara, AIG
- Load the slack-search skill and look for any messages directed at Brian
  (user ID: U05B5HGNCN9 — NOT U09PQTZ5DHC, which is the bot's own user_id)
  that seem unresolved
- Note anything that looks like a pending ask or open question
- This feeds STRATEGIC_CONTEXT.md only. Do NOT message Brian about open asks — the
  EOD review cron already covers that; duplicating it here is the noise he complained about.

### 3b. Review Gemini meeting notes from the past 24 hours (SILENT, same purpose)
Gemini for Google Meet auto-saves meeting summaries and action items to Google Drive
as Docs, and also emails them from meet-recordings-noreply@google.com.

Search Gmail for Gemini-generated note emails first (most reliable):
```
from:meet-recordings-noreply@google.com after:{lookback_date} subject:"Notes from"
```
where lookback_date = yesterday's date in YYYY/MM/DD format.

Also try the Gmail search: `subject:"Notes from your meeting" newer_than:1d`

For each set of meeting notes:
- Read the full content (follow the Drive link in the email body or use gmail_tool read)
- Extract any action items explicitly assigned to Brian or Jeeves
- Extract open questions left unresolved in the meeting
- Flag any commitments Brian made ("I'll send", "we'll share", "I'll follow up")
- Note anything relevant to active lender workstreams (BBVA, CIM, NB, Gramercy, CBIZ)
- Fold what you learn into STRATEGIC_CONTEXT.md with surgical str_replace edits —
  don't rewrite sections that haven't changed. No messages to Brian from this step.

### 4. Final message to Brian — ONLY if step 1 found real problems

If (and only if) the behavior/infra review produced concrete problems or suggestions,
post ONE short Slack DM to Brian (U05B5HGNCN9 — NOT the bot's U09PQTZ5DHC):

  🌙 Dream review — {{YYYY-MM-DD}}

  *Problems from yesterday:*
  - [problem — root cause, one line each, max 4]

  *Suggested infra improvements:*
  - [concrete change: component, what, what it prevents — max 4]

  *Self-applied:* [one line: skill/memory/artifact patches you already made, or "none"]

HARD RULES for this message:
- Under 200 words. No consolidation stats, no comms-scan findings, no "insights",
  no Drive links, no open-items lists. Those were the noise Brian stopped reading.
- If yesterday's work was clean and you have no suggestions, send NOTHING. A silent
  dream is a successful dream. Never send "Dream #N complete" status messages.

**IMPORTANT: Apply consolidation changes directly to STRATEGIC_CONTEXT.md without asking Brian to approve or discard. Just do it.**
"""


# ------------------------------------------------------------------ #
# Dispatch                                                             #
# ------------------------------------------------------------------ #

def run_dream() -> None:
    log.info("Starting dream session...")
    state = load_state()
    audit_events = _read_recent_audit(hours=24)

    transcripts = _fetch_recent_transcripts(n=10)
    log.info("Fetched %d recent session transcripts for consolidation.", len(transcripts))
    day_review = _build_day_review()
    log.info("Day review pack: %d chars.", len(day_review))
    prompt = _build_dream_prompt(state, audit_events, transcripts, day_review=day_review)

    try:
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent / '_shared'))
        from dispatch_queue import enqueue_or_dispatch

        # notification=None: dreams are silent — the only message Brian ever
        # sees is the problems/improvements DM the agent itself may send.
        dispatched = enqueue_or_dispatch(
            prompt,
            notification=None,
            category="Dream",
            source_id=f"dream-{datetime.now().strftime('%Y%m%d-%H%M')}",
            source_metadata={"dream_number": state.get('dream_count', 0) + 1},
        )
        if dispatched:
            state['dream_count'] = state.get('dream_count', 0) + 1
            log.info("Dream session dispatched successfully.")
        else:
            log.warning("Dream rejected — agent at capacity. Will retry next cycle.")
    except Exception as e:
        log.error("Dream dispatch failed: %s", e)
        traceback.print_exc()

    save_state(state)


# ------------------------------------------------------------------ #
# Loop                                                                 #
# ------------------------------------------------------------------ #

def _dreamed_today_pst(state: dict) -> bool:
    last = state.get('last_dream')
    if not last:
        return False
    try:
        last_dt = datetime.fromisoformat(last)
        if last_dt.tzinfo is None:
            last_dt = last_dt.astimezone()  # interpret naive timestamps as local
        return last_dt.astimezone(PACIFIC).date() == datetime.now(PACIFIC).date()
    except Exception as e:
        log.warning("Could not parse last_dream timestamp (%r): %s", last, e)
        return False


def run_loop() -> None:
    log.info(
        "Dreams cron started. Once daily, overnight Pacific "
        f"(window {DREAM_HOUR_PST:02d}:00-{WINDOW_END_HOUR_PST:02d}:00 PST/PDT)."
    )

    # Auto-derive memory.json from recent mem0 facts — at most once per Pacific
    # day, and in an isolated SUBPROCESS. Running it inline (exec_module + run())
    # on this cron daemon thread fired a multi-fact mem0 read + a blocking Haiku
    # call on EVERY gateway startup, contending for the GIL and starving the
    # gateway's asyncio loop during the cron-startup burst — a contributor to the
    # 2026-06-18 /livez stalls + supervisor restart spiral. A subprocess can't
    # touch our GIL, and the daily guard stops every restart from re-deriving.
    try:
        import sys as _sys, subprocess as _sp, pathlib as _pl
        _md_path = _pl.Path(__file__).resolve().parents[3] / "backend" / "scripts" / "memory_derive.py"
        _st = load_state()
        _last_derive = _st.get("last_derive")
        _derived_today = False
        if _last_derive:
            try:
                _ld = datetime.fromisoformat(_last_derive)
                if _ld.tzinfo is None:
                    _ld = _ld.astimezone()
                _derived_today = _ld.astimezone(PACIFIC).date() == datetime.now(PACIFIC).date()
            except Exception:
                _derived_today = False
        if _md_path.exists() and not _derived_today:
            _sp.run([_sys.executable, str(_md_path)], timeout=180, cwd=str(_md_path.parent))
            # Stamp the derive marker directly — NOT via save_state(), which would
            # set last_dream and make the bot think it already dreamed today.
            _st["last_derive"] = datetime.now().isoformat()
            _sp_path = _state_path()
            _sp_path.parent.mkdir(parents=True, exist_ok=True)
            with open(_sp_path, "w") as _f:
                json.dump(_st, _f, indent=2)
    except Exception as _e:
        import logging; logging.getLogger("dreams").warning("memory_derive failed: %s", _e)

    while True:
        # Once-a-day overnight schedule, idempotent across gateway restarts:
        # fire only inside the PST window, and only if we haven't dreamed
        # today (PST date). If the gateway was down for the whole window,
        # skip to the next night rather than dreaming mid-day.
        now_pst = datetime.now(PACIFIC)
        in_window = DREAM_HOUR_PST <= now_pst.hour < WINDOW_END_HOUR_PST

        if in_window and not _dreamed_today_pst(load_state()):
            try:
                run_dream()
            except Exception as e:
                log.error("Dream loop error: %s", e)
                traceback.print_exc()

        time.sleep(CHECK_INTERVAL_SECS)


if __name__ == '__main__':
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    if hasattr(sys.stderr, 'reconfigure'):
        sys.stderr.reconfigure(encoding='utf-8', errors='replace')

    import argparse
    parser = argparse.ArgumentParser(description='Dreams Cron — reflection and consolidation')
    parser.add_argument('mode', nargs='?', choices=['once'],
                        help='Run one dream session instead of looping')
    args = parser.parse_args()

    if args.mode == 'once':
        run_dream()
    else:
        run_loop()
