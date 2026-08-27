#!/usr/bin/env python3
"""Replay the live classifier over the golden set and score it.

This imports `classify_with_llm` from webhook_receiver rather than reimplementing
the prompt. An eval that scores a copy of the prompt drifts away from production
the first time someone edits the real one, and then reports green while the thing
it claims to measure regresses.

What it measures
----------------
  accuracy / precision / recall / F1 on `actionable`
  action_score        mean LLM-judge score (0-2) on proposed_action, over the
                      cases that have ground truth to judge against
  latency, cost proxy (tokens are not returned by the classifier, so this is
                      wall-clock only)

What it does NOT measure: whether the classifier surfaces emails it has never
seen. The negative half of the set comes from mail Brian never replied to, which
is the best available proxy but not a true labeled negative. Treat recall here as
directional.

Results append to .deer-flow/_eval_state.json so scores trend in Grafana.

Usage:
    python run_eval.py                      # full run, writes state
    python run_eval.py --limit 10           # quick smoke
    python run_eval.py --no-judge           # skip LLM judging (fast, free)
    python run_eval.py --no-write           # do not touch _eval_state.json
    python run_eval.py --label "opus5-newprompt"
"""

from __future__ import annotations

import argparse
import json
import logging
import statistics
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

_HERE = Path(__file__).resolve()
_SKILLS = _HERE.parents[1]
_BACKEND = _HERE.parents[3] / "backend"
_DEER = _BACKEND / ".deer-flow"

for _p in (_SKILLS / "_shared", _BACKEND, _HERE.parent):
    if _p.exists() and str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

try:
    from env_loader import load_env

    load_env()
except Exception:
    pass

logging.basicConfig(level=logging.INFO, format="[eval %(asctime)s] %(message)s", datefmt="%H:%M:%S")
log = logging.getLogger("run_eval")
# The classifier logs a line per call at INFO; that is 60 lines of noise on top
# of the scorecard we actually want to read.
logging.getLogger("webhook").setLevel(logging.WARNING)

GOLDEN_SET = _HERE.parent / "cases" / "golden_set.json"
STATE_PATH = _DEER / "_eval_state.json"
MAX_HISTORY = 100
# The classifier and judge are both network-bound; 6 keeps a 60-case run near a
# minute without tripping Anthropic rate limits.
CONCURRENCY = 6


def load_golden_set(path: Path) -> dict:
    if not path.exists():
        raise SystemExit(
            f"No golden set at {path}. Build one first:\n"
            f"    python build_golden_set.py --size 60"
        )
    return json.loads(path.read_text(encoding="utf-8"))


def _classify(case: dict) -> dict:
    """Run the production classifier on one case."""
    from webhook_receiver import classify_with_llm

    inputs = case.get("inputs", {})
    started = time.monotonic()
    result = classify_with_llm(
        source="gmail",
        sender=inputs.get("sender", ""),
        subject=inputs.get("subject", ""),
        body_preview=inputs.get("body", ""),
        to=inputs.get("to", ""),
        cc=inputs.get("cc", ""),
    )
    return {
        "case_id": case["case_id"],
        "expected_actionable": case["expected_actionable"],
        "predicted_actionable": bool(result.get("actionable")),
        "priority": result.get("priority", ""),
        "category": result.get("category", ""),
        "proposed_action": result.get("proposed_action", ""),
        "reasoning": result.get("reasoning", ""),
        "latency_ms": round((time.monotonic() - started) * 1000, 1),
        "outcome": case.get("outcome", ""),
        "sender_domain": case.get("sender_domain", ""),
        "source": case.get("source", ""),
    }


def _score_case(case: dict, use_judge: bool) -> dict:
    row = _classify(case)
    row["correct"] = row["predicted_actionable"] == row["expected_actionable"]
    # Only judge the action when the email genuinely deserved one. Grading the
    # action on a case that should not have been surfaced double-counts the same
    # mistake the actionable metric already caught.
    if use_judge and row["predicted_actionable"] and case["expected_actionable"]:
        from judge import judge_proposed_action

        verdict = judge_proposed_action(case, row)
        row["action_score"] = verdict["score"]
        row["action_reasoning"] = verdict["reasoning"]
    else:
        row["action_score"] = None
        row["action_reasoning"] = ""
    return row


def compute_metrics(rows: list[dict]) -> dict:
    n = len(rows)
    tp = sum(1 for r in rows if r["predicted_actionable"] and r["expected_actionable"])
    fp = sum(1 for r in rows if r["predicted_actionable"] and not r["expected_actionable"])
    fn = sum(1 for r in rows if not r["predicted_actionable"] and r["expected_actionable"])
    tn = sum(1 for r in rows if not r["predicted_actionable"] and not r["expected_actionable"])

    precision = tp / (tp + fp) if (tp + fp) else 0.0
    recall = tp / (tp + fn) if (tp + fn) else 0.0
    f1 = 2 * precision * recall / (precision + recall) if (precision + recall) else 0.0

    judged = [r["action_score"] for r in rows if isinstance(r.get("action_score"), int)]
    latencies = [r["latency_ms"] for r in rows if r.get("latency_ms")]

    return {
        "n": n,
        "accuracy": round((tp + tn) / n, 4) if n else 0.0,
        "precision": round(precision, 4),
        "recall": round(recall, 4),
        "f1": round(f1, 4),
        "true_positives": tp,
        "false_positives": fp,
        "false_negatives": fn,
        "true_negatives": tn,
        "action_score_mean": round(statistics.mean(judged), 3) if judged else None,
        "action_score_n": len(judged),
        "latency_p50_ms": round(statistics.median(latencies), 1) if latencies else 0.0,
    }


