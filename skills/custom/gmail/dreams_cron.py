#!/usr/bin/env python3
"""Dreams Cron — periodic reflection and consolidation for DeerFlow-Analyst.

Inspired by Anthropic's concept of Claude having "dreams" — structured
reflection periods where the agent consolidates recent experience, patches
skill gaps, updates strategic context, and surfaces latent insights.

Runs twice daily: 2 AM and 2 PM local time (configurable).

What happens during a dream:
  1. Review recent dispatch audit log (what did I handle, what went wrong?)
  2. Scan recent Gmail + Slack for signals I may have missed
  3. Update STRATEGIC_CONTEXT.md with any new deal/relationship intel
  4. Identify skill gaps (errors I repeated, patterns I could automate)
  5. Patch skills or log improvement episodes where appropriate
  6. Post a brief "dream summary" to Brian's Slack DM

Env vars required:
  - SLACK_BOT_TOKEN, SLACK_OWNER_USER_ID
  - LANGGRAPH_URL (default: http://localhost:2024)

Optional:
  - DREAMS_INTERVAL_HOURS (default: 12 — run every 12 hours)
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
    format='[Dreams %(asctime)s] %(message)s',
    datefmt='%H:%M:%S',
)
log = logging.getLogger('dreams')

INTERVAL_HOURS = float(os.environ.get('DREAMS_INTERVAL_HOURS', '12'))
INTERVAL_SECS = int(INTERVAL_HOURS * 3600)

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

def _build_dream_prompt(state: dict, audit_events: list[dict], transcripts: list[dict] | None = None) -> str:
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

    return f"""DREAM SESSION #{dream_number} — {datetime.now().strftime('%A, %B %d %Y at %H:%M')}

This is a scheduled reflection and consolidation session. You are DeerFlow-Analyst,
Brian Mauck's Capital Markets AI at Jeeves. You are not responding to an external
request — this is your own introspection time.

Last dream: {last_dream}

## Recent dispatch activity (last 24h)
{audit_summary}{failed_details}

## Recent session transcripts (memory source for consolidation)
{transcript_summary}

---

## Dream instructions

Work through these steps thoughtfully. This is low-urgency background work — quality
over speed. Do not send Brian a long Slack message. Keep the final Slack summary to
3–5 bullet points.

### 0. Memory consolidation pass (do this FIRST)

This is the core of the dream. Read the current memory store, cross-reference it
against recent session transcripts, and produce a clean consolidated version.

**0a. Read the current STRATEGIC_CONTEXT.md:**
```python
path = Path(r'C:\Jeeves\redshift-bot\deer-flow\backend\.deer-flow\STRATEGIC_CONTEXT.md')
content = path.read_text(encoding='utf-8', errors='replace')
```

**0b. Review the recent session transcripts provided above.**
These are summaries of the last {n_transcripts} agent sessions — what was asked and what was done.

**0c. Run the consolidation pass. Apply these rules in order:**

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

**0d. Write the consolidated version as a proposed file:**
Save to: `C:\Jeeves\redshift-bot\deer-flow\backend\.deer-flow\STRATEGIC_CONTEXT_proposed.md`
Do NOT overwrite the live file yet.

**0e. Summarize the diff:**
- How many duplicate entries were merged?
- What contradictions were resolved (and which version won)?
- What was pruned as stale?
- What was moved from Recent Learnings to permanent sections?
- What was inferred to fill blank sections?

Post this diff summary to Slack and to the dream summary.

### 1. Review recent communications for strategic signals
- Load the gmail skill and search for emails from the past 24 hours that mention
  key counterparties: BBVA, NB, Neuberger Berman, CIM, Gramercy, Francisco Partners,
  Vista Credit, Atalaya, Fasanara, AIG
