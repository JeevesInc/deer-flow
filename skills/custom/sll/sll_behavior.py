#!/usr/bin/env python3
"""SLL behavioral signals — score what Brian DID, not what he said.

Rationale (2026-09-22): 429 of 873 logged turns classified `no_signal` and were
written to log.jsonl carrying the default 0.6 composite, i.e. half the corpus was
fabricated neutrality. Brian almost never praises and rarely narrates
dissatisfaction; he just moves on, or he re-asks. Those are the real labels.

Signals produced (all derived from data already on disk — no new plumbing):

  reask_similarity   Jaccard overlap between Brian's next message and the task we
                     just did. High overlap = he is asking for the same thing
                     again = we failed, regardless of tone.
  topic_shift        Low overlap with the task AND low overlap with the response.
                     He moved on. After a delivery turn that is a weak accept.
  rework_marker      Lexical retry cues ("no,", "i said", "instead", "again",
                     "still", "that's not"). Not strictly behavioral — kept
                     separable via use_markers=False.
  gap_minutes        Wall-clock delay before his reply. Recorded, NOT scored:
                     confounded by meetings. Metadata for later calibration.
  task_depth         How many consecutive prior turns shared this task cluster.
                     Depth >= 3 without a topic shift = we are grinding.
  artifact_claimed   Response mentions a file/link/draft/send. Enables a deferred
                     delivery check (did the draft actually get sent?).

Label = one of: rework | moved_on | deepening | unknown
Only rework / moved_on / deepening count as LABELED turns. `unknown` means we
have no evidence and the turn must be excluded from quality metrics rather than
recorded as mediocre.

Always exits 0. Import-safe: sll_score.py degrades to verbal-only if this
module raises.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime
from typing import Any

try:
    import storage
except Exception:  # only when run outside the skill dir
    storage = None  # type: ignore

STOPWORDS = {
    "the", "a", "an", "and", "or", "but", "if", "then", "so", "to", "of", "in",
    "on", "for", "with", "at", "by", "from", "up", "out", "is", "are", "was",
    "were", "be", "been", "being", "do", "does", "did", "have", "has", "had",
    "i", "you", "we", "it", "that", "this", "these", "those", "what", "how",
    "can", "could", "would", "should", "will", "just", "me", "my", "your",
    "our", "us", "there", "here", "not", "no", "yes", "ok", "okay", "please",
    "need", "want", "get", "got", "let", "make", "also", "any", "all", "one",
}

REWORK_MARKERS = [
    r"^no[,.\s]", r"\bi said\b", r"\binstead\b", r"\bagain\b", r"\bstill\b",
    r"\bthat'?s not\b", r"\bthis isn'?t\b", r"\bnot what\b", r"\bredo\b",
    r"\bfix (it|this|that)\b", r"\btry again\b", r"\byou (didn'?t|missed|forgot)\b",
    r"\bwrong\b", r"\bactually\b", r"\brerun\b", r"\bre-?run\b",
]

ARTIFACT_PATTERNS = [
    r"\.xlsx", r"\.docx", r"\.pptx", r"\.csv", r"\.pdf",
    r"drive\.google\.com", r"\buploaded\b", r"\bdraft(ed)?\b", r"\battached\b",
    r"\bsent\b", r"\bworkbook\b", r"\bslide\b", r"\bcertificate\b",
]

# Deltas applied to the composite. Deliberately smaller than the verbal
# overrides — behaviour is evidence, not testimony.
# Calibrated 2026-09-22 against golden set v1 (136 LLM-judged turns, seed 7).
# Purity of each label against the golden label, vs base rates of 63% land /
# 37% fail:
#     moved_on  -> landed   86%  (n=35)   STRONG, well above base
#     deepening -> landed   61%  (n=74)   at base rate = no information
#     rework    -> failed   54%  (n=24)   weak; above 37% base but barely
# So the deltas are deliberately asymmetric. A -0.30 on rework would drag a
# 0.6 turn to 0.30, under FAILURE_THRESHOLD, minting a failure lesson at 2.5x
# boost on a signal that is wrong ~half the time. -0.14 lands at 0.46, just
# above the threshold: the signal is recorded and trendable but cannot by
# itself manufacture a lesson. Same logic caps moved_on below SUCCESS_THRESHOLD.
BEHAVIOR_DELTAS = {
    # Re-fit 2026-09-22 against golden set v2 (138 labels, 50 confirmed by
    # Brian). Every config in the grid — 60 combinations of threshold, marker
    # and depth settings — landed at precision 0.51-0.54 for the lexical
    # `rework` signal. It does not work, so it no longer moves the score; the
    # label is still written to the log so a better method can be measured
    # against it. `no_response` is the one exact behavioural signal we have:
    # a bare "?" means the agent never answered.
    "no_response": -0.35,
    "rework": 0.0,
    "moved_on": 0.10,
    "deepening": 0.0,   # ambiguous: a follow-up on the same topic is just as
    "unknown": 0.0,     # likely a redirect as it is a build-on. No evidence.
}

# Only these carry enough evidence to count as a LABEL. Everything else is
# missing data and must be excluded from quality metrics, not scored 0.6.
LABELED = {"no_response", "rework", "moved_on"}

QUESTION_STARTERS = (
    "what", "which", "who", "whom", "whose", "where", "when", "why", "how",
    "is", "are", "was", "were", "do", "does", "did", "can", "could", "would",
    "should", "will", "any", "got",
)

REASK_THRESHOLD = 0.34      # overlap at/above this = he is re-asking
SHIFT_THRESHOLD = 0.10      # overlap at/below this = he moved on
DEPTH_GRIND = 3


def tokens(text: str) -> set[str]:
    words = re.findall(r"[a-z0-9_]+", (text or "").lower())
    return {w for w in words if w not in STOPWORDS and len(w) > 2}


def jaccard(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / float(len(a | b))


def has_rework_marker(text: str) -> bool:
    t = (text or "").lower().strip()
    return any(re.search(p, t) for p in REWORK_MARKERS)


PROBE_RE = re.compile(
    r"^[\s\?\.!]*"
    r"(\?+|hello|hey|hi|you there|u there|anything|still there|and|well|so)"
    r"[\s\?\.!]*$",
    re.I,
)


def is_probe(text: str) -> bool:
    """A bare "?" is not curiosity — it means the agent never answered.

    Brian, 2026-09-22: "anytime there's a '?' from me it's because there was no
    response from the agent. which happens a lot and is annoying." The
    is_question() rule added earlier the same day was classing "?" as a benign
    follow-up and scoring it `deepening` (unlabeled, zero delta) — precisely
    inverted. A probe is the single least ambiguous failure signal we have.
    """
    t = (text or "").strip()
    return bool(t) and len(t) <= 20 and bool(PROBE_RE.match(t))


def is_question(text: str) -> bool:
    """A follow-up QUESTION on the same topic is curiosity, not a re-ask.

    Without this, "what was the liquidity number in that borrowing base"
    scores as rework purely on vocabulary overlap and stores a false failure
    lesson at 2.5x boost. Asking about the work is not rejecting the work.
    """
    t = (text or "").strip().lower()
    if not t or is_probe(t):
        return False
    if t.endswith("?"):
        return True
    first = re.findall(r"[a-z']+", t)
    return bool(first) and first[0] in QUESTION_STARTERS


def has_artifact_claim(text: str) -> bool:
    t = (text or "").lower()
    return any(re.search(p, t) for p in ARTIFACT_PATTERNS)


ARTIFACT_REF = re.compile(
    r"(?:[A-Za-z]:\\[^\s\"'<>|]+"                      # C:\path\file
    r"|/mnt/[^\s\"'<>|]+"                                # /mnt/... path
    r"|https?://(?:drive|docs)\.google\.com/[^\s\)\"']+"  # Drive/Docs link
    r"|[\w ,.\-()]+\.(?:xlsx|xlsm|csv|docx|pptx|pdf|json|sql|py))"
)


_PROSE_LEADERS = {
    "workbook", "saved", "as", "the", "file", "in", "to", "at", "and", "a",
    "uploaded", "attached", "see", "is", "into", "called", "named", "wrote",
    "output", "deliverable", "generated", "built", "created", "from",
}


def extract_artifacts(text: str, limit: int = 25) -> list[str]:
    """Pull concrete artifact references out of a response.

    The rubric item ARTIFACT_DELIVERED was unjudgeable while the log held only a
    300-char summary — 21 of 48 sampled turns came back "na". Capturing the
    actual paths/links makes "was it produced or just described" checkable.
    """
    seen, out = set(), []
    for m in ARTIFACT_REF.finditer(text or ""):
        ref = m.group(0).strip().rstrip(".,;)")
        # The filename branch allows spaces (our convention is
        # "Category - Descriptor - YYYYMMDD.xlsx"), so it can swallow the prose
        # in front of it: "Workbook saved as Anexo J - ... .xlsx".
        if not ref.lower().startswith(("http", "/mnt")) and ":" not in ref[:3]:
            parts = ref.split()
            while len(parts) > 1 and parts[0].lower() in _PROSE_LEADERS:
                parts.pop(0)
            ref = " ".join(parts)
        if ref.lower() in seen:
            continue
        seen.add(ref.lower())
        out.append(ref)
        if len(out) >= limit:
            break
    return out


def parse_iso(ts: str | None) -> datetime | None:
    if not ts:
        return None
    try:
        return datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    except Exception:
        return None


def gap_minutes(scored_at: str | None, applied_at: str | None) -> float | None:
    a, b = parse_iso(scored_at), parse_iso(applied_at)
    if not a or not b:
        return None
    try:
        return round((b - a).total_seconds() / 60.0, 2)
    except Exception:
        return None


def task_depth(task: str, history: list[dict], lookback: int = 6) -> int:
    """Consecutive prior turns whose task overlaps this one."""
    t = tokens(task)
    depth = 0
    for entry in reversed(history[-lookback:]):
        if jaccard(t, tokens(entry.get("task", ""))) >= REASK_THRESHOLD:
            depth += 1
        else:
            break
    return depth


def analyze(
    task: str,
    response: str,
    user_reply: str,
    history: list[dict] | None = None,
    scored_at: str | None = None,
    applied_at: str | None = None,
    use_markers: bool = True,
) -> dict[str, Any]:
    """Return behavioral signals + label + delta for one turn."""
    history = history or []
    t_task = tokens(task)
    t_resp = tokens(response)
    t_reply = tokens(user_reply)

    reask = jaccard(t_reply, t_task)
    resp_overlap = jaccard(t_reply, t_resp)
    marker = has_rework_marker(user_reply) if use_markers else False
    question = is_question(user_reply)
    depth = task_depth(task, history)

    if is_probe(user_reply):
        # No answer was given. Nothing else about the turn matters.
        label = "no_response"
    elif not t_reply:
        label = "unknown"
    elif marker:
        label = "rework"
    elif reask >= REASK_THRESHOLD and not question:
        label = "rework"
    elif reask <= SHIFT_THRESHOLD and resp_overlap <= SHIFT_THRESHOLD:
        label = "moved_on"
    else:
        label = "deepening"

    # Grinding on the same task for several turns is failure evidence even if
    # each individual reply reads calm.
    if label == "deepening" and depth >= DEPTH_GRIND and not question:
        label = "rework"

    return {
        "behavior_label": label,
        "behavior_labeled": label in LABELED,
        "behavior_delta": BEHAVIOR_DELTAS.get(label, 0.0),
        "reask_similarity": round(reask, 3),
        "response_overlap": round(resp_overlap, 3),
        "rework_marker": marker,
        "is_question": question,
        "is_probe": is_probe(user_reply),
        "task_depth": depth,
        "gap_minutes": gap_minutes(scored_at, applied_at),
        "artifact_claimed": has_artifact_claim(response),
    }


def backfill(limit: int | None = None) -> int:
    """Recompute behavioral labels over log.jsonl and print the comparison."""
    if storage is None:
        print("storage unavailable", file=sys.stderr)
        return 0
    rows = storage.read_jsonl(storage.LOG_PATH)
    if limit:
        rows = rows[-limit:]
    counts: dict[str, int] = {}
    cross: dict[tuple, int] = {}
    rescued = 0
    dark = 0
    gaps = []
    for i, r in enumerate(rows):
        sig = analyze(
            r.get("task", ""),
            r.get("response_summary", ""),
            r.get("user_reply", ""),
            history=rows[:i],
            scored_at=r.get("scored_at"),
            applied_at=r.get("applied_at"),
        )
        bl = sig["behavior_label"]
        vl = r.get("sentiment", "no_signal")
        counts[bl] = counts.get(bl, 0) + 1
        cross[(vl, bl)] = cross.get((vl, bl), 0) + 1
        if vl == "no_signal" and sig["behavior_labeled"]:
            rescued += 1
        if vl == "no_signal" and not sig["behavior_labeled"]:
            dark += 1
        if sig["gap_minutes"] is not None:
            gaps.append(sig["gap_minutes"])

    total = len(rows) or 1
    print(f"turns analyzed: {len(rows)}")
    print("\nbehavioral label distribution:")
    for k, v in sorted(counts.items(), key=lambda x: -x[1]):
        print(f"  {k:<10} {v:>4}  ({100.0*v/total:.0f}%)")
    print(f"\nno_signal turns rescued by behaviour: {rescued}")
    print(f"turns with NO signal of any kind (excluded from metrics): {dark}")
    if gaps:
        gaps.sort()
        print(f"\nreply gap minutes: n={len(gaps)} median={gaps[len(gaps)//2]:.1f}")
    print("\nverbal x behavioral crosstab (top 15):")
    for (vl, bl), n in sorted(cross.items(), key=lambda x: -x[1])[:15]:
        print(f"  {vl:<20} -> {bl:<10} {n:>4}")
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="SLL behavioral signals")
    p.add_argument("--backfill", action="store_true")
    p.add_argument("--limit", type=int, default=None)
    p.add_argument("--task", default="")
    p.add_argument("--response", default="")
    p.add_argument("--user-reply", default="")
    args = p.parse_args(argv)
    try:
        if args.backfill:
            return backfill(args.limit)
        print(json.dumps(analyze(args.task, args.response, args.user_reply), indent=2))
        return 0
    except Exception as e:
        print(f"[sll-behavior] error: {e}", file=sys.stderr)
        return 0


if __name__ == "__main__":
    sys.exit(main())
