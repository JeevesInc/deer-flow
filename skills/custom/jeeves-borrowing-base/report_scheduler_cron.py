#!/usr/bin/env python3
"""Scheduled Report Cron — auto-generates recurring reports via the agent.

Schedule:
  - 2nd of each month, 8am+:     Portfolio report for the 1st (once per month)
  - Every weekday, 8am+:         US Borrowing Base (yesterday's date, for dashboard)
  - Every day, 8:30am+ PST:      MX Borrowing Base (yesterday's date)
  - Mondays, 8:30am+ PT:         NB Borrowing Base (as of the prior Sunday)
  - Mondays, 9:30am+ PT:         Receivable balance forecast
  - Mondays, 10:30am+ PT:        AIG/Aronova TEX invoice file -> Brian's DM (no email)
  - Weekdays only, hourly 8am+:  SOFOM distribution tape reply to Axel

Mechanism:
  Creates a LangGraph thread, starts a run, polls until completion,
  then posts the result to Slack DM.  Portfolio report and weekly BB run
  in background threads so the main loop never blocks.

Env vars:
  - SLACK_BOT_TOKEN, SLACK_OWNER_USER_ID
  - LANGGRAPH_URL          (default: http://localhost:2024)
  - REPORT_SCHEDULER_INTERVAL  (default: 3600s)
  - MAX_TASK_SECONDS       (default: 5400s / 90 min)
"""

import json
import logging
import os
import re
import sys
import threading
import time
import traceback
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / '_shared'))
from env_loader import load_env
load_env()

logging.basicConfig(
    level=logging.INFO,
    format='[ReportScheduler %(asctime)s] %(message)s',
    datefmt='%H:%M:%S',
)
log = logging.getLogger('report_scheduler')

CHECK_INTERVAL   = int(os.environ.get('REPORT_SCHEDULER_INTERVAL', '3600'))  # 1 hour
LANGGRAPH_URL    = os.environ.get('LANGGRAPH_URL', 'http://localhost:2024')
MAX_TASK_SECONDS = int(os.environ.get('MAX_TASK_SECONDS', str(90 * 60)))  # 90 min default


# ---------------------------------------------------------------------------
# State  (thread-safe via _state_lock)
# ---------------------------------------------------------------------------

_state_lock = threading.Lock()


def _state_path():
    backend = Path(__file__).resolve().parent.parent.parent.parent / 'backend' / '.deer-flow'
    os.makedirs(backend, exist_ok=True)
    return str(backend / '_report_scheduler_state.json')


def load_state() -> dict:
    path = _state_path()
    if os.path.exists(path):
        with open(path) as f:
            return json.load(f)
    return {}


def save_state(state: dict):
    with open(_state_path(), 'w') as f:
        json.dump(state, f, indent=2)


def _reconcile_stale_running(today_str: str):
    """Clear + alert on any '_running' state left over from a killed process.

    _launch_background's wrapper only marks a task '{date}' (done) inside a
    `finally` -- but that only runs if the *thread* survives to completion.
    If the whole scheduler process is killed/restarted mid-run (deploy, OOM,
    crash), the thread dies with it: nothing survives to post a Slack alert,
    and the state file is left stuck at '{today}_running' forever for that
    day. Since _state_completed_today() treats any '_running' value as
    "already handled", this silently blocks every retry for the rest of the
    day -- the run just vanishes with no message, no error, nothing.

    Call this once when the scheduler (re)starts, before entering the loop.
    Clears any '_running' flags so the task is free to refire on the next
    matching schedule window, and tells Brian what was orphaned.
    """
    with _state_lock:
        state = load_state()
        changed = False
        for key, val in list(state.items()):
            if not isinstance(val, str) or not val.endswith('_running'):
                continue
            date_part = val[:-len('_running')]
            label = key.replace('last_', '').replace('_', ' ')
            log.warning(f"Orphaned running state on startup: {key}={val}")
            _post_slack(
                f":rotating_light: *{label}* never finished -- state was still "
                f"`{val}` when the scheduler (re)started, meaning the process "
                f"died mid-run with no chance to alert. Clearing the flag so "
                f"it retries on the next scheduled pass"
                + (f" (targeting {date_part})." if date_part != today_str else ".")
            )
            del state[key]
            changed = True
        if changed:
            save_state(state)
        return changed


def _state_completed_today(state: dict, key: str, today_str: str) -> bool:
    """True when the task completed OR is already running today.

    Treating '_running' as blocking prevents re-fires on process restart:
    a new process has an empty _running_tasks dict, so the in-memory
    thread check can't guard against duplicate launches — the state file
    is the only persistent guard.
    """
    val = state.get(key, '')
    # '_failed' also blocks: the run already fired today and errored (Slack alert
    # posted). Without this, the new failure state would re-launch the task on
    # every poll tick and spam the channel. It stays failed until re-run by hand,
    # which is correct for infra faults like a Zscaler deauth that need a human.
    return (val == today_str
            or val == f"{today_str}_running"
            or val == f"{today_str}_failed")


def _sofom_blocked_today(state: dict, key: str, today_str: str) -> bool:
    """Like _state_completed_today, but a '_failed' SOFOM run is RETRYABLE.

    2026-08-17: the webhook fast-path fired at 07:03, died because a Zscaler
    ZPA deauth had taken Redshift down, and still stamped the day complete.
    Both the fast-path and this hourly backstop then treated the day as done
    and skipped silently — a ~90-second network blip cost the whole day's tape,
    and nobody noticed until Brian asked.

    Retrying hourly is safe here: the failure mode is almost always a transient
    tunnel outage (which now self-heals — see the ZPA auto-recovery in
    _shared/redshift.py), the task is idempotent (it regenerates the tape and
    replaces the Gmail draft), and hourly is far too slow to spam Slack. The
    *webhook* path deliberately keeps using _state_completed_today, so
    push-driven retries stay blocked and only this hourly path retries.
    """
    val = state.get(key, '')
    return val == today_str or val == f"{today_str}_running"


def _wait_for_bb_pipeline(today_str: str, max_wait_seconds: int = 600, poll_interval: int = 15) -> bool:
    """Block until BOTH today's US Bridge and MX SOFOM BB builds are fully
    'completed' (not just '_running') in _report_scheduler_state.json, or
    until max_wait_seconds elapses.

    Root-caused 2026-07-15: run_weekly_bb, run_mx_bb, and run_morning_brief
    all launch as parallel background threads in the same loop tick (all
    gated only on their OWN '_running'/'completed' key, with no cross-task
    dependency). The brief posted 3s after launch while the actual tape
    builds/merges took another 2-5 minutes -- cm_morning_brief.py then read
    _cap_markets_state.json before the day's file existed, rendering
    $0/n/a instead of erroring. This gate makes the brief wait on the
    OTHER two tasks' exact 'completed' state (== today_str), not the
    looser _state_completed_today() check (which treats '_running' as
    already-handled -- correct for preventing double-launch, wrong for
    "is the data actually ready").
    """
    # 2026-09-25: the gate still required 'last_bb' (CIM US Bridge BB), which was
    # DISABLED 2026-08-10 -- so it could never be satisfied and the brief waited the
    # full 10 min and posted a false "still building" warning every single day.
    # Only wait on the BB jobs that are actually scheduled today.
    required = ['last_mx_bb']
    if os.environ.get('BRIDGE_BB_ENABLED') == '1':
        required.append('last_bb')
    if datetime.strptime(today_str, '%Y-%m-%d').weekday() == 0:
        required.append('last_nb_bb')   # NB BB runs Mondays only
    waited = 0
    vals = []
    while waited < max_wait_seconds:
        with _state_lock:
            state = load_state()
        vals = [state.get(k, '') for k in required]
        if all(v == today_str for v in vals):
            return True
        if any(v == f"{today_str}_failed" for v in vals):
            log.warning(f"BB gate: a required build failed today ({dict(zip(required, vals))})")
            return False
        time.sleep(poll_interval)
        waited += poll_interval
    log.warning(f"BB gate timed out; state = {dict(zip(required, vals))}")
    return False


# ---------------------------------------------------------------------------
# Slack
# ---------------------------------------------------------------------------

def _post_slack(text, blocks=None):
    # Canonical implementation in _shared/slack.py (P2-4 / GW-F13).
    sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', '_shared'))
    from notify import dm_owner
    return dm_owner(text, blocks=blocks)


# ---------------------------------------------------------------------------
# Background task launcher
# ---------------------------------------------------------------------------

_running_tasks: dict[str, threading.Thread] = {}


def _launch_background(task_name: str, fn, state_key: str, today_str: str):
    """Run fn in a daemon thread with idempotent state bookkeeping.

    - Marks state as '<today>_running' before launch so a crash/restart
      knows the task was interrupted and needs to re-run.
    - Updates state to '<today>' on completion (success or failure — fn
      handles its own Slack alerts for the outcome).
    - Will not double-fire if a thread for this task is already alive.
    """
    existing = _running_tasks.get(task_name)
    if existing and existing.is_alive():
        log.info(f"{task_name} already running — skipping.")
        return

    # Mark in-progress before spawning
    with _state_lock:
        state = load_state()
        state[state_key] = f"{today_str}_running"
        save_state(state)
    log.info(f"State[{state_key}] = '{today_str}_running'")

    def _wrapper():
        outcome = today_str
        try:
            fn()
        except BaseException as exc:
            # Never record a failed run as complete -- downstream gates
            # (morning brief, dashboard) treat '<today>' as "data is ready".
            outcome = f"{today_str}_failed"
            log.error(f"{task_name} failed: {exc}", exc_info=True)
        finally:
            with _state_lock:
                s = load_state()
                s[state_key] = outcome
                save_state(s)
            status = 'completed' if outcome == today_str else 'FAILED'
            log.info(f"State[{state_key}] = '{outcome}' ({status})")

    t = threading.Thread(target=_wrapper, name=task_name, daemon=True)
    _running_tasks[task_name] = t
    t.start()
    log.info(f"Launched '{task_name}' in background thread {t.ident}")


# ---------------------------------------------------------------------------
# Agent task execution via LangGraph REST API
# ---------------------------------------------------------------------------