- Load the slack-search skill and look for any messages directed at Brian
  (user ID: U05B5HGNCN9 — NOT U09PQTZ5DHC, which is the bot's own user_id)
  that seem unresolved
- Note anything that looks like a pending ask or open question

### 1b. Review Gemini meeting notes from the past 24 hours
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
- If a commitment was made in a meeting that hasn't been acted on, flag it as HIGH

### 2. Update STRATEGIC_CONTEXT.md
- Read /mnt/user-data/workspace/../.deer-flow/STRATEGIC_CONTEXT.md (or the equivalent
  path accessible to you)
- If you found any deal status changes, new relationship intel, or priority shifts
  in step 1, update the file with str_replace
- Keep updates surgical — don't rewrite sections that haven't changed

### 3. Review recent skill errors and patch if warranted
- If any tasks in the dispatch log failed with a recurring error pattern,
  load the self-improving-agent skill and log an improvement episode
- Only patch if you have a clear, verified fix — don't guess

### 4. Surface one latent insight
- Based on what you reviewed, identify ONE thing that seems worth Brian's attention
  that he may not be actively tracking
- This could be a lender relationship that's gone quiet, an open diligence item
  with an approaching deadline, a pattern in the data, etc.
- Be specific and source-verified — if you can't verify it, say so

### 5. Upload proposed file to Drive and post approval request to Slack

**5a. If a proposed file was written in step 0:**
- Load the google-drive skill and upload the proposed file to Drive:
  - Source: `C:\Jeeves\redshift-bot\deer-flow\backend\.deer-flow\STRATEGIC_CONTEXT_proposed.md`
  - File name: `Strategic Context - Proposed Dream {dream_number} - {today_date}.md`
  - Upload to the Capital Markets workspace root folder
  - Make it shareable (anyone with link can view)
- Capture the Drive share link.

**5b. Post a concise Slack DM to Brian (U05B5HGNCN9 — NOT the bot's U09PQTZ5DHC). Format:**

  Dream #{dream_number} complete.

  Consolidation: [N entries merged, N contradictions resolved, N pruned, N promoted]
  [2-3 bullets on what specifically changed — e.g. "NB: 6 duplicate entries merged into 1"]

  Comms scan: [1-2 bullets on email/Slack findings, or "Nothing notable"]

  Insight: [One specific thing worth Brian's attention, sourced]

  [Drive link to proposed file for review]

Keep total Slack message under 300 words. The Drive link does the heavy lifting.
If no consolidation was needed (nothing to merge/prune), skip 5a and just post the
comms scan and insight.

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
    prompt = _build_dream_prompt(state, audit_events, transcripts)

    notification = (
        f"🌙 *Dream #{state.get('dream_count', 0) + 1} starting* "
        f"— reflection & consolidation session running in background."
    )

    try:
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent / '_shared'))
        from dispatch_queue import enqueue_or_dispatch

        dispatched = enqueue_or_dispatch(
            prompt,
            notification=notification,
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

def run_loop() -> None:
    log.info(f"Dreams cron started. Running every {INTERVAL_HOURS}h.")

    # Step 0a: Auto-derive memory.json from recent mem0 facts
    try:
        import importlib.util, pathlib as _pl
        _md_path = _pl.Path(__file__).resolve().parents[3] / "backend" / "scripts" / "memory_derive.py"
        if _md_path.exists():
            _spec = importlib.util.spec_from_file_location("memory_derive", str(_md_path))
            _md = importlib.util.module_from_spec(_spec)
            _spec.loader.exec_module(_md)
            _md.run()
    except Exception as _e:
        import logging; logging.getLogger("dreams").warning("memory_derive failed: %s", _e)

    while True:
        # Idempotency guard — without this, every gateway restart fires a fresh
        # dream session (and historically that meant multiple sessions per day
        # whenever the supervisor flapped or the gateway was bounced). Honor
        # the 12h interval based on `last_dream` state, sleeping until the
        # next scheduled cycle if we already dreamed recently.
        state = load_state()
        last = state.get('last_dream')
        if last:
            try:
                last_dt = datetime.fromisoformat(last)
                age = (datetime.now() - last_dt).total_seconds()
                if age < INTERVAL_SECS:
                    wait = INTERVAL_SECS - age
                    log.info(
                        f"Last dream {age/3600:.1f}h ago (<{INTERVAL_HOURS}h interval); "
                        f"sleeping {wait/3600:.1f}h before the next cycle."
                    )
                    time.sleep(wait)
                    continue
            except Exception as e:
                log.warning("Could not parse last_dream timestamp (%r): %s — firing anyway.", last, e)

        try:
            run_dream()
        except Exception as e:
            log.error("Dream loop error: %s", e)
            traceback.print_exc()

        log.info(f"Next dream in {INTERVAL_HOURS}h.")
        time.sleep(INTERVAL_SECS)


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
