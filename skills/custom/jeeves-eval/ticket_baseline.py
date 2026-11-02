#!/usr/bin/env python3
"""Super-Agent Phase 0 baseline — measure before Tickets go live.

Two numbers, both from our own history, weekly for the last N weeks:

1. **Nudges** — Brian's messages to the bot that chase undelivered work
   ("did you…", "hello?", "you there", "where is…", "any update", "status?",
   "still waiting", "I've done nothing"). Source: Slack DM history with the bot,
   top-level messages and thread replies. Proposal §5's most important metric.
2. **Replies on in-flight work sent to the classifier** — inbound proposals
   whose subject is a reply ("Re:") to a thread that already produced a
   proposal in the prior 30 days. Proxy for "belonged to open work".
   Source: .deer-flow/proposal_log.jsonl.

Usage: python ticket_baseline.py [--weeks 8] [--out <json>]
Writes .deer-flow/ticket_baseline.json and prints a table.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import time
from collections import Counter
from datetime import UTC, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]  # deer-flow/
STATE = Path(os.environ.get("DEERFLOW_STATE_DIR") or ROOT / "backend" / ".deer-flow")
OWNER = os.environ.get("SLACK_OWNER_USER_ID", "U05B5HGNCN9")

NUDGE_RE = re.compile(
    r"^\s*(hello\??|hi\?|you there\??|\?+)\s*$"
    r"|\bdid you (do|send|finish|build|fix|run|get)\b"
    r"|\bwhere (is|are) (it|the|that|my)\b"
    r"|\bany updates?\b|\bstatus\?|\bstill waiting\b|\bdone nothing\b|\bwhat happened to\b|\bnothing yet\b",
    re.I,
)
_SUBJ_RE = re.compile(r"^\s*((re|fw|fwd)\s*:\s*)+", re.I)


def week_of(dt: datetime) -> str:
    return (dt - timedelta(days=dt.weekday())).strftime("%Y-%m-%d")


def is_nudge(text: str) -> bool:
    return bool(text) and bool(NUDGE_RE.search(text))


def slack_owner_messages(since: datetime) -> list[tuple[datetime, str]]:
    from slack_sdk import WebClient

    c = WebClient(token=os.environ["SLACK_BOT_TOKEN"])
    ch = c.conversations_open(users=[OWNER])["channel"]["id"]
    out: list[tuple[datetime, str]] = []
    cursor = None
    parents = []
    while True:
        r = c.conversations_history(channel=ch, oldest=str(since.timestamp()), limit=200, cursor=cursor)
        for m in r["messages"]:
            if m.get("user") == OWNER:
                out.append((datetime.fromtimestamp(float(m["ts"]), UTC), m.get("text", "")))
            if m.get("reply_count"):
                parents.append(m["ts"])
        cursor = (r.get("response_metadata") or {}).get("next_cursor")
        if not cursor:
            break
    for ts in parents:
        cur = None
        while True:
            try:
                r = c.conversations_replies(channel=ch, ts=ts, limit=200, cursor=cur)
            except Exception as e:  # rate limit: back off once
                if "ratelimited" in str(e):
                    time.sleep(3)
                    continue
                raise
            for m in r["messages"][1:] if not cur else r["messages"]:
                if m.get("user") == OWNER and m.get("ts") != ts:
                    out.append((datetime.fromtimestamp(float(m["ts"]), UTC), m.get("text", "")))
            cur = (r.get("response_metadata") or {}).get("next_cursor")
            if not cur:
                break
    return out


def classifier_replies(since: datetime, log_path: Path = STATE / "proposal_log.jsonl") -> tuple[Counter, Counter]:
    total, inflight = Counter(), Counter()
    seen: dict[str, datetime] = {}
    rows = []
    for line in log_path.read_text(encoding="utf-8").splitlines():
        try:
            rows.append(json.loads(line))
        except Exception:
            continue
    rows.sort(key=lambda r: r.get("posted_at", ""))
    for r in rows:
        try:
            ts = datetime.fromisoformat(r["posted_at"])
        except Exception:
            continue
        subj = r.get("subject") or ""
        key = _SUBJ_RE.sub("", subj).strip().lower()
        prior = seen.get(key)
        if ts >= since:
            wk = week_of(ts)
            total[wk] += 1
            if _SUBJ_RE.match(subj) and prior and ts - prior <= timedelta(days=30):
                inflight[wk] += 1
        if key:
            seen[key] = ts
    return total, inflight


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--weeks", type=int, default=8)
    ap.add_argument("--out", default=str(STATE / "ticket_baseline.json"))
    a = ap.parse_args()
    now = datetime.now(UTC)
    since = now - timedelta(weeks=a.weeks)
    since = since - timedelta(days=since.weekday(), hours=since.hour, minutes=since.minute)

    msgs = slack_owner_messages(since)
    sent, nudges = Counter(), Counter()
    examples: dict[str, list[str]] = {}
    for ts, text in msgs:
        wk = week_of(ts)
        sent[wk] += 1
        if is_nudge(text):
            nudges[wk] += 1
            examples.setdefault(wk, []).append(text[:80])
    total, inflight = classifier_replies(since)

    weeks = sorted(set(sent) | set(total))
    table = [{"week": w, "brian_msgs": sent[w], "nudges": nudges[w], "proposals": total[w], "replies_on_inflight_to_classifier": inflight[w]} for w in weeks]
    res = {
        "generated_at": now.isoformat(timespec="seconds"),
        "since": since.isoformat(timespec="seconds"),
        "weeks": table,
        "totals": {
            "brian_msgs": sum(sent.values()),
            "nudges": sum(nudges.values()),
            "proposals": sum(total.values()),
            "replies_on_inflight_to_classifier": sum(inflight.values()),
        },
        "nudge_examples": examples,
    }
    Path(a.out).write_text(json.dumps(res, indent=2), encoding="utf-8")
    print(f"{'week':<12}{'msgs':>6}{'nudges':>8}{'props':>7}{'inflight':>10}")
    for r in table:
        print(f"{r['week']:<12}{r['brian_msgs']:>6}{r['nudges']:>8}{r['proposals']:>7}{r['replies_on_inflight_to_classifier']:>10}")
    print("TOTAL", json.dumps(res["totals"]))


if __name__ == "__main__":
    main()