def _run_agent_task(task_message: str) -> str | None:
    """Create a LangGraph thread, start a run, and poll until completion.

    Uses non-blocking /runs + polling /runs/{run_id} every 30s — no hard
    wall on duration.  Cancels and Slacks you if MAX_TASK_SECONDS is hit.
    Returns the agent's response text, or None on failure.
    """
    try:
        import httpx
    except ImportError:
        import subprocess
        subprocess.check_call([sys.executable, '-m', 'pip', 'install', '-q', 'httpx'])
        import httpx

    # Scheduler-driven runs are routine ops (same report every day) — opt out
    # of long-term memory capture so they stop polluting retrieval with
    # near-identical daily run trivia (Mem-H2; marker read by MemoryMiddleware).
    task_message = "<memory:off>\n" + task_message

    POLL_INTERVAL = 30
    TERMINAL      = {"success", "error", "failed", "interrupted", "timeout"}
    http_timeout  = httpx.Timeout(connect=10, read=60, write=30, pool=10)

    try:
        with httpx.Client(timeout=http_timeout) as client:

            # 1. Create thread
            resp = client.post(f"{LANGGRAPH_URL}/threads", json={})
            resp.raise_for_status()
            thread_id = resp.json()["thread_id"]
            log.info(f"Created thread {thread_id}")

            # 2. Start run (non-blocking)
            resp = client.post(
                f"{LANGGRAPH_URL}/threads/{thread_id}/runs",
                json={
                    "assistant_id": "lead_agent",
                    "input": {"messages": [{"role": "human", "content": task_message}]},
                    "config": {"recursion_limit": 500},
                    "context": {
                        "thread_id": thread_id,
                        "thinking_enabled": True,
                        "is_plan_mode": False,
                        "subagent_enabled": False,
                    },
                },
            )
            resp.raise_for_status()
            run_id = resp.json()["run_id"]
            log.info(f"Started run {run_id} — polling every {POLL_INTERVAL}s, cap {MAX_TASK_SECONDS}s")

            # 3. Poll until terminal — escape if hung
            elapsed = 0
            while True:
                time.sleep(POLL_INTERVAL)
                elapsed += POLL_INTERVAL

                if elapsed >= MAX_TASK_SECONDS:
                    log.error(f"Run {run_id} exceeded {MAX_TASK_SECONDS}s — cancelling.")
                    try:
                        client.post(
                            f"{LANGGRAPH_URL}/threads/{thread_id}/runs/{run_id}/cancel",
                            json={"wait": False},
                        )
                    except Exception as cancel_err:
                        log.warning(f"Cancel request failed (ignoring): {cancel_err}")
                    _post_slack(
                        f":alarm_clock: *Hung task cancelled* after {elapsed // 60}m\n"
                        f"Task: `{task_message[:120]}{'...' if len(task_message) > 120 else ''}`\n"
                        f"Run `{run_id}` on thread `{thread_id}` was cancelled. Run it manually."
                    )
                    return None

                status_resp = client.get(
                    f"{LANGGRAPH_URL}/threads/{thread_id}/runs/{run_id}"
                )
                status_resp.raise_for_status()
                run_data = status_resp.json()
                status   = run_data.get("status", "")
                log.info(f"  run {run_id} status={status} elapsed={elapsed}s")

                if status not in TERMINAL:
                    continue

                if status != "success":
                    log.error(f"Run ended with status={status}")
                    return None

                # 4. Fetch final thread state
                state_resp = client.get(f"{LANGGRAPH_URL}/threads/{thread_id}/state")
                state_resp.raise_for_status()
                messages = state_resp.json().get("values", {}).get("messages", [])
                for msg in reversed(messages):
                    if isinstance(msg, dict) and msg.get("type") == "ai":
                        content = msg.get("content", "")
                        if isinstance(content, str) and content.strip():
                            return content.strip()
                        if isinstance(content, list):
                            text_parts = [
                                b["text"] for b in content
                                if isinstance(b, dict) and b.get("type") == "text"
                            ]
                            if text_parts:
                                return "\n".join(text_parts).strip()

                log.warning("Run succeeded but no AI response text found in state")
                return None

    except Exception as e:
        log.error(f"Agent task failed: {e}")
        traceback.print_exc()
        return None


# ---------------------------------------------------------------------------
# Gmail helper — find today's SOFOM distribution email from Axel
# ---------------------------------------------------------------------------

# The distribution email is identified by its SUBJECT alone. Sender is not part
# of the contract: Axel usually sends it, William Filho has covered for him
# (2026-07-23, when a from:axel-only query silently missed the day and stalled
# the tape), and anyone else may cover next. Keep this in sync with
# _SOFOM_SUBJECT_RE in backend/webhook_receiver.py -- the Pub/Sub push is the
# primary trigger and this poll is only the backstop for a missed push, so the
# two must agree on what counts as the email.
SOFOM_SUBJECT_RE = re.compile(r'F/12339\s*>\s*F/12340\s*Distribution', re.IGNORECASE)


def _find_sofom_distribution_email(today_str: str) -> dict | None:
    """Find today's SOFOM distribution email by subject, from any sender.

    Returns {message_id, thread_id, subject} or None. Gmail's query language
    tokenises '/' and '>' unhelpfully, so we search broadly on the one reliable
    keyword plus today's date and apply SOFOM_SUBJECT_RE client-side — that way
    the poll and the webhook fast-path match byte-for-byte the same emails.
    """
    try:
        import importlib.util
        spec  = importlib.util.spec_from_file_location(
            'gmail_tool',
            str(Path(__file__).resolve().parent.parent / 'gmail' / 'gmail_tool.py')
        )
        gmail = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(gmail)
        service  = gmail._get_service()
        # -from:me -in:drafts: with the sender filter gone, our OWN reply and the
        # draft this job creates both match the subject. Replying to those would
        # be nonsense. This excludes self-authored mail without pinning the
        # search to any particular incoming sender.
        query    = f'subject:Distribution after:{today_str} -from:me -in:drafts -in:trash -in:spam'
        result   = service.users().messages().list(userId='me', q=query, maxResults=25).execute()
        matches  = []
        for ref in result.get('messages', []):
            msg = service.users().messages().get(
                userId='me', id=ref['id'],
                format='metadata',
                metadataHeaders=['Subject', 'From', 'Date']
            ).execute()
            headers = {h['name']: h['value'] for h in msg.get('payload', {}).get('headers', [])}
            subject = headers.get('Subject', '')
            if not SOFOM_SUBJECT_RE.search(subject):
                continue
            matches.append((int(msg.get('internalDate', '0')), ref['id'], msg.get('threadId'),
                            subject, headers.get('From', '?')))
        if not matches:
            return None
        # Oldest match = the original distribution email, not a later reply-all
        # on the same thread.
        matches.sort()
        _, msg_id, thread_id, subject, sender = matches[0]
        log.info(f'SOFOM distribution email matched: {subject!r} from {sender}'
                 + (f' ({len(matches)} candidates, used oldest)' if len(matches) > 1 else ''))
        return {'message_id': msg_id, 'thread_id': thread_id, 'subject': subject}
    except Exception as e:
        log.error(f'Gmail search for distribution email failed: {e}')
        return None


# Back-compat alias: older code and code-backups call the Axel-specific name.
_find_axel_distribution_email = _find_sofom_distribution_email


# ---------------------------------------------------------------------------
# Report tasks
# ---------------------------------------------------------------------------

