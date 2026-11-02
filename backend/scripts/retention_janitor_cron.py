#!/usr/bin/env python3
"""Retention janitor cron — stops .deer-flow growing monotonically (P2-7).

.deer-flow reached 5.4GB with no retention anywhere: checkpoints.db held its
476MB high-water forever (SQLite never returns free pages without VACUUM),
threads/ workspace scratch (1.9GB) outlives its threads, knowledge
intel_*.md accumulate daily, dispatch_audit.jsonl grows unbounded and is
re-parsed on every metrics scrape (GW-F16).

Daily (03:30 local, catch-up on downtime):
  - prune .deer-flow/knowledge/intel_*.md older than INTEL_RETENTION_DAYS (60)
  - delete files older than SCRATCH_RETENTION_DAYS (30) under
    threads/*/user-data/workspace (workspace scratch only — outputs and
    uploads are deliverables and are left alone)
  - rotate dispatch_audit.jsonl above AUDIT_ROTATE_MB (10) to .1 (one kept)

Monthly (idle window, stamped per-month):
  - VACUUM checkpoints.db. Skipped gracefully when LangGraph holds the
    write lock; retried next night until it succeeds that month.

State: .deer-flow/_retention_janitor_state.json (via _shared/cron_state).
Deletions are logged with counts/bytes — never silent (no-silent-caps rule).
"""

import logging
import os
import sqlite3
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

_BACKEND = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_BACKEND.parent / "skills" / "custom" / "_shared"))
from cron_state import atomic_write_json, load_json_state  # noqa: E402

logging.basicConfig(level=logging.INFO, format="[JANITOR %(asctime)s] %(message)s",
                    datefmt="%H:%M:%S")
log = logging.getLogger("retention_janitor")

DEER = _BACKEND / ".deer-flow"
STATE_PATH = DEER / "_retention_janitor_state.json"

RUN_HOUR = int(os.environ.get("JANITOR_HOUR", "3"))
CHECK_INTERVAL_SECS = 3600
INTEL_RETENTION_DAYS = int(os.environ.get("INTEL_RETENTION_DAYS", "60"))
SCRATCH_RETENTION_DAYS = int(os.environ.get("SCRATCH_RETENTION_DAYS", "30"))
AUDIT_ROTATE_MB = float(os.environ.get("AUDIT_ROTATE_MB", "10"))


def _heartbeat() -> None:
    try:
        from app.gateway.cron_supervisor import record_heartbeat
        record_heartbeat("retention-janitor")
    except Exception:
        pass


def prune_intel(now: datetime) -> tuple[int, int]:
    """Delete knowledge/intel_*.md older than the retention window."""
    knowledge = DEER / "knowledge"
    if not knowledge.is_dir():
        return 0, 0
    cutoff = now - timedelta(days=INTEL_RETENTION_DAYS)
    count = freed = 0
    for f in sorted(knowledge.glob("intel_*.md")):
        try:
            stamp = datetime.strptime(f.stem, "intel_%Y-%m-%d")
        except ValueError:
            stamp = datetime.fromtimestamp(f.stat().st_mtime)
        if stamp < cutoff:
            size = f.stat().st_size
            f.unlink()
            count += 1
            freed += size
    if count:
        log.info("intel prune: deleted %d files (%.1f KB) older than %dd",
                 count, freed / 1024, INTEL_RETENTION_DAYS)
    return count, freed


def prune_workspace_scratch(now: datetime) -> tuple[int, int]:
    """Delete stale files under threads/*/user-data/workspace only."""
    threads = DEER / "threads"
    if not threads.is_dir():
        return 0, 0
    cutoff_ts = (now - timedelta(days=SCRATCH_RETENTION_DAYS)).timestamp()
    count = freed = 0
    for workspace in threads.glob("*/user-data/workspace"):
        for f in workspace.rglob("*"):
            try:
                if f.is_file() and f.stat().st_mtime < cutoff_ts:
                    size = f.stat().st_size
                    f.unlink()
                    count += 1
                    freed += size
            except OSError:
                continue
        # sweep now-empty subdirectories (deepest first), keep workspace itself
        for d in sorted((p for p in workspace.rglob("*") if p.is_dir()),
                        key=lambda p: len(p.parts), reverse=True):
            try:
                d.rmdir()  # only succeeds when empty
            except OSError:
                pass
    if count:
        log.info("workspace scratch: deleted %d files (%.1f MB) older than %dd",
                 count, freed / 1024 / 1024, SCRATCH_RETENTION_DAYS)
    return count, freed


