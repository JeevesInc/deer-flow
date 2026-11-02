#!/usr/bin/env python3
"""SLL dashboard and maintenance.

    sll_dashboard.py --full              # summary of recent activity, top lessons
    sll_dashboard.py --prune             # drop lessons not retrieved in N days
    sll_dashboard.py --recent 20         # show last N scored turns
"""

import argparse
import logging
import sys
from collections import Counter
from datetime import datetime, timedelta, timezone

import storage

logging.basicConfig(
    level=logging.WARNING,
    format="[sll-dash %(levelname)s] %(message)s",
    stream=sys.stderr,
)
log = logging.getLogger("sll_dashboard")

PRUNE_AGE_DAYS = 30
PRUNE_MIN_RETRIEVALS = 1  # lessons retrieved at least this many times within window are kept


def parse_iso(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except Exception:
        return None


def show_full() -> int:
    log_entries = storage.read_jsonl(storage.LOG_PATH)
    lessons = storage.read_jsonl(storage.LESSONS_PATH)
    retrievals = storage.read_jsonl(storage.RETRIEVALS_PATH)

    print("=" * 70)
    print("SLL DASHBOARD")
    print("=" * 70)
    print(f"Storage:      {storage.SLL_DIR}")
    print(f"Turns scored: {len(log_entries)}")
    print(f"Lessons:      {len(lessons)}")
    print(f"Retrievals:   {len(retrievals)}")
    print()

    if log_entries:
        outcomes = Counter(e.get("outcome", "?") for e in log_entries)
        sentiments = Counter(e.get("sentiment", "?") for e in log_entries)
        composites = [float(e.get("final_composite", 0.0)) for e in log_entries]
        avg = sum(composites) / len(composites) if composites else 0.0
        print(f"Avg composite: {avg:.3f}")
        print(f"Outcome distribution: {dict(outcomes)}")
        print(f"Sentiment distribution: {dict(sentiments)}")
        print()

    if lessons:
        print("--- Top 10 lessons by retrieval count ---")
        ranked = sorted(
            lessons,
            key=lambda e: (int(e.get("retrieval_count") or 0), e.get("created_at") or ""),
            reverse=True,
        )
        for e in ranked[:10]:
            rc = int(e.get("retrieval_count") or 0)
            text = storage.truncate(e.get("text", ""), 100)
            print(f"  [{rc:>3} hits] {text}")
        print()

        print("--- Lessons by outcome ---")
        by_outcome = Counter(e.get("outcome", "?") for e in lessons)
        for k, v in by_outcome.items():
            print(f"  {k}: {v}")
        print()

    if log_entries:
        print("--- Last 10 turns ---")
        for e in log_entries[-10:]:
            ts = e.get("applied_at", "")[:19]
            outcome = e.get("outcome", "?")
            sentiment = e.get("sentiment", "?")
            comp = e.get("final_composite", 0.0)
            task = storage.truncate(e.get("task", ""), 60)
            print(f"  {ts}  comp={comp:.2f}  {outcome:>8}  {sentiment:>20}  {task}")
        print()

    return 0


def show_recent(n: int) -> int:
    log_entries = storage.read_jsonl(storage.LOG_PATH)
    for e in log_entries[-n:]:
        ts = e.get("applied_at", "")[:19]
        outcome = e.get("outcome", "?")
        sentiment = e.get("sentiment", "?")
        comp = e.get("final_composite", 0.0)
        task = storage.truncate(e.get("task", ""), 80)
        lesson = e.get("lesson_stored") or ""
        print(f"{ts}  comp={comp:.2f}  {outcome:>8}  {sentiment:>20}  {task}")
        if lesson:
            print(f"    -> {storage.truncate(lesson, 120)}")
    return 0


def prune(age_days: int, dry_run: bool) -> int:
    lessons = storage.read_jsonl(storage.LESSONS_PATH)
    if not lessons:
        print("No lessons to prune.")
        return 0

    cutoff = datetime.now(timezone.utc) - timedelta(days=age_days)
    keep: list[dict] = []
    drop: list[dict] = []
    for e in lessons:
        created = parse_iso(e.get("created_at"))
        if created and created > cutoff:
            keep.append(e)
            continue
        if int(e.get("retrieval_count") or 0) >= PRUNE_MIN_RETRIEVALS:
            last = parse_iso(e.get("last_retrieved_at"))
            if last and last > cutoff:
                keep.append(e)
                continue
        drop.append(e)

    print(f"Pruning {len(drop)} of {len(lessons)} lessons (age>{age_days}d, retrievals<{PRUNE_MIN_RETRIEVALS} in window)")
    for e in drop[:20]:
        print(f"  - {storage.truncate(e.get('text', ''), 100)}")
    if len(drop) > 20:
        print(f"  ... and {len(drop) - 20} more")

    if dry_run:
        print("[dry-run] no changes written")
        return 0

    if not drop:
        return 0

    storage.write_jsonl(storage.LESSONS_PATH, keep)
    print(f"Kept {len(keep)} lessons.")
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="SLL dashboard")
    p.add_argument("--full", action="store_true", help="Show full dashboard summary")
    p.add_argument("--recent", type=int, default=0, help="Show last N scored turns")
    p.add_argument("--prune", action="store_true", help="Drop stale lessons")
    p.add_argument("--age-days", type=int, default=PRUNE_AGE_DAYS, help="Age threshold for prune")
    p.add_argument("--dry-run", action="store_true", help="Preview prune without deleting")
    args = p.parse_args(argv)

    try:
        if args.prune:
            return prune(args.age_days, args.dry_run)
        if args.recent:
            return show_recent(args.recent)
        # Default and --full both show the full dashboard.
        return show_full()
    except Exception as e:
        log.error("sll_dashboard error: %s", e, exc_info=True)
        return 1


if __name__ == "__main__":
    sys.exit(main())
