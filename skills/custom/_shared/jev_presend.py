"""Jev audit of every final Slack reply (2026-09-23) — SHADOW MODE.

Scores Brian's two most-repeated complaints about the agent's messages:
  narration  P(the message is mid-task progress narration / hedging, not a result)
  verbose    0-2 score (tight / some padding / padded, restates, too much detail)
  unasked    P(it asks Brian for permission/confirmation it did not need)

Writes one line per message to .deer-flow/jev_presend.jsonl. Does NOT block or
alter the message. Enforcement (hold + regenerate) is a separate decision once a
week of scores exists to set thresholds on. Never raises.
"""
import json
import threading
from datetime import datetime, timezone
from pathlib import Path

LOG = Path(r"C:\Jeeves\redshift-bot\deer-flow\backend\.deer-flow\jev_presend.jsonl")

QUESTIONS = {
    "narration": {"type": "noul",
                  "instructions": "Is this Slack message progress narration or hedging (announcing what the assistant "
                                  "will do or is doing: 'let me...', 'I'll now...', 'working on it') instead of a "
                                  "completed action or final answer?"},
    "verbose": {"type": "score",
                "instructions": "How padded is this message for a blunt executive who wants the answer first and "
                                "exceptions only?",
                "criteria": ["Tight: answer first, only what matters",
                             "Some padding: extra caveats, process detail or restated context",
                             "Padded: long timeline, re-derivation, lists of what else was checked, restating the question"]},
    "unasked": {"type": "noul",
                "instructions": "Does the message stop to ask the reader for permission or confirmation on a step "
                                "that was already requested, instead of just doing it?"},
}


def score(text: str) -> dict:
    import jev
    a = jev.ask({"slack_message": text[:12000]}, QUESTIONS, caller="slack-presend", timeout=10, retries=1)
    return {"p_narration": round(a["narration"]["noul"], 3), "verbose": round(a["verbose"]["score"], 2),
            "p_unasked": round(a["unasked"]["noul"], 3)}


def audit_async(text: str, thread_ts: str | None) -> None:
    def _run():
        try:
            rec = {"ts": datetime.now(timezone.utc).isoformat(timespec="seconds"), "thread_ts": thread_ts,
                   "chars": len(text or ""), **score(text or "")}
            with open(LOG, "a", encoding="utf-8") as f:
                f.write(json.dumps(rec) + "\n")
        except Exception:
            pass
    if text and text.strip():
        threading.Thread(target=_run, daemon=True).start()
