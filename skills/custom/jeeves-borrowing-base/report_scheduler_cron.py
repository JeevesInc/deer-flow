#!/usr/bin/env python3
"""Scheduled Report Cron — auto-generates recurring reports via the agent.

Schedule:
  - 2nd of each month, 8am+:     Portfolio report for the 1st (once per month)
  - Every weekday, 8am+:         US Borrowing Base (yesterday's date, for dashboard)
  - Every day, 8:30am+ PST:      MX Borrowing Base (yesterday's date)
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


def _state_completed_today(state: dict, key: str, today_str: str) -> bool:
    """True when the task completed OR is already running today.

    Treating '_running' as blocking prevents re-fires on process restart:
    a new process has an empty _running_tasks dict, so the in-memory
    thread check can't guard against duplicate launches — the state file
    is the only persistent guard.
    """
    val = state.get(key, '')
    return val == today_str or val == f"{today_str}_running"


# ---------------------------------------------------------------------------
# Slack
# ---------------------------------------------------------------------------

def _post_slack(text, blocks=None):
    token    = os.environ.get('SLACK_BOT_TOKEN')
    owner_id = os.environ.get('SLACK_OWNER_USER_ID')
    if not token or not owner_id:
        log.warning("Slack not configured.")
        return False
    try:
        from slack_sdk import WebClient
        client     = WebClient(token=token)
        dm         = client.conversations_open(users=[owner_id])
        channel_id = dm['channel']['id']
        client.chat_postMessage(channel=channel_id, text=text, blocks=blocks)
        return True
    except Exception as e:
        log.error(f"Slack post failed: {e}")
        return False


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
        try:
            fn()
        finally:
            with _state_lock:
                s = load_state()
                s[state_key] = today_str
                save_state(s)
            log.info(f"State[{state_key}] = '{today_str}' (completed)")

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

def _find_axel_distribution_email(today_str: str) -> dict | None:
    """Search Gmail for today's SOFOM distribution email from Axel.
    Returns {message_id, thread_id, subject} or None.
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
        query    = f'from:axel@tryjeeves.com subject:Distribution subject:F/12339 after:{today_str}'
        result   = service.users().messages().list(userId='me', q=query, maxResults=5).execute()
        messages = result.get('messages', [])
        if not messages:
            return None
        msg     = service.users().messages().get(
            userId='me', id=messages[0]['id'],
            format='metadata',
            metadataHeaders=['Subject', 'From', 'Date']
        ).execute()
        headers = {h['name']: h['value'] for h in msg.get('payload', {}).get('headers', [])}
        return {
            'message_id': messages[0]['id'],
            'thread_id':  msg.get('threadId'),
            'subject':    headers.get('Subject', ''),
        }
    except Exception as e:
        log.error(f'Gmail search for distribution email failed: {e}')
        return None


# ---------------------------------------------------------------------------
# Report tasks
# ---------------------------------------------------------------------------

def run_sofom_distribution(message_id: str, subject: str):
    """Generate SOFOM data tape and draft reply to Axel's distribution thread.

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

        _post_slack(
            f':white_check_mark: *SOFOM tape — {subject}*\n'
            f'`{fname}` ({total_rows:,} rows, {three_days_ago} → {yesterday})\n'
            f'Draft reply ready in Gmail. Review and send.'
        )
        log.info('SOFOM distribution completed.')

    except Exception as e:
        log.error(f'SOFOM distribution failed: {e}', exc_info=True)
        _post_slack(
            f':warning: SOFOM tape failed for {repr(subject)}: `{e}`\n'
            f'Run manually: reply to `{message_id}` with 3-day tape ({three_days_ago} → {yesterday}).'
        )


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
        listing = _run(
            [sys.executable, list_script, '1-0K8EM8slr1_I4Iik7_ZZn0t4SSAMKLU',
             '--recursive', '--max-depth', '4'],
            'list_drive', timeout=120
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
            raise RuntimeError(f"Could not find the {yyyymm}/ folder under Debt/CIM in Drive")
        log.info(f"  Upload target: {yyyymm}/ ({month_folder_id})")

        # -- 5. Upload (--new is REQUIRED so the dated master isn't overwritten) -
        upload_script = os.path.join(skills_dir, '..', 'google-drive', 'upload_to_drive.py')
        upload_out = _run(
            [sys.executable, upload_script, final_wb, '--folder', month_folder_id, '--new'],
            'upload', timeout=120
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

        msg = (
            f":white_check_mark: *MX Borrowing Base -- {yesterday}*\n"
            + (f"{stats}\n" if stats else "")
            + (f"<{drive_link}|Open in Drive>" if drive_link else f"Uploaded to Drive/CIM/{yyyymm}/")
        )
        _post_slack(msg)
        log.info(f"run_mx_bb: done. {stats}")

    except Exception as exc:
        log.error(f"run_mx_bb failed: {exc}", exc_info=True)
        _post_slack(
            f":warning: *MX Borrowing Base -- {yesterday}* failed to generate.\n"
            f"`{str(exc)[:400]}`\nRun manually: `build_mx.py --end-date {yesterday}`"
        )


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
            raise RuntimeError(f"Could not find the {yyyymm}/ folder under Debt/CIM in Drive")

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
    """Post daily Capital Markets brief to Brian's Slack DM (~8am weekdays)."""
    import subprocess
    log.info("Posting morning Capital Markets brief...")
    script = str(pathlib.Path(__file__).resolve().parent.parent / 'scripts' / 'cm_morning_brief.py')
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

def run_loop():
    log.info(f"Report scheduler started. Interval={CHECK_INTERVAL}s, max_task={MAX_TASK_SECONDS}s.")
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
                # Every weekday (used by dashboard).
                if (not is_weekend
                        and not _state_completed_today(state, 'last_bb', today_str)
                        and not state.get('last_bb', '').endswith('_running')):
                    _launch_background('weekly_bb', run_weekly_bb, 'last_bb', today_str)

                # ── MX Borrowing Base (SOFOM) ──────────────────
                # Every day at 8:30am PST.
                if (now.hour > 8 or (now.hour == 8 and now.minute >= 30)):
                    if not _state_completed_today(state, 'last_mx_bb', today_str) \
                            and not state.get('last_mx_bb', '').endswith('_running'):
                        _launch_background('mx_bb', run_mx_bb, 'last_mx_bb', today_str)


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

                # ── SOFOM distribution tape ───────────────────────────────
                # Weekdays only — Axel doesn't send on weekends.
                # Fires once per day as soon as his email is detected.
                if not is_weekend:
                    sofom_key = 'last_sofom_distribution'
                    if not _state_completed_today(state, sofom_key, today_str):
                        eml = _find_axel_distribution_email(today_str)
                        if eml:
                            log.info(f"Found Axel distribution email: {eml['subject']}")
                            # Mark running synchronously (direct fn, not threaded)
                            with _state_lock:
                                state = load_state()
                                state[sofom_key] = f"{today_str}_running"
                                save_state(state)
                            run_sofom_distribution(eml['message_id'], eml['subject'])
                            with _state_lock:
                                state = load_state()
                                state[sofom_key] = today_str
                                save_state(state)
                        else:
                            log.info('No Axel distribution email yet — retrying next hour.')
                else:
                    log.info('Weekend — skipping SOFOM distribution check.')

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
        eml = _find_axel_distribution_email(today_str)
        if eml:
            run_sofom_distribution(eml['message_id'], eml['subject'])
        else:
            print('No distribution email from Axel found for today.')
    else:
        run_loop()