def _load_state() -> dict:
    if STATE_PATH.exists():
        try:
            return json.loads(STATE_PATH.read_text(encoding="utf-8"))
        except ValueError:
            log.warning("eval state was corrupt; starting a fresh history")
    return {"runs": []}


def write_state(metrics: dict, label: str, golden: dict) -> dict:
    """Append this run to the rolling history and stamp `latest` for Grafana."""
    state = _load_state()
    run = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "unix_ts": time.time(),
        "label": label,
        "golden_set_built_at": golden.get("built_at", ""),
        "golden_set_size": golden.get("size", 0),
        **metrics,
    }
    runs = state.get("runs", [])
    runs.append(run)
    state["runs"] = runs[-MAX_HISTORY:]
    state["latest"] = run
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(state, indent=2), encoding="utf-8")
    tmp.replace(STATE_PATH)
    return run


def _delta_line(metrics: dict) -> str:
    """Compare against the previous run — the number people actually act on."""
    runs = _load_state().get("runs", [])
    if not runs:
        return "  (no previous run to compare against)"
    prev = runs[-1]
    out = []
    for key in ("accuracy", "precision", "recall", "f1"):
        before, now = prev.get(key), metrics.get(key)
        if isinstance(before, (int, float)) and isinstance(now, (int, float)):
            d = now - before
            out.append(f"{key} {d:+.3f}")
    stamp = prev.get("label") or prev.get("ts", "")[:16]
    return f"  vs {stamp}: " + ", ".join(out) if out else "  (no comparable previous metrics)"


def print_report(metrics: dict, rows: list[dict], golden: dict, delta: str) -> None:
    m = metrics
    print()
    print("=" * 66)
    print(f"  CLASSIFIER EVAL — {m['n']} cases from a golden set built {golden.get('built_at', '?')[:10]}")
    print("=" * 66)
    print(f"  accuracy   {m['accuracy']:.3f}")
    print(f"  precision  {m['precision']:.3f}   (of what it surfaced, how much was right)")
    print(f"  recall     {m['recall']:.3f}   (directional — see module docstring)")
    print(f"  F1         {m['f1']:.3f}")
    print(f"  confusion  tp={m['true_positives']} fp={m['false_positives']} "
          f"fn={m['false_negatives']} tn={m['true_negatives']}")
    if m["action_score_mean"] is not None:
        print(f"  action     {m['action_score_mean']:.2f} / 2.00  (judged on {m['action_score_n']} cases)")
    else:
        print("  action     not judged")
    print(f"  latency    {m['latency_p50_ms']:.0f} ms p50")
    print(delta)

    misses = [r for r in rows if not r["correct"]]
    if misses:
        print("-" * 66)
        print(f"  {len(misses)} MISS(ES):")
        for r in misses[:15]:
            direction = "false positive" if r["predicted_actionable"] else "false negative"
            print(f"    [{direction}] {r['sender_domain']} ({r['outcome']})")
            print(f"        {r['reasoning'][:110]}")
        if len(misses) > 15:
            print(f"    ... and {len(misses) - 15} more (see --dump)")

    weak = [r for r in rows if isinstance(r.get("action_score"), int) and r["action_score"] < 2]
    if weak:
        print("-" * 66)
        print(f"  {len(weak)} WEAK ACTION(S):")
        for r in weak[:10]:
            print(f"    [{r['action_score']}/2] {r['sender_domain']}: {r['proposed_action'][:80]}")
            print(f"        judge: {r['action_reasoning'][:100]}")
    print("=" * 66)


def main() -> int:
    ap = argparse.ArgumentParser(description="Score the live classifier against the golden set")
    ap.add_argument("--set", default=str(GOLDEN_SET))
    ap.add_argument("--limit", type=int, default=0, help="Only run the first N cases (smoke test).")
    ap.add_argument("--no-judge", action="store_true", help="Skip LLM judging of proposed_action.")
    ap.add_argument("--no-write", action="store_true", help="Do not append to _eval_state.json.")
    ap.add_argument("--label", default="", help="Name this run (e.g. the change being tested).")
    ap.add_argument("--dump", default="", help="Write per-case results to this JSON path.")
    args = ap.parse_args()

    golden = load_golden_set(Path(args.set))
    cases = golden["cases"][: args.limit] if args.limit else golden["cases"]
    log.info("scoring %d cases (judge=%s)", len(cases), not args.no_judge)

    started = time.monotonic()
    with ThreadPoolExecutor(max_workers=CONCURRENCY) as pool:
        rows = list(pool.map(lambda c: _score_case(c, not args.no_judge), cases))
    elapsed = time.monotonic() - started

    metrics = compute_metrics(rows)
    delta = _delta_line(metrics)  # must be read BEFORE the new run is appended
    if not args.no_write:
        write_state(metrics, args.label, golden)
    print_report(metrics, rows, golden, delta)
    log.info("done in %.1fs", elapsed)

    if args.dump:
        Path(args.dump).write_text(json.dumps(rows, indent=2, ensure_ascii=False), encoding="utf-8")
        log.info("per-case results written to %s", args.dump)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