def rotate_dispatch_audit() -> bool:
    """Rotate dispatch_audit.jsonl when it exceeds the cap (GW-F16)."""
    audit = DEER / "dispatch_audit.jsonl"
    try:
        if not audit.exists() or audit.stat().st_size < AUDIT_ROTATE_MB * 1024 * 1024:
            return False
        rotated = audit.with_suffix(".jsonl.1")
        if rotated.exists():
            rotated.unlink()
        os.replace(audit, rotated)
        log.info("rotated dispatch_audit.jsonl (%.1f MB) -> .1", rotated.stat().st_size / 1024 / 1024)
        return True
    except OSError as e:
        log.warning("dispatch_audit rotation failed: %s", e)
        return False


def vacuum_checkpoints() -> bool:
    """VACUUM checkpoints.db; False (retry later) when LangGraph holds the lock."""
    db = DEER / "checkpoints.db"
    if not db.exists():
        return True
    before = db.stat().st_size
    try:
        conn = sqlite3.connect(str(db), timeout=30)
        try:
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            conn.execute("VACUUM")
        finally:
            conn.close()
    except sqlite3.OperationalError as e:
        log.warning("VACUUM skipped (%s) — will retry next night", e)
        return False
    after = db.stat().st_size
    log.info("VACUUM checkpoints.db: %.1f MB -> %.1f MB",
             before / 1024 / 1024, after / 1024 / 1024)
    return True


def sync_sll_lessons() -> bool:
    """Push newly captured SLL lessons into the mem0 `lessons` namespace.

    Idempotent (keyed by sll_id) and best-effort — lessons wait in the JSONL
    until the next pass if Qdrant is down (P2-1 / §4.1).
    """
    import subprocess
    try:
        r = subprocess.run(
            [sys.executable, str(_BACKEND / "scripts" / "sll_sync.py")],
            capture_output=True, text=True, timeout=600,
        )
        if r.returncode != 0:
            log.warning("sll_sync failed (rc=%d): %s", r.returncode, (r.stderr or "")[-300:])
        return r.returncode == 0
    except Exception as e:
        log.warning("sll_sync failed: %s", e)
        return False


def prune_traces() -> int:
    """Drop run-trace day-files past the retention window.

    The trace sink writes one file per day under .deer-flow/traces/ and does not
    clean up after itself — every other unbounded writer in .deer-flow got a
    janitor entry for exactly this reason (P2-7), so this one gets one on day
    zero rather than after it fills the disk.
    """
    try:
        from deerflow.agents.tracing import prune
        removed = prune()
        if removed:
            log.info("traces: removed %d day-file(s) past retention", removed)
        return removed
    except Exception as e:
        log.warning("trace prune failed: %s", e)
        return 0


def run_once(now: datetime | None = None) -> dict:
    now = now or datetime.now()
    state = load_json_state(STATE_PATH, {})
    summary = {}

    intel_n, intel_b = prune_intel(now)
    scratch_n, scratch_b = prune_workspace_scratch(now)
    rotated = rotate_dispatch_audit()
    summary.update(intel_deleted=intel_n, intel_bytes=intel_b,
                   scratch_deleted=scratch_n, scratch_bytes=scratch_b,
                   audit_rotated=rotated,
                   traces_pruned=prune_traces(),
                   sll_synced=sync_sll_lessons())

    month = now.strftime("%Y-%m")
    if state.get("last_vacuum_month") != month:
        if vacuum_checkpoints():
            state["last_vacuum_month"] = month
            summary["vacuumed"] = True

    state["last_run"] = now.isoformat()
    atomic_write_json(STATE_PATH, state)
    return summary


def _ran_today(state: dict, now: datetime) -> bool:
    last = state.get("last_run")
    if not last:
        return False
    try:
        return datetime.fromisoformat(last).date() == now.date()
    except ValueError:
        return False


def run_loop() -> None:
    log.info("Retention janitor started. Daily from %02d:00 (intel>%dd, scratch>%dd, "
             "audit>%.0fMB, monthly VACUUM).",
             RUN_HOUR, INTEL_RETENTION_DAYS, SCRATCH_RETENTION_DAYS, AUDIT_ROTATE_MB)
    while True:
        _heartbeat()
        now = datetime.now()
        # >= hour + not-ran-today: downtime across the hour delays, never skips (GW-F10 pattern)
        if now.hour >= RUN_HOUR and not _ran_today(load_json_state(STATE_PATH, {}), now):
            try:
                run_once(now)
            except Exception as e:
                log.error("janitor run failed: %s", e)
        time.sleep(CHECK_INTERVAL_SECS)


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Retention janitor")
    parser.add_argument("mode", nargs="?", choices=["once"], help="run one pass now")
    args = parser.parse_args()
    if args.mode == "once":
        print(run_once())
    else:
        run_loop()