def run_sofom_distribution(message_id: str, subject: str):
    """Generate SOFOM data tape and SEND reply-all on the distribution thread.

    Runs directly (SQL → Excel → Gmail draft) with no LangGraph agent,
    so it cannot time out or silently fail.
    """
    import psycopg2
    import pandas as pd
    import subprocess as _sp

    yesterday      = (datetime.now().date() - timedelta(days=1)).isoformat()
    three_days_ago = (datetime.now().date() - timedelta(days=3)).isoformat()
    start_date_dt  = datetime.now().date() - timedelta(days=3)
    end_date_dt    = datetime.now().date() - timedelta(days=1)
    fname = (
        'tape_sofom_daterange_'
        + three_days_ago.replace('-', '') + '_'
        + yesterday.replace('-', '') + '.xlsx'
    )

    log.info(f'Running SOFOM tape for {message_id} ({subject}), {three_days_ago}→{yesterday}')
    _post_slack(f':flag-mx: Running SOFOM distribution tape for *{subject}*...')

    SKILLS = str(Path(__file__).resolve().parent.parent)

    try:
        import sys as _sys
        scripts_dir = os.path.join(SKILLS, 'cfo-org-kb', 'scripts')
        if scripts_dir not in _sys.path:
            _sys.path.insert(0, scripts_dir)

        sql_path     = os.path.join(SKILLS, 'cfo-org-kb', 'sql', 'data_tape_sofom.sql')
        sql_template = open(sql_path).read()

        con = psycopg2.connect(
            host=os.environ['REDSHIFT_HOST'],
            port=int(os.environ['REDSHIFT_PORT']),
            dbname=os.environ['REDSHIFT_DB'],
            user=os.environ['REDSHIFT_USER'],
            password=os.environ['REDSHIFT_PASSWORD'],
            sslmode='require',
            sslrootcert='disable',
        )

        from eligibility_calculator_sofom import calculate_eligibility_fields

        dfs = []
        d   = start_date_dt
        while d <= end_date_dt:
            date_str = d.isoformat()
            log.info(f'  Querying {date_str}...')
            df = pd.read_sql_query(sql_template.format(date_str), con)
            log.info(f'  → {len(df)} rows')
            if len(df) > 0:
                df = calculate_eligibility_fields(df)
            dfs.append(df)
            d += timedelta(days=1)

        con.close()

        combined   = pd.concat(dfs, ignore_index=True)
        total_rows = len(combined)
        log.info(f'Tape: {total_rows} total rows across {combined["dt"].nunique()} dates')

        outputs_dir = str(Path(__file__).resolve().parent / 'outputs')
        os.makedirs(outputs_dir, exist_ok=True)
        out_path = os.path.join(outputs_dir, fname)
        combined.to_excel(out_path, index=False, sheet_name='tape_combined')
        log.info(f'Saved tape → {out_path}')

        gmail_tool = str(Path(__file__).resolve().parent.parent / 'gmail' / 'gmail_tool.py')
        result = _sp.run(
            [_sys.executable, gmail_tool, 'draft', message_id,
             'tape attached, please review', '--attach', out_path],
            capture_output=True, text=True
        )
        if result.returncode != 0:
            raise RuntimeError(f'Gmail draft failed: {result.stderr}')

        # AUTO-SEND (Brian, 2026-09-30: "The SOFOM distribution can be sent
        # directly now, no need to draft and wait for me to send").
        # Fail-closed: send only if every one of the 3 days returned rows;
        # otherwise the draft stays in Gmail for Brian and Slack says why.
        # SOFOM_AUTO_SEND=0 reverts to draft-only.
        _per_day = [len(x) for x in dfs]
        _complete = len(_per_day) == 3 and all(n > 0 for n in _per_day)
        _auto = os.environ.get('SOFOM_AUTO_SEND', '1') != '0'
        _draft_id = None
        for _ln in result.stdout.splitlines():
            if 'Draft ID:' in _ln:
                _draft_id = _ln.split('Draft ID:', 1)[1].strip()
        _hdr = (f':white_check_mark: *SOFOM tape — {subject}*\n'
                f'`{fname}` ({total_rows:,} rows, {three_days_ago} → {yesterday}; '
                f'per day {"/".join(str(n) for n in _per_day)})\n')
        if _auto and _complete and _draft_id:
            try:
                _gdir = str(Path(__file__).resolve().parent.parent / 'gmail')
                if _gdir not in _sys.path:
                    _sys.path.insert(0, _gdir)
                import gmail_tool as _gt
                _sent = _gt._get_service().users().drafts().send(
                    userId='me', body={'id': _draft_id}).execute()
                log.info(f"SOFOM reply SENT: message {_sent.get('id')} (draft {_draft_id})")
                _post_slack(_hdr + 'Sent reply-all on the distribution thread.')
            except Exception as _se:
                log.error(f'SOFOM auto-send failed, draft left: {_se}', exc_info=True)
                _post_slack(_hdr + f':warning: Auto-send failed (`{str(_se)[:150]}`) '
                            '-- draft left in Gmail. Review and send.')
        else:
            _why = ('SOFOM_AUTO_SEND=0' if not _auto else
                    'a day returned 0 rows' if not _complete else
                    'draft id not found in gmail_tool output')
            log.warning(f'SOFOM auto-send skipped: {_why}')
            _post_slack(_hdr + f':warning: NOT sent ({_why}) -- draft reply in Gmail. Review and send.')
        log.info('SOFOM distribution completed.')
        return True

    except Exception as e:
        log.error(f'SOFOM distribution failed: {e}', exc_info=True)
        _post_slack(
            f':warning: SOFOM tape failed for {repr(subject)}: `{e}`\n'
            f'Run manually: reply to `{message_id}` with 3-day tape ({three_days_ago} → {yesterday}).'
        )
        # Callers stamp scheduler state from this return value. Returning False
        # rather than raising keeps the webhook fast-path's fallback behaviour
        # intact, but the outcome is no longer invisible to the caller.
        return False


def run_portfolio_report():
    """Trigger portfolio report for the 1st of the current month."""
    now          = datetime.now()
    report_date  = now.replace(day=1).strftime('%Y-%m-%d')
    month_label  = now.strftime('%B %Y')

    log.info(f"Triggering portfolio report for {report_date}")
    _post_slack(f":calendar: Running scheduled *Portfolio Report* for {month_label}...")

    task = (
        f"Run the portfolio report for {report_date}. "
        f"Upload the completed report to Drive in the Portfolio Reporting/{now.strftime('%Y%m')}/ folder "
        f"and share the link here."
    )
    result = _run_agent_task(task)

    if result:
        _post_slack(f":white_check_mark: *Portfolio Report — {month_label}*\n{result[:3000]}")
        log.info("Portfolio report completed.")
    else:
        _post_slack(
            f":warning: *Portfolio Report — {month_label}* failed to generate. "
            f"Run it manually."
        )
        log.error("Portfolio report failed.")


def run_mx_bb():
    """Build MX (SOFOM) Borrowing Base directly (no agent) using yesterday's date.

    Runs daily at 8:30am PST. Mirrors run_weekly_bb's deterministic pipeline so a
    transient Redshift/Zscaler drop or a long agent run can't silently fail the job:
      1. build_mx.py --end-date {yesterday}  (with one zscaler-reauth retry on conn error)
         -> $OUTPUTS_PATH/Borrowing Base - SOFOM - {YYYYMMDD}.xlsx
      2. merge_template.py (most-recent SOFOM Master on Drive as template)
         -> $OUTPUTS_PATH/Jeeves SOFOM Borrowing Base - Master - {YYYYMMDD}.xlsx
      3. upload_to_drive.py --new -> Debt/CIM/{YYYYMM}/ folder
      4. Post Slack with Drive link + key balances
    """
    import subprocess, re as _re

    yesterday   = (datetime.now().date() - timedelta(days=1)).isoformat()
    yyyymmdd    = yesterday.replace('-', '')
    yyyymm      = yyyymmdd[:6]
    outputs_dir = os.environ.get('OUTPUTS_PATH', 'C:/Jeeves/redshift-bot/deer-flow/backend/.deer-flow/threads/7a0af2ac-5107-48a9-a6f6-137a645fa75a/user-data/outputs').replace(chr(92), '/')
    skills_dir  = os.path.dirname(os.path.abspath(__file__))

    log.info(f"run_mx_bb: building MX SOFOM BB for {yesterday} (direct pipeline)")
    _post_slack(f":flag-mx: Running scheduled *MX Borrowing Base* for {yesterday}...")

    def _run(cmd, step, timeout=600):
        log.info(f"  [{step}] {' '.join(cmd)}")
        env = os.environ.copy()
        env['PYTHONIOENCODING'] = 'utf-8'
        # stdin=DEVNULL so merge_template.py's "Continue anyway?" prompt (SOFOM has no
        # Summary tabs) auto-answers 'y' via its sys.stdin.isatty() is False branch.
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout,
                           encoding='utf-8', errors='replace', env=env,
                           stdin=subprocess.DEVNULL)
        out = ((r.stdout or '') + (r.stderr or '')).strip()
        if r.returncode != 0:
            raise RuntimeError(f"Step '{step}' failed (rc={r.returncode}):\n{out[-1000:]}")
        log.info(f"  [{step}] OK: {out[-300:]}")
        return out

    def _run_retry(cmd, step, timeout, attempts=3, backoff=20):
        """_run with bounded retries -- for NETWORK steps (Drive list/upload).

        2026-09-14: the day's tape died because the Drive listing took >120s
        (measured 102s on a good day -- it was always one slow call away from
        failing) and a single TimeoutExpired killed the whole run. Network
        steps now get 3 tries before the job is declared failed.
        """
        last_err = None
        for i in range(1, attempts + 1):
            try:
                return _run(cmd, step if i == 1 else f"{step}_try{i}", timeout=timeout)
            except Exception as err:
                last_err = err
                log.warning(f"  [{step}] attempt {i}/{attempts} failed: {str(err)[:200]}")
                if i < attempts:
                    time.sleep(backoff)
        raise last_err

    try:
        # -- 1. Generate raw SOFOM tape (with one zscaler-reauth retry) ----------
        build_script = os.path.join(skills_dir, 'build_mx.py')
        build_out = ""
        try:
            build_out = _run([sys.executable, build_script, '--end-date', yesterday],
                             'build_mx', timeout=900)
        except RuntimeError as build_err:
            msg = str(build_err).lower()
            conn_err = any(k in msg for k in (
                'connection', 'timeout', 'ssl', 'could not connect',
                'connection refused', 'host', 'operationalerror', 'reset'))
            if not conn_err:
                raise
            log.warning("build_mx hit a connection error — running zscaler reauth then retrying once.")
            reauth = os.path.join(skills_dir, '..', 'zscaler-reauth', 'reauth.py')
            try:
                _run([sys.executable, reauth], 'zscaler_reauth', timeout=180)
            except Exception as re_err:
                log.error(f"zscaler reauth failed: {re_err}")
            build_out = _run([sys.executable, build_script, '--end-date', yesterday],
                             'build_mx_retry', timeout=900)

        raw_wb = os.path.join(outputs_dir, f'Borrowing Base - SOFOM - {yyyymmdd}.xlsx')
        if not os.path.exists(raw_wb):
            raise FileNotFoundError(f"build_mx.py did not produce {raw_wb}")

        # -- 2. Find most-recent SOFOM Master on Drive to use as template -------
        list_script = os.path.join(skills_dir, '..', 'google-drive', 'list_drive_folder.py')
        # 2026-09-14: was a --max-depth 4 recursive crawl of the whole Debt root
        # (1-0K8EM8slr1_I4Iik7_ZZn0t4SSAMKLU): 2,167 items, 102s measured, 120s
        # timeout -> the job failed the moment Drive was slightly slow. Both
        # things this step needs (the newest SOFOM Master and the {yyyymm}/
        # upload folder) live in Debt/CIM/Reporting, so list ONLY that folder at
        # depth 1: ~18s, and no cross-facility {yyyymm}/ name collisions with
        # Debt/Neuberger Berman/Reporting.
        MX_REPORTING_FOLDER = '1oDi6HjzArnX_jPQbI9UfEs4R5DC9TTrA'
        listing = _run_retry(
            [sys.executable, list_script, MX_REPORTING_FOLDER,
             '--recursive', '--max-depth', '1'],
            'list_drive', timeout=300
        ) or ""

        best_date, template_id = '', ''
        for line in listing.splitlines():
            if 'SOFOM Borrowing Base - Master' not in line:
                continue
            m_date = _re.search(r'Master - (\d{8})\.xlsx', line)
            m_id   = _re.search(r'id:\s*([A-Za-z0-9_-]+)', line)
            if m_date and m_id:
                d = m_date.group(1)
                if d < yyyymmdd and d > best_date:
                    best_date, template_id = d, m_id.group(1)
        if not template_id:
            raise RuntimeError("Could not find a prior SOFOM Master template on Drive")
        log.info(f"  Template: Jeeves SOFOM Borrowing Base - Master - {best_date}.xlsx ({template_id})")

        # -- 3. Merge into template --------------------------------------------
        merge_script = os.path.join(skills_dir, 'merge_template.py')
        final_wb = os.path.join(outputs_dir, f'Jeeves SOFOM Borrowing Base - Master - {yyyymmdd}.xlsx')
        _run([sys.executable, merge_script, raw_wb, template_id, '--output', final_wb],
             'merge_template', timeout=300)
        if not os.path.exists(final_wb):
            raise FileNotFoundError(f"merge_template.py did not produce {final_wb}")

        # -- 4. Find the correct CIM month folder ------------------------------
        month_folder_id = ''
        for line in listing.splitlines():
            m_f  = _re.search(r'\[folder\]\s+' + yyyymm + r'/', line)
            m_id = _re.search(r'id:\s*([A-Za-z0-9_-]+)', line)
            if m_f and m_id:
                month_folder_id = m_id.group(1)
                break
        if not month_folder_id:
            month_folder_id = _ensure_month_folder(yyyymm)
        log.info(f"  Upload target: {yyyymm}/ ({month_folder_id})")

        # -- 5. Upload (--new is REQUIRED so the dated master isn't overwritten) -
        upload_script = os.path.join(skills_dir, '..', 'google-drive', 'upload_to_drive.py')
        upload_out = _run_retry(
            [sys.executable, upload_script, final_wb, '--folder', month_folder_id, '--new'],
            'upload', timeout=300, attempts=3
        )
        drive_link = ''
        m_link = _re.search(r'https://(?:docs|drive)\.google\.com/\S+', upload_out)
        if m_link:
            drive_link = m_link.group(0).rstrip(')')

        # -- 6. Parse key balances from build output ---------------------------
        stats = ""
        m_bal  = _re.search(r'Latest date SOFOM balance:\s*\$([\d,]+\.\d+)', build_out)
        m_elig = _re.search(r'Eligible SOFOM balance:\s*\$([\d,]+\.\d+)', build_out)
        if m_bal and m_elig:
            stats = f"SOFOM balance: *${m_bal.group(1)}*  |  Eligible: *${m_elig.group(1)}*"

        # -- 6b. Source-data gaps are never silent ----------------------------
        # 2026-09-21 (Brian): "If the spot rate or any other column isn't
        # populated you need to let me know, not just coalesce over it."
        # build_mx.py emits one DATA-GAP line per missing critical column;
        # every one of them goes to Slack ABOVE the numbers, because a base
        # built on carried-forward FX is not the same number as a market base.
        gap_lines = [ln.strip() for ln in build_out.splitlines()
                     if 'DATA-GAP:' in ln]
        gap_block = ""
        if gap_lines:
            uniq = list(dict.fromkeys(g.split('DATA-GAP:', 1)[1].strip() for g in gap_lines))
            gap_block = (":rotating_light: *SOURCE DATA GAPS -- base is not clean*\n"
                         + "\n".join(f"  - {g}" for g in uniq) + "\n")
            log.warning(f"run_mx_bb: {len(uniq)} data gap(s): {uniq}")

        msg = (
            f":white_check_mark: *MX Borrowing Base -- {yesterday}*\n"
            + gap_block
            + (f"{stats}\n" if stats else "")
            + (f"<{drive_link}|Open in Drive>" if drive_link else f"Uploaded to Drive/CIM/{yyyymm}/")
        )
        _post_slack(msg)
        log.info(f"run_mx_bb: done. {stats}{' | GAPS: ' + str(len(gap_lines)) if gap_lines else ''}")

    except Exception as exc:
        log.error(f"run_mx_bb failed: {exc}", exc_info=True)
        _post_slack(
            f":warning: *MX Borrowing Base -- {yesterday}* failed to generate.\n"
            f"`{str(exc)[:400]}`\nRun manually: `build_mx.py --end-date {yesterday}`"
        )
        # Re-raise so _launch_background records '<today>_failed' rather than
        # marking the day complete. Swallowing it here is what let the
        # 2026-08-08 build report success in state while producing nothing.
        raise


