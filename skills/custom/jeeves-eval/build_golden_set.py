#!/usr/bin/env python3
"""Build the golden eval set from real labeled history.

Sources and what each contributes
---------------------------------
POSITIVES (expected actionable=True)
    proposal_log.jsonl x proposal_outcomes.jsonl, outcome in
    {approved, redirected, handled_elsewhere}. Brian either told the agent to
    go, gave it different instructions, or answered the email himself in Gmail.
    All three mean surfacing it was correct.

    `handled_elsewhere` counts ONLY when Brian himself replied. If a colleague
    replied instead (`thread_progressed`), the case is dropped — that is the
    normal shape of internal mail he is CC'd on, and it says nothing about
    whether he needed to act.

NEGATIVES (expected actionable=False)
    (a) Same join, outcome in {rejected, dismissed}. The classifier surfaced it
        and Brian declined or archived it — a measured false positive.
    (b) action_observations.jsonl `no_reply` rows that were never proposed and
        whose Gmail thread shows no owner reply. Emails that came in and
        correctly warranted nothing.

EXCLUDED
    outcome in {ignored, unknown}. "No reply anywhere and still sitting in the
    inbox" is not evidence either way — it may mean noise, or a busy week. A
    golden set is only worth as much as its weakest label, so these stay out.

Why the labels are trustworthy only as of 2026-08-26: before then every
proposal without a Slack reply was labeled `ignored` unconditionally, which
buried 433 genuine positives. Run
`python proposal_learner.py --relabel-ignored` before rebuilding this set if you
suspect the outcomes log has drifted back.

Bodies are fetched from Gmail so the eval replays the classifier on exactly the
input it saw in production, not on a summary of it.

Usage:
    python build_golden_set.py                 # default 60 cases
    python build_golden_set.py --size 100
    python build_golden_set.py --out cases/golden_set.json
"""

from __future__ import annotations

import argparse
import json
import logging
import random
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

_HERE = Path(__file__).resolve()
_SKILLS = _HERE.parents[1]
_BACKEND = _HERE.parents[3] / "backend"
_DEER = _BACKEND / ".deer-flow"

for _p in (_SKILLS / "_shared", _SKILLS / "gmail"):
    if _p.exists() and str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

try:
    from env_loader import load_env

    load_env()
except Exception:
    pass

logging.basicConfig(level=logging.INFO, format="[golden %(asctime)s] %(message)s", datefmt="%H:%M:%S")
log = logging.getLogger("build_golden_set")

SCHEMA_VERSION = 1

POSITIVE_OUTCOMES = {"approved", "redirected", "handled_elsewhere"}
NEGATIVE_OUTCOMES = {"rejected", "dismissed"}
# Deliberately not a label source — see module docstring.
EXCLUDED_OUTCOMES = {"ignored", "unknown"}

# `handled_elsewhere` is only a positive when BRIAN acted. `thread_progressed`
# means someone else replied in the thread, which happens constantly on internal
# mail where he is merely CC'd — exactly the traffic the classifier is supposed
# to suppress. The first eval run scored those as false negatives while the
# classifier's own reasoning ("scheduling confirmation, Brian merely CC'd") was
# correct, i.e. the label was wrong, not the model. Same principle that keeps
# `ignored` out: only label what the evidence actually supports.
POSITIVE_DISPOSITIONS = {"owner_replied", "owner_replied_pre"}
AMBIGUOUS_DISPOSITIONS = {"thread_progressed"}

# Cap how much of the set any single sender domain may occupy. Without this the
# set is ~40% one counterparty and the score mostly measures how well the
# classifier handles that one relationship.
MAX_SHARE_PER_DOMAIN = 0.25

DEFAULT_SIZE = 60
BODY_CHARS = 4000


def _read_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    out = []
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except ValueError:
                continue
    return out


# ---------------------------------------------------------------------------
# Gmail body fetch
# ---------------------------------------------------------------------------


def _gmail_service():
    from googleapiclient.discovery import build

    from google_auth import get_credentials

    return build("gmail", "v1", credentials=get_credentials(), cache_discovery=False)


def _decode_part(part: dict) -> str:
    import base64

    data = (part.get("body") or {}).get("data")
    if not data:
        return ""
    try:
        return base64.urlsafe_b64decode(data.encode()).decode("utf-8", errors="replace")
    except Exception:
        return ""


