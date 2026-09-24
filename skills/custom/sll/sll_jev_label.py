#!/usr/bin/env python3
"""Jev pre-labels SLL turns land/fail so Brian only reviews the uncertain ones.

Validated 2026-09-23 against the 62 Brian-confirmed labels in golden set v2.

    python sll_jev_label.py validate --golden <v2.jsonl>
    python sll_jev_label.py queue --golden <v2.jsonl> --out <dir> [--auto 0.85]

`queue` labels every log.jsonl turn not already in the golden set, writes
  <out>/SLL - Jev Prelabels - YYYYMMDD.jsonl   (all, with p_fail + confidence)
  <out>/SLL - Golden Review Queue - YYYYMMDD.csv (only confidence < --auto)
"""
import argparse, csv, json, os, sys
from concurrent.futures import ThreadPoolExecutor
from datetime import date

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "_shared"))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import jev
import storage

CRITERIA = {
    "fail": "Brian corrected or rejected the output, re-asked for the same thing, expressed frustration or "
            "impatience (including a bare '?'), or had to supply information the agent should already have had",
    "land": "Brian accepted it and moved on, built on the result, approved a next step, or asked an unrelated new "
            "question. Moving to a new topic without comment counts as land; terseness alone is not failure",
    "unsure": "His next message genuinely does not reveal whether the turn landed",
}


def label(row):
    state = {"agent_task": (row.get("task") or "")[-1500:],
             "agent_output_summary": (row.get("response_summary") or "")[:1200],
             "brian_next_message": (row.get("user_reply") or "")[-1500:]}
    try:
        a = jev.choice(state, "Brian Mauck (blunt, rarely praises, moves on when satisfied) sent his next message "
                              "after the agent's output. Did the agent's turn land or fail?", CRITERIA, caller="sll-golden")
        return {"jev_label": a["choice"], "confidence": round(float(a.get("confidence", 0)), 3),
                "p_fail": round(float(a["probabilities"].get("fail", 0)), 3)}
    except Exception as e:
        return {"jev_label": "error", "confidence": 0.0, "p_fail": None, "err": str(e)[:120]}


def load(p):
    return [json.loads(l) for l in open(p, encoding="utf-8") if l.strip()]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("mode", choices=["validate", "queue"])
    ap.add_argument("--golden", required=True)
    ap.add_argument("--out", default=".")
    ap.add_argument("--auto", type=float, default=0.85)
    a = ap.parse_args()
    gold = load(a.golden)
    if a.mode == "validate":
        conf = [g for g in gold if g.get("label_source") == "brian_confirmed" and g["label"] in ("land", "fail")]
        with ThreadPoolExecutor(6) as ex:
            res = list(ex.map(label, conf))
        for thr in (0.0, 0.5, 0.7, 0.85):
            kept = [(g, r) for g, r in zip(conf, res) if r["confidence"] >= thr and r["jev_label"] in ("land", "fail")]
            ok = sum(1 for g, r in kept if g["label"] == r["jev_label"])
            print(f"conf>={thr}: covered {len(kept)}/{len(conf)}  agree {ok}/{len(kept)} = {ok / max(1, len(kept)):.3f}")
        fails = [(g, r) for g, r in zip(conf, res) if g["label"] == "fail"]
        print("fail recall:", sum(1 for g, r in fails if r["jev_label"] == "fail"), "/", len(fails))
        return
    done = {g["log_index"] for g in gold}
    rows = storage.read_jsonl(storage.LOG_PATH) if hasattr(storage, "LOG_PATH") else load(os.path.join(
        os.path.dirname(storage.__file__), "..", "..", "..", "backend", ".deer-flow", "sll", "log.jsonl"))
    todo = [(i, r) for i, r in enumerate(rows) if i not in done and r.get("user_reply")]
    with ThreadPoolExecutor(6) as ex:
        res = list(ex.map(lambda ir: label(ir[1]), todo))
    stamp = date.today().strftime("%Y%m%d")
    pj = os.path.join(a.out, f"SLL - Jev Prelabels - {stamp}.jsonl")
    pc = os.path.join(a.out, f"SLL - Golden Review Queue - {stamp}.csv")
    with open(pj, "w", encoding="utf-8") as f:
        for (i, r), l in zip(todo, res):
            f.write(json.dumps({"log_index": i, **l, "task": r.get("task", "")[-400:],
                                "user_reply": r.get("user_reply", "")[-400:]}) + "\n")
    q = [((i, r), l) for (i, r), l in zip(todo, res) if l["confidence"] < a.auto]
    with open(pc, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f); w.writerow(["log_index", "jev_label", "confidence", "your_label (land/fail/unsure)", "task (tail)", "brian_next_message"])
        for (i, r), l in sorted(q, key=lambda x: x[1]["confidence"]):
            w.writerow([i, l["jev_label"], l["confidence"], "", r.get("task", "")[-300:], r.get("user_reply", "")[-300:]])
    from collections import Counter
    print("labeled", len(todo), Counter(l["jev_label"] for l in res), "auto-accepted", len(todo) - len(q), "review", len(q))
    print(pj); print(pc)


if __name__ == "__main__":
    main()