def run_nb_bb(as_of=None, variant='weekly', cico_msg=None):
    """Build the NB Borrowing Base — WEEKLY, MONDAYS, as of the prior Sunday.

    Cadence set by Brian 2026-08-11 and re-confirmed 2026-08-17 ("it needs to run
    every monday as of the previous day"). Pipeline mirrors run_mx_bb:
      1. Find the most recent prior "Jeeves NB Borrowing Base - Form - {YYYYMMDD}.xlsx"
         in Debt/Neuberger Berman/Reporting/{YYYYMM}/ and download it as the template
         (clone-and-edit — never re-architect the form).
      2. build_nb.py --as-of {yesterday} --template {template} --out-date {today}
         (tape from cfo-org-kb data_tape.sql; bank balances from the latest CICO
         daily balances email, matched to Schedule 5.22 by trailing digits)
      3. upload_to_drive.py --new  ->  the {YYYYMM} Reporting subfolder
      4. Post Slack with the Drive link + headline stats
    """
    import subprocess, re as _re

    # variant='month_end' (2026-09-23, Brian: "I want all of it done before the
    # requirement"): Section 6.02(b)(y) month-end BBC + 6.02(f) cash report,
    # called from nb_monthly_reporting_cron with as_of = month end and the
    # month-end CICO balances message. Same build + same fail-closed covenant
    # gate; the Gmail draft is NOT built here -- the month-end certifications
    # (account delta, SPV Effective Advance Rates, Immaterial Subsidiary test)
    # are added by the agent first. Returns a result dict for the caller.
    is_me       = (variant == 'month_end')
    yesterday   = as_of or (datetime.now().date() - timedelta(days=1)).isoformat()
    today_stamp = (yesterday.replace('-', '') if is_me
                   else datetime.now().date().strftime('%Y%m%d'))
    yyyymm      = yesterday.replace('-', '')[:6]
    result      = {'variant': variant, 'as_of': yesterday, 'gate_ok': False,
                   'final_wb': None, 'drive_link': '', 'cov_res': None, 'build_out': ''}
    _label      = 'NB Month-End Borrowing Base' if is_me else 'NB Borrowing Base'
    outputs_dir = os.environ.get('OUTPUTS_PATH', '').replace(chr(92), '/')
    skills_dir  = os.path.dirname(os.path.abspath(__file__))
    NB_REPORTING_FOLDER = '17Eji0ZgVcaAZcdoPDKNNJaClN8PKuTLW'

    log.info(f"run_nb_bb: building NB BB as of {yesterday}")
    _post_slack(f":bank: Running scheduled *{_label}* as of {yesterday}...")

    def _run(cmd, step, timeout=900):
        log.info(f"  [{step}] {' '.join(str(c) for c in cmd)}")
        env = os.environ.copy()
        env['PYTHONIOENCODING'] = 'utf-8'
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout,
                           encoding='utf-8', errors='replace', env=env,
                           stdin=subprocess.DEVNULL)
        out = ((r.stdout or '') + (r.stderr or '')).strip()
        if r.returncode != 0:
            raise RuntimeError(f"Step '{step}' failed (rc={r.returncode}):\n{out[-1000:]}")
        log.info(f"  [{step}] OK: {out[-300:]}")
        return out

    try:
        # -- 1. Locate the most recent prior NB form as template ---------------
        list_script = os.path.join(skills_dir, '..', 'google-drive', 'list_drive_folder.py')
        listing = _run([sys.executable, list_script, NB_REPORTING_FOLDER,
                        '--recursive', '--max-depth', '3'], 'list_drive', timeout=180) or ""
        # Brian, 2026-08-17: ALWAYS template off the most recent workbook on
        # Drive. The old rule matched only the '- Form -' name and only dates
        # strictly before today, so a short-named file (e.g. 'Jeeves NB
        # Borrowing Base - 20260807.xlsx') or a manual build done earlier the
        # same day was invisible and the run inherited a stale form.
        best_date, best_is_form, template_id, month_folder_id = '', False, '', ''
        for line in listing.splitlines():
            m_date = _re.search(r'NB Borrowing Base - (Form - )?(\d{8})\.xlsx', line)
            m_id   = _re.search(r'id:\s*([A-Za-z0-9_-]+)', line)
            if m_date and m_id:
                d, is_form = m_date.group(2), bool(m_date.group(1))
                # newest date wins; on a same-date tie prefer the '- Form -' copy
                if d > best_date or (d == best_date and is_form and not best_is_form):
                    best_date, best_is_form, template_id = d, is_form, m_id.group(1)
            m_f = _re.search(r'\[folder\]\s+' + yyyymm + r'/', line)
            if m_f and m_id and not month_folder_id:
                month_folder_id = m_id.group(1)
        if not template_id:
            raise RuntimeError('No "NB Borrowing Base - [Form - ]YYYYMMDD.xlsx" found on Drive')
        tpl_name = 'Jeeves NB Borrowing Base - {}{}.xlsx'.format(
            'Form - ' if best_is_form else '', best_date)
        log.info(f"  Template: {tpl_name} ({template_id})")

        dl_script = os.path.join(skills_dir, '..', 'google-drive', 'download_file.py')
        _run([sys.executable, dl_script, template_id], 'download_template', timeout=300)
        template = os.path.join(outputs_dir, tpl_name)
        if not os.path.exists(template):
            raise FileNotFoundError(f'template download missing: {template}')

        # -- 2. Build (one zscaler-reauth retry on a connection error) ---------
        build_script = os.path.join(skills_dir, 'build_nb.py')
        cmd = [sys.executable, build_script, '--as-of', yesterday,
               '--template', template, '--out-date', today_stamp]
        if cico_msg:
            cmd += ['--cico-msg', cico_msg]
        if is_me:
            cmd += ['--cico-fill-nearest']  # Brian 2026-09-23: closest date fills gaps
        try:
            build_out = _run(cmd, 'build_nb', timeout=1800)
        except RuntimeError as build_err:
            msg = str(build_err).lower()
            if not any(k in msg for k in ('connection', 'timeout', 'ssl', 'could not connect',
                                          'connection refused', 'operationalerror', 'reset')):
                raise
            log.warning('build_nb hit a connection error — zscaler reauth then one retry.')
            reauth = os.path.join(skills_dir, '..', 'zscaler-reauth', 'reauth.py')
            try:
                _run([sys.executable, reauth], 'zscaler_reauth', timeout=180)
            except Exception as re_err:
                log.error(f'zscaler reauth failed: {re_err}')
            build_out = _run(cmd, 'build_nb_retry', timeout=1800)

        final_wb = os.path.join(outputs_dir,
                                f'Jeeves NB Borrowing Base - Form - {today_stamp}.xlsx')
        if not os.path.exists(final_wb):
            raise FileNotFoundError(f'build_nb.py did not produce {final_wb}')

        # -- 3. Upload into the month subfolder (--new: never overwrite a snapshot)
        if not month_folder_id:
            # 2026-08-17: a new month has no subfolder until somebody makes one,
            # so the first Monday run of every month would hard-fail here after
            # a clean build. Create it under NB Reporting instead.
            sys.path.insert(0, os.path.join(skills_dir, '..', '_shared'))
            from google_auth import get_credentials
            from googleapiclient.discovery import build as _gbuild
            _svc = _gbuild('drive', 'v3', credentials=get_credentials())
            month_folder_id = _svc.files().create(
                body={'name': yyyymm,
                      'mimeType': 'application/vnd.google-apps.folder',
                      'parents': [NB_REPORTING_FOLDER]},
                fields='id', supportsAllDrives=True).execute()['id']
            log.info(f'  Created {yyyymm} subfolder under NB Reporting ({month_folder_id})')
        upload_script = os.path.join(skills_dir, '..', 'google-drive', 'upload_to_drive.py')
        upload_out = _run([sys.executable, upload_script, final_wb,
                           '--folder', month_folder_id, '--new'], 'upload', timeout=180)
        drive_link = ''
        m_link = _re.search(r'https://(?:docs|drive)\.google\.com/\S+', upload_out)
        if m_link:
            drive_link = m_link.group(0).rstrip(')')

        # -- 3.5 Gmail draft to the NB / Wilmington Trust distribution ---------
        # 2026-09-09: this step never existed, so Brian had to ask for the email
        # by hand on 8/25, 9/2 and 9/9 even though the workbook built fine.
        # DRAFT ONLY -- nothing is ever auto-sent.
        draft_note = ''

        # -- 3.4 Covenant gate -------------------------------------------------
        # 2026-09-16 (Brian): "NEVER ALLOW IT TO BE CREATED WITH A COVENANT
        # FAILURE THAT ISN'T FLAGGED."  The workbook stores every covenant cell
        # as an uncached Excel formula, so nb_covenant_check.py re-computes
        # 7.01(a) Liquidity, 7.01(b) DQ30, 7.01(c) EBITDA Coverage and BB
        # sufficiency in Python.  Fail closed: if the check errors or any test
        # fails, NO draft is created and Slack gets a red flag instead.
        class _CovenantGate(Exception):
            pass

        class _MonthEndHandoff(Exception):
            pass

        cov_res = None
        cov_err = ''
        try:
            sys.path.insert(0, skills_dir)
            from nb_covenant_check import check_workbook as _cov_check
            from nb_covenant_check import format_report as _cov_fmt
            cov_res = _cov_check(final_wb)
            log.info('  [covenant_check] ' + _cov_fmt(cov_res).replace('\n', ' | '))
        except Exception as _ce:
            cov_err = str(_ce)[:200]
            log.error(f'  [covenant_check] FAILED: {_ce}', exc_info=True)

        try:
            if cov_res is None:
                raise _CovenantGate(
                    f'covenant check could not run ({cov_err}) -- no draft created')
            if not cov_res['ok']:
                raise _CovenantGate('; '.join(
                    '{} -- {}'.format(t['name'], t['detail']) for t in cov_res['failures']))
            if is_me:
                raise _MonthEndHandoff()
            import shutil as _shutil, base64 as _b64
            sys.path.insert(0, os.path.join(skills_dir, '..', 'gmail'))
            import gmail_tool as _gt
            _dist = os.path.join(os.path.dirname(_state_path()), 'nb_bb_distribution.json')
            with open(_dist, encoding='utf-8') as _f:
                _cfg = json.load(_f)
            _as_of_long = datetime.strptime(yesterday, '%Y-%m-%d').strftime('%B %-d, %Y') \
                if os.name != 'nt' else datetime.strptime(yesterday, '%Y-%m-%d').strftime('%B %#d, %Y')
            _attach = os.path.join(outputs_dir,
                                   f'Jeeves NB Borrowing Base - {today_stamp}.xlsx')
            _shutil.copy2(final_wb, _attach)
            _subject = _cfg.get('subject_template',
                                'Jeeves Weekly Borrowing Base - As of {as_of_long}'
                                ).format(as_of_long=_as_of_long)
            _body = ("Hi all-\n\n"
                     f"Attached is this week's borrowing base as of Sunday, {_as_of_long}.\n\n"
                     "Thanks,\nBrian\n")
            _mime = _gt._build_mime(_body, 'brian.mauck@tryjeeves.com',
                                    ', '.join(_cfg['to']), _subject,
                                    cc=', '.join(_cfg.get('cc', [])),
                                    attachments=[_attach])
            _svc_g = _gt._get_service()
            _draft = _svc_g.users().drafts().create(
                userId='me',
                body={'message': {'raw': _b64.urlsafe_b64encode(_mime.as_bytes()).decode()}}
            ).execute()
            log.info(f"  [gmail_draft] OK: draft {_draft['id']} -- {_subject}")
            _cov_line = '; '.join(
                '{}: {}'.format(t['name'].split()[1] if len(t['name'].split()) > 1
                                else t['name'], t['detail'])
                for t in cov_res['tests'])
            log.info('  [covenant_check] all tests pass -- ' + _cov_line)
            draft_note = (f"\n:email: Draft ready in Gmail: *{_subject}* "
                          f"(To: {len(_cfg['to'])}, Cc: {len(_cfg.get('cc', []))}) -- review and send.")
        except _MonthEndHandoff:
            result['gate_ok'] = True
            draft_note = ('\n:white_check_mark: Covenant gate passed. Month-end certifications '
                          '+ Gmail draft are being prepared by the agent (not sent).')
        except _CovenantGate as _cg:
            log.error(f'  [gmail_draft] WITHHELD -- covenant gate: {_cg}')
            draft_note = ('\n:rotating_light: *COVENANT FAILURE -- Gmail draft WITHHELD.* '
                          f'{_cg}\nNothing was drafted to NB / Wilmington Trust. '
                          'Review the certificate before any send.')
        except Exception as _de:
            log.error(f"  [gmail_draft] FAILED: {_de}", exc_info=True)
            draft_note = f"\n:warning: Gmail draft step failed -- `{str(_de)[:200]}`"

        # -- 4. Slack ----------------------------------------------------------
        bits = []
        m_stats = _re.search(r"STATS: (\{.*\})", build_out)
        m_cico  = _re.search(r"CICO email: '([^']+)'", build_out)
        if m_stats:
            bits.append(f"`{m_stats.group(1)[:220]}`")
        if m_cico:
            bits.append(f"Bank balances from *{m_cico.group(1)}*")
        _post_slack(
            f":white_check_mark: *{_label} -- as of {yesterday}*\n"
            + ("\n".join(bits) + "\n" if bits else "")
            + (f"<{drive_link}|Open in Drive>" if drive_link
               else f"Uploaded to Drive/Neuberger Berman/Reporting/{yyyymm}/")
            + "\nOpen in Excel to recalculate the form."
            + draft_note
        )
        log.info('run_nb_bb: done.')
        result.update(final_wb=final_wb, drive_link=drive_link, cov_res=cov_res,
                      build_out=build_out)
        return result

    except Exception as exc:
        log.error(f"run_nb_bb failed: {exc}", exc_info=True)
        _post_slack(
            f":warning: *{_label} -- as of {yesterday}* failed to generate.\n"
            f"`{str(exc)[:400]}`\n"
            f"Run manually: `build_nb.py --as-of {yesterday} --template <prior form>`"
        )
        raise


