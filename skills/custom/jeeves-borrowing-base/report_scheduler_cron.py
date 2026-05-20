#!/usr/bin/env python3
"""Scheduled Report Cron — auto-generates recurring reports via the agent.

Schedule:
  - 2nd–5th of each month, 8am+: Portfolio report for the 1st (retries if
    process was down on the 2nd)
  - Every Monday, 8am+:          US Borrowing Base (yesterday's date)
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
    """True only when the task completed successfully today (not just started)."""
    return state.get(key) == today_str


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
    _post_slack(f':mexico: Running SOFOM distribution tape for *{subject}*...')

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


def run_weekly_bb():
    """Trigger US Borrowing Base using yesterday's date."""
    yesterday = (datetime.now().date() - timedelta(days=1)).isoformat()

    log.info(f"Triggering weekly US BB for {yesterday}")
    _post_slack(f":calendar: Running scheduled *US Borrowing Base* for {yesterday}...")

    task = (
        f"Run the US borrowing base for {yesterday}. "
        f"Upload the completed file to Drive and share the link here."
    )
    result = _run_agent_task(task)

    if result:
        _post_slack(f":white_check_mark: *US Borrowing Base — {yesterday}*\n{result[:3000]}")
        log.info("Weekly BB completed.")
    else:
        _post_slack(
            f":warning: *US Borrowing Base — {yesterday}* failed to generate. "
            f"Run it manually."
        )
        log.error("Weekly BB failed.")


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
                # Fire on days 2–5 of the month so a down-day on the 2nd
                # doesn't silently miss the whole month.
                month_key = f"portfolio_{now.strftime('%Y%m')}"
                if (2 <= now.day <= 5
                        and not _state_completed_today(state, month_key, today_str)
                        and not state.get(month_key, '').endswith('_running')):
                    _launch_background('portfolio_report', run_portfolio_report, month_key, today_str)

                # ── US Borrowing Base ─────────────────────────────────────
                # Mondays only.
                if (weekday == 0
                        and not _state_completed_today(state, 'last_bb', today_str)
                        and not state.get('last_bb', '').endswith('_running')):
                    _launch_background('weekly_bb', run_weekly_bb, 'last_bb', today_str)

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
                        choices=['portfolio', 'bb', 'sofom-distribution'],
                        help='Run once in this mode instead of looping')
    args = parser.parse_args()

    if args.mode == 'portfolio':
        run_portfolio_report()
    elif args.mode == 'bb':
        run_weekly_bb()
    elif args.mode == 'sofom-distribution':
        today_str = datetime.now().date().isoformat()
        eml = _find_axel_distribution_email(today_str)
        if eml:
            run_sofom_distribution(eml['message_id'], eml['subject'])
        else:
            print('No distribution email from Axel found for today.')
    else:
        run_loop()