def _extract_body(payload: dict) -> str:
    """Depth-first walk preferring text/plain, falling back to stripped HTML."""
    if not payload:
        return ""
    mime = payload.get("mimeType", "")
    if mime == "text/plain":
        return _decode_part(payload)
    if mime == "text/html":
        import re

        html = _decode_part(payload)
        text = re.sub(r"<(script|style)[^>]*>.*?</\1>", "", html, flags=re.DOTALL | re.I)
        text = re.sub(r"<br\s*/?>|</p>", "\n", text, flags=re.I)
        text = re.sub(r"<[^>]+>", "", text)
        return re.sub(r"\n{3,}", "\n\n", text).strip()
    best = ""
    for part in payload.get("parts", []) or []:
        got = _extract_body(part)
        if got and (not best or part.get("mimeType") == "text/plain"):
            best = got
            if part.get("mimeType") == "text/plain":
                break
    return best


def _header(headers: list[dict], name: str) -> str:
    lname = name.lower()
    for h in headers or []:
        if (h.get("name") or "").lower() == lname:
            return h.get("value") or ""
    return ""


def fetch_message(service, msg_id: str) -> dict | None:
    """Return the classifier's real inputs for one message, or None."""
    try:
        msg = service.users().messages().get(userId="me", id=msg_id, format="full").execute()
    except Exception as e:
        log.debug("fetch failed for %s: %s", msg_id, e)
        return None
    payload = msg.get("payload") or {}
    headers = payload.get("headers", [])
    body = _extract_body(payload)
    if not body.strip():
        return None
    return {
        "sender": _header(headers, "From"),
        "to": _header(headers, "To"),
        "cc": _header(headers, "Cc"),
        "subject": _header(headers, "Subject"),
        "body": body[:BODY_CHARS],
    }


# ---------------------------------------------------------------------------
# Candidate assembly
# ---------------------------------------------------------------------------


def _domain(addr: str) -> str:
    import re

    m = re.search(r"<([^>]+)>", addr or "")
    email = (m.group(1) if m else (addr or "")).strip().lower()
    return email.rsplit("@", 1)[-1] if "@" in email else ""


def candidates_from_proposals() -> list[dict]:
    proposals = {p["slack_ts"]: p for p in _read_jsonl(_DEER / "proposal_log.jsonl") if p.get("slack_ts")}
    outcomes = _read_jsonl(_DEER / "proposal_outcomes.jsonl")

    cands = []
    for o in outcomes:
        outcome = o.get("outcome")
        if outcome in EXCLUDED_OUTCOMES:
            continue
        prop = proposals.get(o.get("proposal_slack_ts"))
        if not prop or not prop.get("gmail_msg_id"):
            continue
        disposition = o.get("gmail_disposition", "")
        if outcome == "handled_elsewhere" and disposition in AMBIGUOUS_DISPOSITIONS:
            continue  # a colleague replied — says nothing about whether Brian had to
        if outcome in POSITIVE_OUTCOMES:
            expected = True
        elif outcome in NEGATIVE_OUTCOMES:
            expected = False
        else:
            continue
        cands.append(
            {
                "case_id": f"prop-{prop['gmail_msg_id']}",
                "source": "proposal",
                "gmail_msg_id": prop["gmail_msg_id"],
                "expected_actionable": expected,
                "outcome": outcome,
                "gmail_disposition": o.get("gmail_disposition", ""),
                "sender_domain": prop.get("sender_domain", ""),
                "posted_at": prop.get("posted_at", ""),
                # What the classifier said at the time, and what Brian did about
                # it. Both are inputs to the proposed-action judge, never to the
                # actionable label.
                "historical_proposed_action": prop.get("proposed_action", ""),
                "owner_reply": o.get("brian_reply", ""),
                "label_reasoning": o.get("label_reasoning", ""),
            }
        )
    return cands


def candidates_from_observations(proposed_ids: set[str]) -> list[dict]:
    """`no_reply` emails that were never proposed — the true-negative side.

    Without these the set only measures precision: every case would be an email
    the classifier already chose to surface, so it could score perfectly by
    calling everything actionable.
    """
    obs = _read_jsonl(_DEER / "action_observations.jsonl")
    cands = []
    for o in obs:
        if o.get("kind") != "no_reply":
            continue
        thread_id = o.get("thread_id")
        if not thread_id or thread_id in proposed_ids:
            continue
        body = (o.get("body") or "").strip()
        # Very short bodies are usually image-only newsletters or bare
        # forwards; there is nothing for a classifier to reason about.
        if len(body) < 120:
            continue
        cands.append(
            {
                "case_id": f"obs-{thread_id}",
                "source": "observation",
                "gmail_msg_id": thread_id,
                "expected_actionable": False,
                "outcome": "no_reply_never_proposed",
                "gmail_disposition": "",
                "sender_domain": o.get("from_domain", ""),
                "posted_at": o.get("received_at", ""),
                "historical_proposed_action": "",
                "owner_reply": "",
                "label_reasoning": "Received, never replied to, never surfaced as a proposal.",
                # Observations already carry the full message — no Gmail fetch.
                "inputs": {
                    "sender": o.get("from", ""),
                    "to": o.get("to", ""),
                    "cc": o.get("cc", ""),
                    "subject": o.get("subject", ""),
                    "body": body[:BODY_CHARS],
                },
            }
        )
    return cands