def run_weekly_bb():
    """Build US Borrowing Base directly (no agent) to avoid 90-min LangGraph timeout.

    Pipeline:
      1. build_us.py  → $OUTPUTS_PATH/Borrowing Base - US - {YYYYMMDD}.xlsx
      2. merge_template.py (most-recent Bridge BB on Drive as template)
         → $OUTPUTS_PATH/Jeeves Bridge Borrowing Base - {YYYYMMDD}.xlsx
      3. upload_to_drive.py → Debt/CIM/{YYYYMM}/ folder
      4. Post Slack with Drive link + key stats
    """
    import subprocess, re as _re

    yesterday   = (datetime.now().date() - timedelta(days=1)).isoformat()
    yyyymmdd    = yesterday.replace('-', '')
    yyyymm      = yyyymmdd[:6]
    outputs_dir = os.environ.get('OUTPUTS_PATH', 'C:/Jeeves/redshift-bot/deer-flow/backend/.deer-flow/threads/7a0af2ac-5107-48a9-a6f6-137a645fa75a/user-data/outputs').replace(chr(92), '/')
    skills_dir  = os.path.dirname(os.path.abspath(__file__))

    log.info(f"run_weekly_bb: building US BB for {yesterday} (direct pipeline)")
    _post_slack(f":calendar: Running scheduled *US Borrowing Base* for {yesterday}...")

    def _run(cmd, step, timeout=600):
        log.info(f"  [{step}] {' '.join(cmd)}")
        env = os.environ.copy()
        env['PYTHONIOENCODING'] = 'utf-8'
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout,
                           encoding='utf-8', errors='replace', env=env)
        out = ((r.stdout or '') + (r.stderr or '')).strip()
        if r.returncode != 0:
            raise RuntimeError(f"Step '{step}' failed (rc={r.returncode}):\n{out[-1000:]}")
        log.info(f"  [{step}] OK: {out[-300:]}")
        return out

    try:
        # ── 1. Generate raw data workbook ──────────────────────────────────
        build_script = os.path.join(skills_dir, 'build_us.py')
        _run([sys.executable, build_script, '--date', yesterday], 'build_us', timeout=900)

        raw_wb = os.path.join(outputs_dir, f'Borrowing Base - US - {yyyymmdd}.xlsx')
        if not os.path.exists(raw_wb):
            raise FileNotFoundError(f"build_us.py did not produce {raw_wb}")

        # ── 2. Find most-recent Bridge BB on Drive to use as template ──────
        # CIM parent folder: 1-0K8EM8slr1_I4Iik7_ZZn0t4SSAMKLU
        list_script = os.path.join(skills_dir, '..', 'google-drive', 'list_drive_folder.py')
        listing = _run(
            [sys.executable, list_script, '1-0K8EM8slr1_I4Iik7_ZZn0t4SSAMKLU',
             '--recursive', '--max-depth', '4'],
            'list_drive', timeout=120
        ) or ""

        # Pick the most-recent Jeeves Bridge BB file (by filename date, must be prior to today)
        bridge_lines = [l for l in listing.splitlines() if 'Jeeves Bridge Borrowing Base' in l]
        if not bridge_lines:
            raise RuntimeError("No existing Bridge BB found on Drive to use as template")

        best_date, template_id = '', ''
        for line in bridge_lines:
            m_date = _re.search(r'(\d{8})\.xlsx', line)
            m_id   = _re.search(r'id:\s*([A-Za-z0-9_-]+)', line)
            if m_date and m_id:
                d = m_date.group(1)
                if d < yyyymmdd and d > best_date:
                    best_date, template_id = d, m_id.group(1)

        if not template_id:
            raise RuntimeError(f"Could not find a prior Bridge BB template on Drive")

        log.info(f"  Template: Jeeves Bridge Borrowing Base - {best_date}.xlsx ({template_id})")

        # ── 3. Merge into template ─────────────────────────────────────────
        merge_script = os.path.join(skills_dir, 'merge_template.py')
        final_wb = os.path.join(outputs_dir, f'Jeeves Bridge Borrowing Base - {yyyymmdd}.xlsx')
        _run([sys.executable, merge_script, raw_wb, template_id, '--output', final_wb],
             'merge_template', timeout=300)

        if not os.path.exists(final_wb):
            raise FileNotFoundError(f"merge_template.py did not produce {final_wb}")

        # ── 4. Find the correct CIM month folder ──────────────────────────
        month_folder_id = ''
        for line in listing.splitlines():
            m_f  = _re.search(r'\[folder\]\s+' + yyyymm + r'/', line)
            m_id = _re.search(r'id:\s*([A-Za-z0-9_-]+)', line)
            if m_f and m_id:
                month_folder_id = m_id.group(1)
                break

        if not month_folder_id:
            month_folder_id = _ensure_month_folder(yyyymm)

        log.info(f"  Upload target: {yyyymm}/ ({month_folder_id})")

        # ── 5. Upload ──────────────────────────────────────────────────────
        upload_script = os.path.join(skills_dir, '..', 'google-drive', 'upload_to_drive.py')
        upload_out = _run(
            [sys.executable, upload_script, final_wb, '--folder', month_folder_id],
            'upload', timeout=120
        )

        drive_link = ''
        m_link = _re.search(r'https://drive\.google\.com/\S+', upload_out)
        if m_link:
            drive_link = m_link.group(0).rstrip(')')

        # ── 6. Parse key stats from raw workbook ──────────────────────────
        try:
            import openpyxl
            wb_data = openpyxl.load_workbook(raw_wb, read_only=True, data_only=True)
            ws = wb_data['eligibility_summary']
            rows = list(ws.iter_rows(values_only=True))
            eop_balance = elig_balance = elig_accounts = None
            for row in rows[1:]:
                if row and str(row[0]).startswith('EOP'):
                    eop_balance   = row[2]
                    elig_balance  = row[4]
                    elig_accounts = row[3]
                    break
            wb_data.close()
            stats = (
                f"EOP balance: *${eop_balance:,.0f}*  |  "
                f"Eligible: *${elig_balance:,.0f}* ({elig_accounts:,} accts)"
                if eop_balance else ""
            )
        except Exception as e:
            log.warning(f"Could not parse stats: {e}")
            stats = ""

        msg = (
            f":white_check_mark: *US Borrowing Base — {yesterday}*\n"
            + (f"{stats}\n" if stats else "")
            + (f"<{drive_link}|Open in Drive>" if drive_link else f"Uploaded to Drive/CIM/{yyyymm}/")
        )
        _post_slack(msg)
        log.info(f"run_weekly_bb: done. {stats}")

    except Exception as exc:
        log.error(f"run_weekly_bb failed: {exc}", exc_info=True)
        _post_slack(
            f":warning: *US Borrowing Base — {yesterday}* failed.\n"
            f"`{exc}`\nRun manually: `build_us.py --date {yesterday}`"
        )



