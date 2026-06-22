#!/usr/bin/env python3
"""Monthly diligence registry refresh cron.

Runs inside the gateway via cron_supervisor (expects a blocking run_loop()).

Behavior:
    - Polls hourly.
    - Runs the refresh on the 1st of each month, or whenever the last
      successful run is more than 35 days old (covers gateway downtime
      spanning the 1st).
    - Executes diligence_registry_refresh.py --dry-run as a subprocess:
      crawls all counterparty Drive folders, diffs against the canonical
      registry in Drive Debt/ root, and writes a dated Refresh Summary.
      Discovery only — the registry Excel is hand-curated (status/owner/
      notes), so the cron never rebuilds or re-uploads it; new items get
      DM'd to Brian for triage instead.

State: backend/.deer-flow/_diligence_registry_state.json
"""

import json
import logging
import os
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

logger = logging.getLogger(__name__)

_SCRIPT_DIR = Path(__file__).resolve().parent
_BACKEND_DIR = _SCRIPT_DIR.parent.parent.parent / "backend"
_STATE_FILE = _BACKEND_DIR / ".deer-flow" / "_diligence_registry_state.json"
_REFRESH_SCRIPT = _SCRIPT_DIR / "diligence_registry_refresh.py"
_OUTPUTS_DIR = _BACKEND_DIR / ".deer-flow" / "diligence"

CHECK_INTERVAL_SECS = 3600
MAX_AGE_DAYS = 35  # force a run if the last success is older than this


def _load_state() -> dict:
    try:
        with open(_STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _save_state(state: dict) -> None:
    _STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(_STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2)


def _days_since_last_run(state: dict) -> float:
    last = state.get("last_success")
    if not last:
        return float("inf")
    try:
        return (datetime.now() - datetime.fromisoformat(last)).total_seconds() / 86400
    except ValueError:
        return float("inf")


def _due(state: dict) -> bool:
    days = _days_since_last_run(state)
    if days > MAX_AGE_DAYS:
        return True
    # On the 1st, run once (last_success not from today)
    if datetime.now().day == 1 and days >= 1:
        return True
    return False


def _dm_owner(text: str) -> None:
    token = os.environ.get("SLACK_BOT_TOKEN")
    owner = os.environ.get("SLACK_OWNER_USER_ID")
    if not token or not owner:
        return
    try:
        from slack_sdk import WebClient
        client = WebClient(token=token)
        ch = client.conversations_open(users=[owner])["channel"]["id"]
        client.chat_postMessage(channel=ch, text=text)
    except Exception as e:
        logger.warning("[diligence-registry] Slack DM failed: %s", e)


def _run_refresh() -> None:
    _OUTPUTS_DIR.mkdir(parents=True, exist_ok=True)
    env = dict(os.environ, DILIGENCE_OUTPUTS_PATH=str(_OUTPUTS_DIR), PYTHONIOENCODING="utf-8")
    logger.info("[diligence-registry] running monthly refresh")
    result = subprocess.run(
        [sys.executable, str(_REFRESH_SCRIPT), "--dry-run"],
        capture_output=True, text=True, timeout=1800, env=env,
        cwd=str(_SCRIPT_DIR),
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"refresh exited {result.returncode}: {(result.stderr or result.stdout)[-800:]}"
        )

    state = _load_state()
    state["last_success"] = datetime.now().isoformat()
    state["last_output_tail"] = result.stdout[-2000:]
    _save_state(state)

    # DM the summary file to Brian
    date_str = datetime.now().strftime("%Y%m%d")
    summary_path = _OUTPUTS_DIR / f"Diligence Refresh Summary - {date_str}.txt"
    if summary_path.exists():
        summary = summary_path.read_text(encoding="utf-8", errors="replace")
        if len(summary) > 3200:
            summary = summary[:3200] + "\n... (truncated, full file: " + str(summary_path) + ")"
        _dm_owner(
            ":card_index_dividers: *Monthly Diligence Registry discovery complete*\n"
            "New items below are NOT yet in the canonical registry — ask the analyst to "
            "triage them into `Diligence Registry - Capital Markets` when you've reviewed.\n"
            "```" + summary + "```"
        )
    else:
        _dm_owner(":card_index_dividers: Monthly Diligence Registry refresh ran, but no summary file was produced — check the gateway log.")
    logger.info("[diligence-registry] refresh complete")


def run_loop() -> None:
    logger.info("[diligence-registry] cron started (hourly poll, runs on the 1st or when >%d days stale)", MAX_AGE_DAYS)
    while True:
        try:
            if _due(_load_state()):
                _run_refresh()
        except Exception:
            # Let cron_supervisor's crash handling alert + restart with backoff
            raise
        time.sleep(CHECK_INTERVAL_SECS)


if __name__ == "__main__":
    # Manual one-shot for testing: python diligence_registry_cron.py --once
    logging.basicConfig(level=logging.INFO)
    if "--once" in sys.argv:
        _run_refresh()
    else:
        run_loop()
