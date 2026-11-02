#!/usr/bin/env python3
"""Remove specific proposal patterns from mem0 by exact text match.

Surgical counterpart to purge_proposal_patterns.py. Used to walk back an
over-broad restore without flattening the whole namespace again.

Context: restoring 93 audited patterns on 2026-08-26 fixed the reported
false-positive (a no-op acknowledgment of a Monex comprobante) but dropped eval
recall from 0.600 to 0.367 — far outside the measured +/-0.03 run-to-run noise
band. Every pattern in this namespace is a SUPPRESSION rule; there are no
"do surface this" patterns, so pattern count acts as a volume dial regardless of
how well-founded each individual rule is. The fix is to keep only the rules
targeting the failure class actually observed.

Reads a JSON array of pattern strings to REMOVE. Anything not matched exactly is
reported, never guessed at.

Usage:
    python remove_patterns_by_text.py <remove_list.json> --dry-run
    python remove_patterns_by_text.py <remove_list.json> --apply
"""

from __future__ import annotations

import argparse
import json
import sys
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
TOP_K = 10000


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("remove_list", help="JSON array of pattern strings to remove")
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--dry-run", action="store_true")
    g.add_argument("--apply", action="store_true")
    args = ap.parse_args()

    targets = {t.strip() for t in json.loads(Path(args.remove_list).read_text(encoding="utf-8")) if t.strip()}

    from deerflow.agents.memory.mem0_store import get_mem0

    m = get_mem0()
    live = m.vector_store.list(filters={"user_id": NAMESPACE}, top_k=TOP_K)[0]
    print(f"namespace holds {len(live)}; remove list has {len(targets)}")

    matched = []
    for item in live:
        text = (getattr(item, "payload", {}) or {}).get("data", "").strip()
        if text in targets:
            matched.append((getattr(item, "id", None), text))

    print(f"exact matches found: {len(matched)}")
    unmatched = len(targets) - len({t for _, t in matched})
    if unmatched:
        print(f"NOTE: {unmatched} remove-list entries had no live match (already absent).")

    if args.dry_run:
        for _, t in matched[:10]:
            print(f"  - {t[:110]}")
        print(f"\n[dry-run] would remove {len(matched)}, leaving {len(live) - len(matched)}.")
        return 0

    removed = 0
    for mem_id, _ in matched:
        if not mem_id:
            continue
        try:
            m.delete(mem_id)
            removed += 1
        except Exception as e:  # noqa: BLE001
            print(f"  failed to remove {mem_id}: {e}")

    remaining = m.vector_store.list(filters={"user_id": NAMESPACE}, top_k=TOP_K)[0]
    print(f"removed {removed}. Namespace now holds {len(remaining)}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