def run_morning_brief():
    """Post daily Capital Markets brief to Brian's Slack DM (~8am weekdays).

    Root-caused 2026-07-15: this used to fire immediately (3s after launch)
    in parallel with run_weekly_bb/run_mx_bb -- and separately,
    cap_markets_metrics_refresh.py (which writes the _cap_markets_state.json
    this brief's cm_morning_brief.py reads) runs on its OWN independent
    1-hour cron, so its last snapshot before the brief fired could be
    mid-build/stale for today's date label. Two unsynchronized crons racing
    on the same state file produced the $0/n/a brief.

    Fix: (1) wait for today's US Bridge + MX SOFOM BB builds to report
    'completed' via _wait_for_bb_pipeline, then (2) force a synchronous
    cap_markets_metrics_refresh.py run so _cap_markets_state.json reflects
    the just-finished tapes -- instead of trusting whatever the independent
    hourly refresh cron last happened to write -- THEN post.
    """
    import subprocess
    today_str = datetime.now().date().isoformat()
    # deer-flow root: parents[3] from skills/custom/jeeves-borrowing-base/.
    deer_flow_root = Path(__file__).resolve().parents[3]
    scripts_dir = deer_flow_root / 'backend' / 'scripts'

    log.info("Waiting for today's BB pipelines before posting morning brief...")
    ready = _wait_for_bb_pipeline(today_str)
    if not ready:
        log.warning(
            "BB pipelines not marked 'completed' after max wait -- "
            "posting brief anyway with a staleness warning."
        )

    # Force a fresh metrics snapshot now that the tapes are (believed) built,
    # rather than trusting cap_markets_metrics_refresh.py's own independent
    # hourly cadence to have already caught up.
    refresh_script = str(scripts_dir / 'cap_markets_metrics_refresh.py')
    try:
        r = subprocess.run(
            [sys.executable, refresh_script],
            capture_output=True, text=True, timeout=240
        )
        if r.returncode == 0:
            log.info("Pre-brief metrics refresh OK.")
        else:
            log.error(f"Pre-brief metrics refresh failed: {r.stderr.strip()[-300:]}")
    except Exception as e:
        log.error(f"Pre-brief metrics refresh error: {e}")

    if not ready:
        _post_slack(
            ":warning: Morning brief delayed -- BB pipeline still building after "
            "10 min. Figures below may be incomplete/stale."
        )

    log.info("Posting morning Capital Markets brief...")
    # cm_morning_brief.py lives in backend/scripts/, not skills/custom/scripts/.
    script = str(scripts_dir / 'cm_morning_brief.py')
    try:
        result = subprocess.run(
            [sys.executable, script],
            capture_output=True, timeout=60
        )
        if result.returncode == 0:
            log.info("Morning brief sent.")
        else:
            err = result.stderr.decode('utf-8', errors='replace')[:200]
            log.error(f"Morning brief failed: {err}")
            _post_slack(f":warning: Morning brief failed: `{err[:120]}`")
    except Exception as e:
        log.error(f"Morning brief error: {e}")


def run_cico_dashboard():
    """Full dashboard update — BB files, CICO email, Redshift strats, ops pulse.
    Runs every weekday after 8am via dashboard_full_update.py.
    """
    import subprocess, re as _re
    log.info("Triggering full dashboard update...")

    script = os.path.normpath(os.path.join(
        os.path.dirname(__file__), '..', 'jeeves-capital-markets', 'dashboard_full_update.py'
    ))

    if not os.path.exists(script):
        log.error(f"dashboard_full_update.py not found at {script}")
        _post_slack(":warning: Dashboard update failed — script not found.")
        return

    try:
        result = subprocess.run(
            [sys.executable, script],
            capture_output=True, text=True, timeout=300
        )
        output = (result.stdout + result.stderr).strip()
        log.info(f"Dashboard update output: {output[-500:]}")

        m = _re.search(r'https://drive\.google\.com/\S+', output)
        link = m.group(0) if m else None

        if link:
            _post_slack(
                f":bar_chart: *Capital Markets Dashboard updated*\n"
                f"CICO data refreshed from today's email.\n{link}"
            )
            log.info(f"CICO dashboard updated: {link}")
        else:
            _post_slack(
                f":warning: CICO dashboard update ran but no Drive link found.\n"
                f"```{output[-300:]}```"
            )
    except subprocess.TimeoutExpired:
        log.error("CICO dashboard update timed out after 5 minutes")
        _post_slack(":warning: CICO dashboard update timed out.")
    except Exception as e:
        log.error(f"CICO dashboard update error: {e}")
        _post_slack(f":warning: CICO dashboard update error: {e}")


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------

