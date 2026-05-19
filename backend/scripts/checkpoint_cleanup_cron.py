"""Hourly checkpoint pruning cron.

Runs the DELETE passes from ``cleanup_checkpoints.py`` against the live
LangGraph checkpoints.db. Skips VACUUM — that holds an exclusive lock and
would block all LangGraph writers for the duration. The DELETE passes are
safe under WAL: writers contend for the writer lock briefly (a few
seconds) but readers continue without blocking.

Why this exists:
    start.sh prune_checkpoints() only runs on supervisor restart. If the
    supervisor stays up for days (or dies but children survive — adoption
    mode), the DB grows unbounded. This cron decouples DELETE cleanup
    from supervisor liveness. VACUUM reclamation still requires a real
    (non-adopting) restart.

Note on on-disk size:
    SQLite reuses freed pages internally before requesting new ones from
    the OS, so the .db file size will not visibly shrink after DELETE-
    only passes. Row counts drop and growth stops — until a VACUUM, the
    file stays at its high-water mark.
"""

from __future__ import annotations

import logging
import sys
import time
from pathlib import Path

logger = logging.getLogger(__name__)

INTERVAL_SEC = 60 * 60  # 1 hour
INITIAL_DELAY_SEC = 5 * 60  # let gateway settle before first run
KEEP_PER_THREAD = 5
MAX_AGE_DAYS = 3

_BACKEND_DIR = Path(__file__).resolve().parent.parent
_DB_PATH = _BACKEND_DIR / ".deer-flow" / "checkpoints.db"
_SCRIPTS_DIR = _BACKEND_DIR / "scripts"

# Importable sibling — cron_supervisor loads us via importlib so our parent
# isn't on sys.path. The script-as-module approach mirrors how this file
# itself is invoked.
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))
from cleanup_checkpoints import db_size_mb, prune  # noqa: E402


def _run_once() -> None:
    if not _DB_PATH.exists():
        logger.info("[checkpoint-cleanup] no DB at %s; skipping", _DB_PATH)
        return
    size_before = db_size_mb(_DB_PATH)
    result = prune(
        db_path=_DB_PATH,
        keep_per_thread=KEEP_PER_THREAD,
        max_age_days=MAX_AGE_DAYS,
        vacuum=False,
        log=logger.info,
    )
    logger.info(
        "[checkpoint-cleanup] done: stale_threads=%d stale_rows=%d trimmed_rows=%d "
        "orphan_writes=%d rows_before=%d rows_after=%d size=%.1fMB",
        result["stale_threads"],
        result["stale_rows"],
        result["trimmed_rows"],
        result["orphan_writes"],
        result["rows_before"],
        result["rows_after"],
        size_before,
    )


def run_loop() -> None:
    """Entry point invoked by cron_supervisor."""
    logger.info(
        "[checkpoint-cleanup] cron started; interval=%ds keep_per_thread=%d max_age_days=%d",
        INTERVAL_SEC,
        KEEP_PER_THREAD,
        MAX_AGE_DAYS,
    )
    # Initial delay so we don't race start.sh's VACUUM-bearing prune at boot.
    time.sleep(INITIAL_DELAY_SEC)
    while True:
        try:
            _run_once()
        except Exception:
            logger.exception("[checkpoint-cleanup] failed; will retry next cycle")
        time.sleep(INTERVAL_SEC)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    _run_once()