def stratify(cands: list[dict], size: int, seed: int) -> list[dict]:
    """Sample to `size`, balancing positives/negatives and capping any one
    sender domain."""
    rng = random.Random(seed)
    pos = [c for c in cands if c["expected_actionable"]]
    neg = [c for c in cands if not c["expected_actionable"]]
    rng.shuffle(pos)
    rng.shuffle(neg)

    want_each = size // 2
    per_domain_cap = max(1, int(size * MAX_SHARE_PER_DOMAIN))

    def _take(pool: list[dict], want: int) -> list[dict]:
        counts: dict[str, int] = defaultdict(int)
        picked, overflow = [], []
        for c in pool:
            d = c.get("sender_domain") or "?"
            if counts[d] < per_domain_cap:
                picked.append(c)
                counts[d] += 1
            else:
                overflow.append(c)
            if len(picked) >= want:
                return picked
        # Only breach the cap if we would otherwise come up short. Say so out
        # loud — a set that is silently 45% one domain reads as balanced when
        # it is not, and that shows up later as an unexplained score swing.
        short = want - len(picked)
        if short > 0 and overflow:
            taken = overflow[:short]
            log.warning(
                "domain cap (%d/domain) breached to fill %d slot(s): %s",
                per_domain_cap, len(taken),
                dict(defaultdict(int, {c.get("sender_domain") or "?": 1 for c in taken})),
            )
            picked.extend(taken)
        return picked

    chosen = _take(pos, want_each) + _take(neg, size - want_each)
    rng.shuffle(chosen)
    return chosen


def build(size: int, out_path: Path, seed: int) -> dict:
    proposals = _read_jsonl(_DEER / "proposal_log.jsonl")
    proposed_ids = {p.get("gmail_msg_id") for p in proposals if p.get("gmail_msg_id")}

    cands = candidates_from_proposals() + candidates_from_observations(proposed_ids)
    log.info(
        "candidates: %d (%d positive, %d negative)",
        len(cands),
        sum(1 for c in cands if c["expected_actionable"]),
        sum(1 for c in cands if not c["expected_actionable"]),
    )

    # Oversample before fetching, because some Gmail fetches will come back
    # empty (deleted mail, attachment-only bodies) and drop out.
    picked = stratify(cands, int(size * 1.6), seed)

    service = None
    need_fetch = [c for c in picked if "inputs" not in c]
    if need_fetch:
        try:
            service = _gmail_service()
        except Exception as e:
            log.error("Gmail unavailable (%s) — only observation-sourced cases will be built.", e)

    built: list[dict] = []
    for c in picked:
        if "inputs" not in c:
            if service is None:
                continue
            inputs = fetch_message(service, c["gmail_msg_id"])
            if not inputs:
                continue
            c["inputs"] = inputs
            c["sender_domain"] = c["sender_domain"] or _domain(inputs["sender"])
        built.append(c)

    # Re-stratify post-fetch so the dropouts do not skew the final balance.
    final = stratify(built, size, seed)

    payload = {
        "schema_version": SCHEMA_VERSION,
        "built_at": datetime.now(timezone.utc).isoformat(),
        "seed": seed,
        "size": len(final),
        "positives": sum(1 for c in final if c["expected_actionable"]),
        "negatives": sum(1 for c in final if not c["expected_actionable"]),
        "label_policy": {
            "positive_outcomes": sorted(POSITIVE_OUTCOMES),
            "negative_outcomes": sorted(NEGATIVE_OUTCOMES),
            "excluded_outcomes": sorted(EXCLUDED_OUTCOMES),
            "ambiguous_dispositions_excluded": sorted(AMBIGUOUS_DISPOSITIONS),
            "note": "ignored/unknown are excluded — absence of a reply is not a label. "
                    "handled_elsewhere counts as positive only when Brian himself replied.",
        },
        "cases": final,
    }
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    log.info("wrote %s — %d cases (%d pos / %d neg)",
             out_path, payload["size"], payload["positives"], payload["negatives"])
    return payload


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Build the classifier golden eval set")
    ap.add_argument("--size", type=int, default=DEFAULT_SIZE)
    ap.add_argument("--seed", type=int, default=20260826,
                    help="Fixed so rebuilds are reproducible; change it to draw a fresh sample.")
    ap.add_argument("--out", default=str(_HERE.parent / "cases" / "golden_set.json"))
    args = ap.parse_args()
    build(args.size, Path(args.out), args.seed)
