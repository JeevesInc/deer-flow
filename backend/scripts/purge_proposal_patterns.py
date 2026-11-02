#!/usr/bin/env python3
"""Purge and rebuild the mem0 `proposal-patterns` namespace.

Why
---
Until 2026-08-26 `proposal_learner.label_outcome()` labeled every proposal with
no Slack reply as `ignored` — 445 of 470 rows were wrong. The pattern
synthesizer is explicitly pointed at IGNORED cases to learn "where the
classifier got it wrong", so ~433 CORRECT proposals were mined as failures and
written back as suppression rules. Several surviving patterns reason literally
from "goes unanswered" and name the CFO.

The production effect is a cliff, not a drift:

    2026-07-13..21   24, 24, 22, 26, 23, 18, 14, 32, 34 proposals/day
    2026-07-22 on     3, 1, 1, 1, 2, 1, 2, 4 ... 1-2/day

against flat inbound volume (128 → 135 observed/month). Twelve patterns had
accumulated by July 21.

The labeling bug is fixed. This clears the patterns it produced so they can be
re-derived from the corrected labels.

Safety
------
* Backs up every pattern to .deer-flow/proposal_patterns_backup_<stamp>.json
  BEFORE deleting. Restore with --restore <file>.
* --dry-run shows exactly what would go.
* Only touches user_id="proposal-patterns". No other namespace is read or
  written.

Usage
-----
    python purge_proposal_patterns.py --dry-run
    python purge_proposal_patterns.py --purge
    python purge_proposal_patterns.py --restore <backup.json>

After --purge, rebuild from the corrected labels and measure:

    cd ../../skills/custom/gmail  && python proposal_learner.py --backfill
    cd ../jeeves-eval            && python run_eval.py --label "after-mem0-purge"
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

_HERE = Path(__file__).resolve()
_BACKEND = _HERE.parents[1]
_DEER = _BACKEND / ".deer-flow"

for _p in (_BACKEND / "packages" / "harness", _BACKEND.parent / "skills" / "custom" / "_shared"):
    if _p.exists() and str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

try:
    from env_loader import load_env

    load_env()
except Exception:
    pass

NAMESPACE = "proposal-patterns"


def _mem0():
    from deerflow.agents.memory.mem0_store import get_mem0

    return get_mem0()


# mem0's get_all() defaults to top_k=20 and delete_all() deletes whatever
# vector_store.list() returns, which itself defaults to top_k=100. Reading the
# namespace with the defaults reports "20" for a namespace holding 247, so a
# purge looks like a no-op while it is actually chewing through 100 per call.
# Every read here is explicitly unbounded.
FETCH_TOP_K = 10000


def _fetch(m) -> list[dict]:
    r = m.get_all(filters={"user_id": NAMESPACE}, top_k=FETCH_TOP_K)
    return r.get("results", r) if isinstance(r, dict) else r


def _backup(items: list[dict]) -> Path:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
    path = _DEER / f"proposal_patterns_backup_{stamp}.json"
    path.write_text(json.dumps(items, indent=2, default=str), encoding="utf-8")
    return path


def show(items: list[dict]) -> None:
    print(f"\n{len(items)} pattern(s) in namespace '{NAMESPACE}':\n")
    for i, it in enumerate(items, 1):
        text = (it.get("memory") or "").replace("\n", " ")
        print(f"  {i:2}. [{(it.get('created_at') or '')[:10]}] {text[:150]}")
    print()


def purge(dry_run: bool) -> int:
    m = _mem0()
    items = _fetch(m)
    show(items)
    if not items:
        print("Nothing to purge.")
        return 0
    if dry_run:
        print(f"[dry-run] would back up {len(items)} pattern(s), then delete the namespace.")
        return 0

    path = _backup(items)
    print(f"Backed up {len(items)} pattern(s) -> {path.name}")

    # delete_all() only removes one vector_store.list() page (100) per call, so
    # a single call silently leaves the rest behind. Loop until the namespace
    # actually reports empty, with a bound so a stuck backend cannot spin here.
    remaining = items
    for attempt in range(1, 51):
        m.delete_all(user_id=NAMESPACE)
        remaining = _fetch(m)
        print(f"  pass {attempt}: {len(remaining)} remaining")
        if not remaining:
            break
    else:
        print("WARNING: namespace still not empty after 50 passes — inspect before rebuilding.")
        return 1

    print(f"Purged. Remaining in namespace: {len(remaining)}")
    print(
        "\nNext:\n"
        "  cd ../../skills/custom/gmail && python proposal_learner.py --backfill\n"
        "  cd ../jeeves-eval           && python run_eval.py --label 'after-mem0-purge'"
    )
    return 0


def restore(path: Path) -> int:
    """Re-add backed-up patterns verbatim.

    infer=False matches how proposal_learner writes them — the curated sentence
    is what the classifier retrieves, so letting mem0's extractor reword it on
    the way back in would not be a restore.
    """
    items = json.loads(path.read_text(encoding="utf-8"))
    m = _mem0()
    n = 0
    for it in items:
        text = (it.get("memory") or "").strip()
        if not text:
            continue
        m.add(text, user_id=NAMESPACE, infer=False)
        n += 1
    print(f"Restored {n} pattern(s) from {path.name}")
    return 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--dry-run", action="store_true", help="List what would be purged; change nothing.")
    g.add_argument("--purge", action="store_true", help="Back up, then delete the namespace.")
    g.add_argument("--restore", metavar="BACKUP_JSON", help="Re-add patterns from a backup file.")
    args = ap.parse_args()

    if args.restore:
        raise SystemExit(restore(Path(args.restore)))
    raise SystemExit(purge(dry_run=args.dry_run))