def run_receivable_forecast():
    """Daily receivable balance forecast — WEEKLY, MONDAYS, after the NB borrowing base.

    Cadence set by Brian 2026-09-11 ("systemize it so we can run it on a weekly basis,
    retrain using all available data"). Delegates to the model of record in cfo-org-kb:

      scripts/receivable_balance_weekly_run.py
        1. pulls the daily balance/flow panel from Redshift to the last COMPLETE tape day
        2. refits the pooled XGBoost (direct multi-horizon, log-ratio target, MAE) on
           ALL available history; bias corrections refit monthly from out-of-fold folds
        3. forecasts every day to month end, immaterial series carried flat
        4. re-runs a rolling 40-day hold-out so the quoted accuracy is never stale
        5. writes workbook + two charts, uploads to Drive, posts the summary to Slack

    The script posts its own Slack summary with the Drive links; this wrapper only
    announces the start and raises on a non-zero exit so the supervisor marks it failed.
    """
    import subprocess

    skills_dir = os.path.dirname(os.path.abspath(__file__))
    script = os.path.abspath(os.path.join(skills_dir, '..', 'cfo-org-kb', 'scripts',
                                          'receivable_balance_weekly_run.py'))
    log.info(f"run_receivable_forecast: launching {script}")
    _post_slack(":chart_with_upwards_trend: Running scheduled *receivable balance forecast* "
                "(retrain on all history, forecast to month end)...")

    env = os.environ.copy()
    env['PYTHONIOENCODING'] = 'utf-8'
    r = subprocess.run([sys.executable, script], capture_output=True, text=True,
                       timeout=5400, encoding='utf-8', errors='replace', env=env,
                       stdin=subprocess.DEVNULL)
    out = ((r.stdout or '') + (r.stderr or '')).strip()
    if r.returncode != 0:
        raise RuntimeError(f"receivable forecast failed (rc={r.returncode}):\n{out[-1500:]}")
    log.info(f"run_receivable_forecast: OK\n{out[-800:]}")


def run_aig_tex_file():
    """Build the weekly AIG/Aronova TEX invoice-history file and post it to Brian's DM.

    Cadence set by Brian 2026-09-16: Mondays ~10:30am PT, no email. The file still has to
    be uploaded to Aronova's SFTP (/puts) by hand -- this machine holds NO ftp-tex
    credentials, and the Airflow DAG (airflow-dags PR #542, merged 2026-09-09) is still
    waiting on the aronova_tex_sftp connection + the sftp provider in the image. So the
    job's deliverable is a validated file sitting in Slack, ready to drag onto the SFTP.

    Pipeline:
      1. sql_runner.py on cfo-org-kb/sql/aig_invoice_data_v8_tex_validated.sql -> CSV
      2. aig_tex_builder.build_tex_dataframe / write_tex_file -> Jeeves-Invoice-{stamp}.txt
      3. aig_tex_builder.validate_tex_file -- HARD GATE. Any error and nothing is posted
         except the failure, because this file lands in a counterparty's platform.
      4. slack_tool.py upload -> the .txt as a real attachment in Brian's DM, with the
         row count and the per-currency breakdown in the comment.
    """
    import importlib.util, subprocess

    now_local   = datetime.now()
    stamp       = now_local.strftime('%Y%m%d-%H%M')
    yyyymmdd    = now_local.strftime('%Y%m%d')
    outputs_dir = os.environ.get('OUTPUTS_PATH', str(Path(__file__).resolve().parent)).replace(chr(92), '/')
    skills_dir  = os.path.dirname(os.path.abspath(__file__))
    owner_uid   = (os.environ.get('SLACK_OWNER_USER_ID')
                   or os.environ.get('SLACK_OWNER_ID') or 'U05B5HGNCN9')
    # files_upload_v2 needs a CONVERSATION id (^[CGDZ]...), not a user id -- passing
    # U05B5HGNCN9 fails with invalid_arguments (proved on the 2026-09-16 dry run).
    # Resolve the DM channel once, here, and fall back to the user id so a
    # conversations.open hiccup does not kill the build.
    owner_dm = owner_uid
    try:
        sys.path.insert(0, os.path.join(skills_dir, '..', '_shared'))
        from notify import get_client
        owner_dm = get_client().conversations_open(users=[owner_uid])['channel']['id']
    except Exception as _e:
        log.warning('run_aig_tex_file: could not resolve owner DM channel: ' + str(_e))

    sql_path = os.path.join(skills_dir, '..', 'cfo-org-kb', 'sql',
                            'aig_invoice_data_v8_tex_validated.sql')
    csv_path = os.path.join(outputs_dir, 'aig_tex_raw_' + yyyymmdd + '.csv')
    txt_path = os.path.join(outputs_dir, 'Jeeves-Invoice-' + stamp + '.txt')

    log.info('run_aig_tex_file: building AIG TEX file ' + os.path.basename(txt_path))

    try:
        # -- 1. Pull the v8 tape -------------------------------------------------
        runner = os.path.join(skills_dir, '..', 'jeeves-redshift', 'sql_runner.py')
        env = os.environ.copy()
        env['PYTHONIOENCODING'] = 'utf-8'
        r = subprocess.run([sys.executable, runner, '--file', sql_path, '--output', csv_path,
                            '--statement-timeout', '900'],
                           capture_output=True, text=True, timeout=1800,
                           encoding='utf-8', errors='replace', env=env)
        if r.returncode != 0 or not os.path.exists(csv_path):
            raise RuntimeError('v8 query failed (rc=' + str(r.returncode) + '): '
                               + ((r.stdout or '') + (r.stderr or ''))[-800:])

        # -- 2/3. Format and validate -------------------------------------------
        import pandas as pd
        spec = importlib.util.spec_from_file_location(
            'aig_tex_builder', os.path.join(skills_dir, 'aig_tex_builder.py'))
        builder = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(builder)

        raw = pd.read_csv(csv_path, dtype=str, keep_default_na=False)
        df = builder.build_tex_dataframe(raw)
        builder.write_tex_file(df, txt_path)
        errors, stats = builder.validate_tex_file(txt_path)
        if errors:
            raise RuntimeError('TEX validation failed, file NOT posted: ' + '; '.join(errors))

        # -- 3.5 Upload to Aronova SFTP /puts and verify remote size -------------
        # Added 2026-09-28: the job previously stopped at Slack and nothing reached
        # Aronova from 9/8 to 9/28. A failure here raises -> :warning: to Brian.
        import paramiko
        sftp_dir  = os.environ.get('AIG_SFTP_REMOTE_DIR', 'puts')
        remote    = sftp_dir + '/' + os.path.basename(txt_path)
        local_sz  = os.path.getsize(txt_path)
        tr = paramiko.Transport((os.environ['AIG_SFTP_HOST'], int(os.environ.get('AIG_SFTP_PORT', '22'))))
        try:
            tr.connect(username=os.environ['AIG_SFTP_USER'], password=os.environ['AIG_SFTP_PASSWORD'])
            sf = paramiko.SFTPClient.from_transport(tr)
            sf.put(txt_path, remote)
            remote_sz = sf.stat(remote).st_size
            sf.close()
        finally:
            tr.close()
        if remote_sz != local_sz:
            raise RuntimeError('SFTP size mismatch for ' + remote + ': local '
                               + str(local_sz) + ' vs remote ' + str(remote_sz))
        log.info('run_aig_tex_file: SFTP upload OK ' + remote + ' (' + str(remote_sz) + ' bytes)')

        # -- 4. Per-currency breakdown (amounts are mixed currency, never summed) --
        num = df.copy()
        num['invoicedamount'] = pd.to_numeric(num['invoicedamount'], errors='coerce')
        by_ccy = num.groupby('currency').agg(rows=('statement_id', 'count'),
                                             invoiced=('invoicedamount', 'sum'))
        by_ccy = by_ccy.sort_values('rows', ascending=False)
        ccy_txt = '  |  '.join(
            str(c) + ' ' + format(int(v['rows']), ',') + ' rows / ' + format(v['invoiced'], ',.0f')
            for c, v in by_ccy.iterrows())
        open_rows = int((df['settlementdatetime'].astype(str).str.strip() == '').sum())

        comment = (
            ':inbox_tray: *AIG / TEX weekly file -- ' + now_local.strftime('%a %d %b %Y') + '*'
            + chr(10) + '`' + os.path.basename(txt_path) + '`  -- '
            + format(stats['rows'], ',') + ' rows, ' + str(stats['fields'])
            + ' fields, validation clean, zip populated '
            + str(stats.get('zip_populated_pct', 0)) + '%, ' + format(open_rows, ',')
            + ' open invoices.' + chr(10)
            + 'Invoiced by currency (local, no FX): ' + ccy_txt + chr(10)
            + ':white_check_mark: Uploaded to Aronova SFTP `' + remote + '` -- remote size '
            + format(remote_sz, ',') + ' bytes matches local.'
        )

        up = os.path.join(skills_dir, '..', 'slack-search', 'slack_tool.py')
        r2 = subprocess.run([sys.executable, up, 'upload', '--channel', owner_dm,
                             '--comment', comment, txt_path],
                            capture_output=True, text=True, timeout=300,
                            encoding='utf-8', errors='replace', env=env)
        if r2.returncode != 0:
            raise RuntimeError('Slack upload failed: ' + ((r2.stdout or '') + (r2.stderr or ''))[-500:])

        log.info('run_aig_tex_file: done. ' + str(stats))

    except Exception as exc:
        log.error('run_aig_tex_file failed: ' + str(exc), exc_info=True)
        _post_slack(
            ':warning: *AIG / TEX weekly file* FAILED -- check whether it reached Aronova SFTP.'
            + chr(10) + '`' + str(exc)[:600] + '`'
        )


