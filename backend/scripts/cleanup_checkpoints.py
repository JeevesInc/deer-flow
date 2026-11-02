"""Prune the LangGraph checkpoints SQLite database.

Two-pass cleanup:
  1. Drop every row for threads whose latest checkpoint is older than --max-age-days.
  2. For surviving threads, keep only the latest --keep-per-thread checkpoints.
  3. VACUUM to reclaim disk space.

Run with LangGraph stopped — SQLite WAL conflicts otherwise.

Checkpoint IDs are UUIDv6 (timestamp-sortable). Age is decoded from the
60-bit timestamp embedded in the latest checkpoint_id per thread.
"""

from __future__ import annotations

import argparse
import datetime as dt
import sqlite3
import sys
import uuid
from pathlib import Path

GREGORIAN_EPOCH = dt.datetime(1582, 10, 15, tzinfo=dt.timezone.utc)


def uuid6_timestamp(checkpoint_id: str) -> dt.datetime | None:
    """Decode the timestamp from a UUIDv6 checkpoint_id. Returns None on failure."""
    try:
        u = uuid.UUID(checkpoint_id)
    except (ValueError, AttributeError):
        return None
    # UUIDv6 layout: time_high (32) | time_mid (16) | version(4)+time_low(12)
    time_high, time_mid, time_low_and_version = u.fields[0], u.fields[1], u.fields[2]
    if (time_low_and_version >> 12) != 6:
        return None
    ts_60 = (time_high << 28) | (time_mid << 12) | (time_low_and_version & 0x0FFF)
    # 60-bit count of 100ns intervals since 1582-10-15
    return GREGORIAN_EPOCH + dt.timedelta(microseconds=ts_60 // 10)


def db_size_mb(path: Path) -> float:
    return path.stat().st_size / (1024 * 1024)


def prune(
    db_path: Path,
    *,
    keep_per_thread: int = 5,
    max_age_days: int = 7,
    vacuum: bool = True,
    dry_run: bool = False,
    log=print,
) -> dict[str, int]:
    """Run the DELETE passes (and optionally VACUUM) against checkpoints.db.

    Args:
        db_path: SQLite file. Returns immediately if missing.
        keep_per_thread: Per-thread retention for Pass 2.
        max_age_days: Pass 1 stale-thread cutoff.
        vacuum: Skip when LangGraph is live (VACUUM holds an exclusive lock
            that conflicts with active writers and blocks them for the
            duration). DELETE passes are safe under WAL.
        dry_run: Report counts without committing.
        log: Callable used for status output (defaults to ``print``;
            callers running under a logger pass ``logger.info``).

    Returns:
        Counts of work done — useful for callers that want to log structured
        results: ``{"stale_threads", "stale_rows", "trimmed_rows",
        "orphan_writes", "rows_before", "rows_after"}``.
    """
    result = {
        "stale_threads": 0,
        "stale_rows": 0,
        "trimmed_rows": 0,
        "orphan_writes": 0,
        "rows_before": 0,
        "rows_after": 0,
    }
    if not db_path.exists():
        log(f"[cleanup] No DB at {db_path}; nothing to do.")
        return result

    cutoff = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=max_age_days)
    log(f"[cleanup] DB: {db_path} ({db_size_mb(db_path):.1f} MB)")
    log(f"[cleanup] Cutoff: {cutoff.isoformat()} (max-age-days={max_age_days})")
    log(f"[cleanup] Per-thread retention: {keep_per_thread}")
    if dry_run:
        log("[cleanup] DRY RUN — no deletions will be committed.")

    con = sqlite3.connect(str(db_path))
    cur = con.cursor()

    total_before = cur.execute("SELECT COUNT(*) FROM checkpoints").fetchone()[0]
    threads_before = cur.execute("SELECT COUNT(DISTINCT thread_id) FROM checkpoints").fetchone()[0]
    result["rows_before"] = total_before
    log(f"[cleanup] Before: {total_before:,} rows across {threads_before} threads")

    # Pass 1: identify stale threads by decoding MAX(checkpoint_id) timestamp.
    stale_threads: list[str] = []
    fresh_threads: list[str] = []
    undecodable: list[str] = []
    for thread_id, max_cid in cur.execute(
        "SELECT thread_id, MAX(checkpoint_id) FROM checkpoints GROUP BY thread_id"
    ):
        ts = uuid6_timestamp(max_cid)
        if ts is None:
            undecodable.append(thread_id)
            continue
        (stale_threads if ts < cutoff else fresh_threads).append(thread_id)

    if undecodable:
        log(f"[cleanup] WARN: {len(undecodable)} threads had non-UUIDv6 checkpoint_ids; treating as fresh.")
        fresh_threads.extend(undecodable)

    stale_row_count = 0
    if stale_threads:
        placeholders = ",".join("?" * len(stale_threads))
        stale_row_count = cur.execute(
            f"SELECT COUNT(*) FROM checkpoints WHERE thread_id IN ({placeholders})",
            stale_threads,
        ).fetchone()[0]
    result["stale_threads"] = len(stale_threads)
    result["stale_rows"] = stale_row_count
    log(f"[cleanup] Pass 1: {len(stale_threads)} stale threads ({stale_row_count:,} rows) past cutoff")

    if stale_threads and not dry_run:
        placeholders = ",".join("?" * len(stale_threads))
        cur.execute(
            f"DELETE FROM checkpoints WHERE thread_id IN ({placeholders})",
            stale_threads,
        )

    # Pass 2: trim surviving threads to latest N checkpoints.
    pass2_deletes = 0
    for thread_id in fresh_threads:
        rows = cur.execute(
            "SELECT checkpoint_ns, checkpoint_id FROM checkpoints WHERE thread_id=? ORDER BY checkpoint_id DESC",
            (thread_id,),
        ).fetchall()
        if len(rows) <= keep_per_thread:
            continue
        to_delete = rows[keep_per_thread:]
        pass2_deletes += len(to_delete)
        if not dry_run:
            cur.executemany(
                "DELETE FROM checkpoints WHERE thread_id=? AND checkpoint_ns=? AND checkpoint_id=?",
                [(thread_id, ns, cid) for ns, cid in to_delete],
            )

    result["trimmed_rows"] = pass2_deletes
    log(f"[cleanup] Pass 2: trimmed {pass2_deletes:,} excess rows from {len(fresh_threads)} surviving threads")

    # Pass 3: prune the `writes` table — per-node intermediate state that
    # accumulates one row per step. Without this the DB stays 100x bigger
    # than the checkpoints alone would warrant (99% of bloat lives here).
    # Tables present in LangGraph SQLite checkpointer >= 0.1.50.
    has_writes = cur.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='writes'"
    ).fetchone() is not None
    if has_writes:
        orphan_count = cur.execute(
            """
            SELECT COUNT(*) FROM writes
            WHERE (thread_id, checkpoint_ns, checkpoint_id) NOT IN
                  (SELECT thread_id, checkpoint_ns, checkpoint_id FROM checkpoints)
            """
        ).fetchone()[0]
        result["orphan_writes"] = orphan_count
        log(f"[cleanup] Pass 3: {orphan_count:,} orphan rows in 'writes' table")
        if not dry_run and orphan_count:
            cur.execute(
                """
                DELETE FROM writes
                WHERE (thread_id, checkpoint_ns, checkpoint_id) NOT IN
                      (SELECT thread_id, checkpoint_ns, checkpoint_id FROM checkpoints)
                """
            )

    if dry_run:
        con.rollback()
        con.close()
        return result

    con.commit()

    total_after = cur.execute("SELECT COUNT(*) FROM checkpoints").fetchone()[0]
    threads_after = cur.execute("SELECT COUNT(DISTINCT thread_id) FROM checkpoints").fetchone()[0]
    result["rows_after"] = total_after
    log(f"[cleanup] After:  {total_after:,} rows across {threads_after} threads")

    if vacuum:
        # VACUUM must run outside a transaction. Skipped by callers that
        # share the DB with a live LangGraph (its exclusive lock would
        # block all writers for the duration).
        con.isolation_level = None
        log("[cleanup] VACUUM ...")
        con.execute("VACUUM")
        log(f"[cleanup] Done. DB now {db_size_mb(db_path):.1f} MB")
    else:
        log("[cleanup] Skipping VACUUM (vacuum=False)")

    con.close()
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default=".deer-flow/checkpoints.db", help="Path to checkpoints.db")
    parser.add_argument("--keep-per-thread", type=int, default=5, help="Latest N checkpoints to keep per thread")
    parser.add_argument("--max-age-days", type=int, default=7, help="Drop threads with no checkpoint newer than this")
    parser.add_argument("--no-vacuum", action="store_true", help="Skip VACUUM (safe with live LangGraph)")
    parser.add_argument("--dry-run", action="store_true", help="Report what would be deleted without changing anything")
    args = parser.parse_args()

    prune(
        db_path=Path(args.db),
        keep_per_thread=args.keep_per_thread,
        max_age_days=args.max_age_days,
        vacuum=not args.no_vacuum,
        dry_run=args.dry_run,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
