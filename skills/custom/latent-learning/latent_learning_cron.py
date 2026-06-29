#!/usr/bin/env python3
"""Latent-learning cron — periodically triggers the latent-learning skill so the
analyst reviews its own task history and DRAFTS specialist sub-agent specs.

This is the gateway-cron (Windows, in-process under cron_supervisor) replacement
for the original omnigent WSL systemd timer (scripts/latent-learning.{service,timer}
+ latent_learning_cron.sh). It runs on the same host as the rest of the stack and
under the same supervisor — no dependency on the omnigent WSL host.

Behavior: dispatches the same prompt the `!learn` command sends. The skill WRITES
DRAFTS to the agents-draft directory and reports a summary — it never promotes an
agent. Promotion stays a manual, human-gated step (`!promote <name>` in Slack).

Cadence: WEEKLY by default (not nightly). Proposing structural changes — new
specialist agents — is slow-moving; a nightly run would spam Brian with redundant
drafts. Override via env if you want it more/less often.

Env vars:
  - LATENT_LEARN_WEEKDAY (default 6 = Sunday; Python weekday(): Mon=0..Sun=6)
  - LATENT_LEARN_HOUR    (default 3 = 3 AM local)
  - LATENT_LEARN_DISABLE (set to "1" to disable without unwiring)
  - LANGGRAPH_URL        (used by dispatch_queue, default http://localhost:2024)
"""

import json
import logging
import os
import sys
import time
import traceback
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / '_shared'))
from env_loader import load_env
load_env()

logging.basicConfig(
    level=logging.INFO,
    format='[LATENT-LEARN %(asctime)s] %(message)s',
    datefmt='%H:%M:%S',
)
log = logging.getLogger('latent_learning_cron')

LEARN_WEEKDAY = int(os.environ.get('LATENT_LEARN_WEEKDAY', '6'))   # Sunday
LEARN_HOUR    = int(os.environ.get('LATENT_LEARN_HOUR', '3'))      # 3 AM
DISABLED      = os.environ.get('LATENT_LEARN_DISABLE', '').strip() == '1'
CHECK_INTERVAL_SECS = 3600  # hourly; act only at the target weekday+hour


def _state_path() -> Path:
    here = Path(__file__).resolve()
    return here.parents[3] / 'backend' / '.deer-flow' / '_latent_learning_state.json'


def load_state() -> dict:
    p = _state_path()
    if p.exists():
        try:
            with open(p) as f:
                return json.load(f)
        except Exception:
            pass
    return {'last_run': None, 'run_count': 0}


def save_state(state: dict) -> None:
    p = _state_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    state['last_run'] = datetime.now().isoformat()
    with open(p, 'w') as f:
        json.dump(state, f, indent=2)


def _already_ran_today(state: dict) -> bool:
    last = state.get('last_run')
    if not last:
        return False
    try:
        return datetime.fromisoformat(last).date() == datetime.now().date()
    except Exception:
        return False


def _build_prompt(run_number: int) -> str:
    today = datetime.now().strftime('%A, %B %d %Y')
    return (
        "LATENT-LEARNING REVIEW #" + str(run_number) + " -- " + today + "\n\n"
        "Run the latent-learning skill now. Analyse your memory facts and recently "
        "completed work to identify recurring task domains where a dedicated "
        "specialist sub-agent would help. For each domain, draft a specialist "
        "sub-agent spec and write it to the agents-draft directory. Do NOT promote "
        "anything — promotion is Brian's call via `!promote <name>`.\n\n"
        "Report back a concise summary: which domains you saw, which drafts you "
        "wrote (names + one-line descriptions), and which existing specialists "
        "already cover a domain so you skipped it. If nothing new is warranted, say "
        "so in one line rather than inventing drafts."
    )


def run_review() -> None:
    state = load_state()
    run_number = state.get('run_count', 0) + 1
    prompt = _build_prompt(run_number)
    notification = (
        "Latent-learning review starting -- analysing task history for recurring "
        "domains and drafting specialist agents. Summary to follow."
    )
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent / '_shared'))
        from dispatch_queue import enqueue_or_dispatch

        dispatched = enqueue_or_dispatch(
            prompt,
            notification=notification,
            category="Latent Learning",
            source_id="latent-learning-" + datetime.now().strftime('%Y%m%d'),
            source_metadata={"run_number": run_number},
        )
        if dispatched:
            state['run_count'] = run_number
            save_state(state)
            log.info("Latent-learning review dispatched (run #%d).", run_number)
        else:
            # Capacity rejection: queued by enqueue_or_dispatch, retried next cycle.
            # Do NOT mark today as done, so it can re-fire within the hour window.
            log.warning("Latent-learning review queued (agent at capacity).")
    except Exception as e:
        log.error("Latent-learning dispatch failed: %s", e)
        traceback.print_exc()


def run_loop() -> None:
    if DISABLED:
        log.info("Latent-learning cron disabled via LATENT_LEARN_DISABLE=1. Idling.")
        while True:
            time.sleep(CHECK_INTERVAL_SECS)
    wd = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun'][LEARN_WEEKDAY]
    log.info("Latent-learning cron started. Triggers %s at %02d:00 local.", wd, LEARN_HOUR)
    while True:
        now = datetime.now()
        if now.weekday() == LEARN_WEEKDAY and now.hour == LEARN_HOUR:
            if not _already_ran_today(load_state()):
                try:
                    run_review()
                except Exception as e:
                    log.error("Latent-learning loop error: %s", e)
                    traceback.print_exc()
            else:
                log.info("Already ran latent-learning today, skipping.")
        time.sleep(CHECK_INTERVAL_SECS)


if __name__ == '__main__':
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    if hasattr(sys.stderr, 'reconfigure'):
        sys.stderr.reconfigure(encoding='utf-8', errors='replace')
    run_loop()