def run_loop():
    log.info(f"Report scheduler started. Interval={CHECK_INTERVAL}s, max_task={MAX_TASK_SECONDS}s.")
    _reconcile_stale_running(datetime.now().date().isoformat())
    while True:
        try:
            with _state_lock:
                state = load_state()

            now       = datetime.now()
            today_str = now.date().isoformat()
            weekday   = now.weekday()  # 0=Mon … 4=Fri, 5=Sat, 6=Sun
            is_weekend = weekday >= 5

            if now.hour >= 8:

                # ── Portfolio report ──────────────────────────────────────
                # Fire once per month on the 2nd only.
                month_key = f"portfolio_{now.strftime('%Y%m')}"
                if (now.day == 2
                        and not _state_completed_today(state, month_key, today_str)
                        and not state.get(month_key, '').endswith('_running')):
                    _launch_background('portfolio_report', run_portfolio_report, month_key, today_str)

                # ── US Borrowing Base ─────────────────────────────────────
                # DISABLED 2026-08-10: the CIM US bridge facility is TERMINATED and
                # replaced by the NB facility (Brian, 2026-08-10). Bridge borrowing bases
                # are no longer produced. Replacement cadence is an NB borrowing base every
                # Monday, as of the prior Sunday (Brian, 2026-08-11). To re-enable temporarily, set env BRIDGE_BB_ENABLED=1.
                if (os.environ.get('BRIDGE_BB_ENABLED') == '1'
                        and not is_weekend
                        and not _state_completed_today(state, 'last_bb', today_str)
                        and not state.get('last_bb', '').endswith('_running')):
                    _launch_background('weekly_bb', run_weekly_bb, 'last_bb', today_str)

                # ── MX Borrowing Base (SOFOM) ──────────────────
                # Every day at 8:30am PST.
                if (now.hour > 8 or (now.hour == 8 and now.minute >= 30)):
                    # 2026-09-14: a '_failed' marker blocks every later tick, so one
                    # transient fault (Drive slow, Zscaler blip) used to cost the whole
                    # day's tape. Allow up to 2 automatic same-day retries; after that it
                    # stays failed and needs a human, as before.
                    _mx_val = state.get('last_mx_bb', '')
                    _mx_retry = False
                    if _mx_val == f"{today_str}_failed":
                        _rk = state.get('mx_bb_retries', '')
                        _n = int(_rk.split(':')[1]) if _rk.startswith(today_str + ':') else 0
                        if _n < 2:
                            _mx_retry = True
                            with _state_lock:
                                _s = load_state()
                                _s['mx_bb_retries'] = f"{today_str}:{_n + 1}"
                                _s.pop('last_mx_bb', None)
                                save_state(_s)
                            log.warning(f"mx_bb: auto-retry {_n + 1}/2 after failed run")
                    if _mx_retry or (not _state_completed_today(state, 'last_mx_bb', today_str)
                            and not _mx_val.endswith('_running')):
                        _launch_background('mx_bb', run_mx_bb, 'last_mx_bb', today_str)


                # ── NB Borrowing Base ───────────────────────────────
                # Weekly, MONDAYS at 8:30am PT, as of the prior day (Sunday).
                # Cadence set by Brian 2026-08-11, re-confirmed 2026-08-17.
                if (weekday == 0
                        and (now.hour > 8 or (now.hour == 8 and now.minute >= 30))
                        and not _state_completed_today(state, 'last_nb_bb', today_str)
                        and not state.get('last_nb_bb', '').endswith('_running')):
                    _launch_background('nb_bb', run_nb_bb, 'last_nb_bb', today_str)

                # ── Receivable balance forecast ───────────────────────────
                # Weekly, MONDAYS from 9:30am PT — deliberately after the NB BB job so the
                # two heavy Redshift pulls do not overlap. Retrains on all available
                # history and forecasts to month end. Cadence set by Brian 2026-09-11.
                if (weekday == 0
                        and (now.hour > 9 or (now.hour == 9 and now.minute >= 30))
                        and not _state_completed_today(state, 'last_receivable_forecast', today_str)
                        and not state.get('last_receivable_forecast', '').endswith('_running')):
                    _launch_background('receivable_forecast', run_receivable_forecast,
                                       'last_receivable_forecast', today_str)

                # -- AIG / Aronova TEX invoice-history file ------------------
                # Weekly, MONDAYS from 10:30am PT (Brian, 2026-09-16: "just set this
                # up as a weekly monday cron, like 1030am. no email needed"). Runs
                # after the NB BB (8:30) and the receivable forecast (9:30) so the
                # three heavy Redshift pulls do not overlap. Posts the validated .txt
                # to Brian's DM; the SFTP upload to Aronova stays manual until the
                # Airflow DAG has its aronova_tex_sftp connection.
                if (weekday == 0
                        and (now.hour > 10 or (now.hour == 10 and now.minute >= 30))
                        and not _state_completed_today(state, 'last_aig_tex', today_str)
                        and not state.get('last_aig_tex', '').endswith('_running')):
                    _launch_background('aig_tex_file', run_aig_tex_file,
                                       'last_aig_tex', today_str)

                # ── Morning Capital Markets Brief ─────────────────────────
                # Weekdays only, once per day at 8am — posts BB + CICO snapshot to Brian.
                if (not is_weekend
                        and not _state_completed_today(state, 'last_morning_brief', today_str)
                        and not state.get('last_morning_brief', '').endswith('_running')):
                    _launch_background('morning_brief', run_morning_brief,
                                       'last_morning_brief', today_str)

                # ── CICO Dashboard ───────────────────────────────────────
                # Weekdays only — update after 8am using previous day's email.
                if (not is_weekend
                        and not _state_completed_today(state, 'last_cico_dashboard', today_str)
                        and not state.get('last_cico_dashboard', '').endswith('_running')):
                    _launch_background('cico_dashboard', run_cico_dashboard,
                                       'last_cico_dashboard', today_str)

            # ── SOFOM distribution tape ───────────────────────────────────
            # Deliberately OUTSIDE the `now.hour >= 8` and weekday gates. The
            # real trigger is the Pub/Sub push (webhook_receiver's SOFOM
            # fast-path), which fires the moment an email matching the subject
            # arrives — whatever the hour, whatever the day, whoever sent it.
            # This poll exists only to catch a push that never landed (a lapsed
            # Gmail watch has cost 33 days before), so gating it by office hours
            # or weekdays would just reintroduce the blind spot it's here to
            # cover. Identification is by subject alone — see SOFOM_SUBJECT_RE.
            sofom_key = 'last_sofom_distribution'
            # _sofom_blocked_today, not _state_completed_today: a run that
            # failed earlier today (usually a Zscaler/Redshift blip) must be
            # retryable, or one transient error silently costs the entire day.
            if not _sofom_blocked_today(state, sofom_key, today_str):
                eml = _find_sofom_distribution_email(today_str)
                if eml:
                    log.info(f"SOFOM distribution email found by backstop poll: {eml['subject']}")
                    # Mark running synchronously (direct fn, not threaded).
                    # finally-stamp like _launch_background does: without
                    # it, an exception here leaves '{today}_running' stuck
                    # and silently blocks retries until the next restart's
                    # reconcile pass (GW-F9).
                    with _state_lock:
                        state = load_state()
                        state[sofom_key] = f"{today_str}_running"
                        # Records WHICH message we acted on, so the webhook
                        # fast-path can tell a duplicate push of this same
                        # email (skip) from a reply on the thread (classify).
                        state[f'{sofom_key}_msg_id'] = eml['message_id']
                        save_state(state)
                    ok = False
                    try:
                        ok = bool(run_sofom_distribution(eml['message_id'], eml['subject']))
                    finally:
                        with _state_lock:
                            state = load_state()
                            # Record the real outcome. Stamping the bare
                            # date on failure is what turned a 90-second
                            # outage into a lost day on 2026-08-17.
                            state[sofom_key] = today_str if ok else f"{today_str}_failed"
                            save_state(state)
                    if not ok:
                        log.warning('SOFOM distribution failed — will retry on the next tick.')
                else:
                    # Cheap, but it is the only evidence the backstop is alive.
                    # A silently-skipped check is exactly how 2026-08-17 hid.
                    log.info('SOFOM backstop: no matching distribution email today yet.')

        except Exception as e:
            log.error(f"Scheduler loop error: {e}")
            traceback.print_exc()

        time.sleep(CHECK_INTERVAL)


if __name__ == '__main__':
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    if hasattr(sys.stderr, 'reconfigure'):
        sys.stderr.reconfigure(encoding='utf-8', errors='replace')

    import argparse
    parser = argparse.ArgumentParser(description='Report Scheduler Cron')
    parser.add_argument('mode', nargs='?',
                        choices=['portfolio', 'bb', 'mx-bb', 'sofom-distribution'],
                        help='Run once in this mode instead of looping')
    args = parser.parse_args()

    if args.mode == 'portfolio':
        run_portfolio_report()
    elif args.mode == 'bb':
        run_weekly_bb()
    elif args.mode == 'mx-bb':
        run_mx_bb()
    elif args.mode == 'sofom-distribution':
        today_str = datetime.now().date().isoformat()
        eml = _find_sofom_distribution_email(today_str)
        if eml:
            run_sofom_distribution(eml['message_id'], eml['subject'])
        else:
            print('No distribution email from Axel found for today.')
    else:
        run_loop()


# ---------------------------------------------------------------------------
# Month-folder auto-create (added 2026-07-02 -- fixes recurring month-rollover
# failure: "Could not find the {yyyymm}/ folder under Debt/CIM in Drive".
# Nobody pre-creates next month's Debt/CIM/Reporting/{yyyymm}/ folder, so the
# US BB and MX BB crons died every 1st of the month. This creates it instead.
# ---------------------------------------------------------------------------
_CIM_REPORTING_PARENT_ID = '1oDi6HjzArnX_jPQbI9UfEs4R5DC9TTrA'


def _ensure_month_folder(yyyymm):
    """Return the Drive folder ID for Debt/CIM/Reporting/{yyyymm}/, creating it if missing."""
    sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', '_shared'))
    from env_loader import load_env
    load_env()
    from google_auth import get_credentials
    from googleapiclient.discovery import build

    service = build('drive', 'v3', credentials=get_credentials())
    q = ("'" + _CIM_REPORTING_PARENT_ID + "' in parents and name = '" + yyyymm +
         "' and mimeType = 'application/vnd.google-apps.folder' and trashed = false")
    existing = service.files().list(q=q, fields='files(id,name)').execute().get('files', [])
    if existing:
        return existing[0]['id']

    meta = {
        'name': yyyymm,
        'mimeType': 'application/vnd.google-apps.folder',
        'parents': [_CIM_REPORTING_PARENT_ID],
    }
    folder = service.files().create(body=meta, fields='id,name').execute()
    log.warning("Created missing Drive folder Debt/CIM/Reporting/" + yyyymm + "/ (" + folder['id'] + ")")
    _post_slack(":file_folder: Auto-created missing `" + yyyymm + "/` folder under Debt/CIM/Reporting (month rollover).")
    return folder['id']
