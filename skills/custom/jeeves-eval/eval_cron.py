#!/usr/bin/env python3
"""Weekly classifier eval — keeps the quality metric from going stale.

A one-shot eval tells you where you stand today. It cannot tell you that last
Tuesday's prompt edit cost you six points of recall, which is the failure this
whole thing exists to catch. So it runs on a schedule and the score trends.

Runs the golden set through the live classifier, writes
`.deer-flow/_eval_state.json` (the source for the jeeves_eval_* gauges and the
"Jeeves Agent Quality" Grafana dashboard), and DMs Brian only when something
moved materially — per the standing rule that background jobs are silent unless
there is a problem worth acting on.

Cost: one classifier call per case plus one judge call per judged case. At the
default 60-case set that is roughly 70-90 Claude calls a week.

Env vars:
  EVAL_CRON_DISABLE    "1" to idle without unwiring
  EVAL_CRON_WEEKDAY    default 6 (Sunday; Python weekday(): Mon=0..Sun=6)
  EVAL_CRON_HOUR       default 4 (4 AM local — after dreams ~02:00 and the
                       03:00 backup, before the workday)
  EVAL_ALERT_DELTA     default 0.05 — absolute F1/recall drop that triggers a DM
  EVAL_CRON_NO_JUDGE   "1" to skip LLM judging (cheaper, no action score)
"""

import logging
import os
import sys
import time
import traceback
from datetime import datetime
from pathlib import Path

_HERE = Path(__file__).resolve()
_SHARED = _HERE.parents[1] / "_shared"
# Module-level, not inside a function: the cron supervisor loads this file BY
# PATH, so neither its own directory nor _shared is on sys.path at import time.
for _p in (_SHARED, _HERE.parent):
    if _p.exists() and str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from env_loader import load_env  # noqa: E402

load_env()

logging.basicConfig(
    level=logging.INFO,
    format="[EVAL %(asctime)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("eval_cron")

EVAL_WEEKDAY = int(os.environ.get("EVAL_CRON_WEEKDAY", "6"))
EVAL_HOUR = int(os.environ.get("EVAL_CRON_HOUR", "4"))
DISABLED = os.environ.get("EVAL_CRON_DISABLE", "").strip() == "1"
ALERT_DELTA = float(os.environ.get("EVAL_ALERT_DELTA", "0.05"))
NO_JUDGE = os.environ.get("EVAL_CRON_NO_JUDGE", "").strip() == "1"
CHECK_INTERVAL_SECS = 3600


def _alert(text: str) -> None:
    """DM Brian via the canonical send path. Best-effort — a failed
    notification must not fail the run."""
    if not text.strip():
        return
    try:
        sys.path.insert(0, str(_HERE.parents[1] / "slack-search"))
        from slack_tool import cmd_send

        # Read the owner id from env every time rather than hardcoding it —
        # slack_tool resolves the same way and a baked-in id drifts.
        owner = os.environ.get("SLACK_OWNER_USER_ID", "").strip()
        if not owner:
            log.warning("SLACK_OWNER_USER_ID unset — cannot DM the eval alert.")
            return
        cmd_send(owner, text)
    except SystemExit:
        # cmd_send exits the process on a bad send; inside a cron thread that
        # would take the whole gateway down with it.
        log.warning("Slack alert rejected by slack_tool.")
    except Exception as e:
        log.warning("Slack alert failed: %s", e)


def run_eval_once() -> dict | None:
    """Score the golden set and alert on a material regression."""
    import run_eval

    golden_path = _HERE.parent / "cases" / "golden_set.json"
    if not golden_path.exists():
        log.error("No golden set at %s — run build_golden_set.py first.", golden_path)
        _alert(
            ":warning: Classifier eval skipped — no golden set on disk. "
            "Run `python build_golden_set.py --size 60` in skills/custom/jeeves-eval."
        )
        return None

    golden = run_eval.load_golden_set(golden_path)
    # Read the previous run BEFORE writing this one, or the comparison is
    # against itself and every week looks flat.
    prev = (run_eval._load_state().get("runs") or [None])[-1]

    from concurrent.futures import ThreadPoolExecutor

    with ThreadPoolExecutor(max_workers=run_eval.CONCURRENCY) as pool:
        rows = list(pool.map(lambda c: run_eval._score_case(c, not NO_JUDGE), golden["cases"]))

    metrics = run_eval.compute_metrics(rows)
    run_eval.write_state(metrics, f"weekly-{datetime.now():%Y-%m-%d}", golden)
    log.info(
        "eval complete: acc=%.3f prec=%.3f rec=%.3f f1=%.3f",
        metrics["accuracy"], metrics["precision"], metrics["recall"], metrics["f1"],
    )

    if prev:
        drops = []
        for key in ("f1", "recall", "precision"):
            before, now = prev.get(key), metrics.get(key)
            if isinstance(before, (int, float)) and isinstance(now, (int, float)):
                if before - now >= ALERT_DELTA:
                    drops.append(f"{key} {before:.3f} → {now:.3f} ({now - before:+.3f})")
        if drops:
            _alert(
                ":chart_with_downwards_trend: *Classifier quality regressed this week*\n"
                + "\n".join(f"• {d}" for d in drops)
                + f"\n\nGolden set: {metrics['n']} cases. "
                f"Confusion: tp={metrics['true_positives']} fp={metrics['false_positives']} "
                f"fn={metrics['false_negatives']} tn={metrics['true_negatives']}.\n"
                "Dashboard: http://localhost:3001/d/jeeves-agent-quality\n"
                "Re-run locally with `python run_eval.py --dump misses.json` to see which cases moved."
            )
    return metrics


def run_loop() -> None:
    if DISABLED:
        log.info("Eval cron disabled via EVAL_CRON_DISABLE=1. Idling.")
        while True:
            time.sleep(CHECK_INTERVAL_SECS)

    from cron_schedule import weekly_run_due
    import run_eval

    wd = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"][EVAL_WEEKDAY]
    log.info("Eval cron started. Triggers %s at %02d:00 local.", wd, EVAL_HOUR)
    while True:
        try:
            latest = run_eval._load_state().get("latest") or {}
            # Catch-up semantics: a week missed to downtime fires on the next
            # tick rather than silently skipping (GW-F10).
            if weekly_run_due(latest.get("ts"), datetime.now(), EVAL_WEEKDAY, EVAL_HOUR):
                run_eval_once()
        except Exception as e:
            log.error("Eval loop error: %s", e)
            traceback.print_exc()
        time.sleep(CHECK_INTERVAL_SECS)


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    if "--once" in sys.argv:
        run_eval_once()
    else:
        run_loop()
