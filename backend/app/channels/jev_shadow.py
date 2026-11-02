"""Jev shadow routing (2026-09-30, Brian).

For every inbound chat turn, asks TypeSafe's Jev which Anthropic model
(Opus 5.5 / Sonnet 5.5) and effort (low/medium/high) it WOULD use, and appends
the answer to .deer-flow/jev_shadow.jsonl. SHADOW ONLY: never changes the run.
Fire-and-forget; every failure is swallowed and logged. Sends TypeSafe only the
latest message (<=2000 chars) plus boolean signals, not thread history.
Kill switch: JEV_SHADOW=off.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

TYPESAFE_URL = "https://api.typesafe.ai/v1/systemone"
JEV_MODEL = "jev-latest"
MAX_CHARS = 2000
TIMEOUT_S = 5.0
# v2 (2026-10-07, Brian picked option 2): include prior thread turns so short
# follow-ups ("?", "fix it") are judged with the work behind them.
PAYLOAD_VERSION = "v2-ctx"
LANGGRAPH_URL = os.environ.get("LANGGRAPH_URL", "http://localhost:2024")
CTX_TURNS = 6
CTX_TURN_CHARS = 1500
CTX_TOTAL_CHARS = 6000
_SECRET_RES = [
    re.compile(r"\b(?:hf_|ghp_|gho_|github_pat_|sk-|xox[abpr]-)[A-Za-z0-9_\-]{10,}"),
    re.compile(r"(?<![\d$.,])\d{6,8}(?!\d|[.,]\d)"),  # 2FA / OTP codes
]


def _redact(text: str) -> str:
    for rx in _SECRET_RES:
        text = rx.sub("[REDACTED]", text or "")
    return text


def _msg_text(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return " ".join(b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text")
    return ""


def _thread_context(thread_id: str) -> list[dict]:
    """Last CTX_TURNS human/ai text turns from the LangGraph thread (oldest first)."""
    req = urllib.request.Request(f"{LANGGRAPH_URL}/threads/{thread_id}/state", method="GET")
    with urllib.request.urlopen(req, timeout=TIMEOUT_S) as resp:
        msgs = (json.loads(resp.read()).get("values") or {}).get("messages") or []
    out = []
    for m in msgs:
        role = {"human": "user", "ai": "assistant"}.get(m.get("type"))
        txt = _msg_text(m.get("content")).strip()
        if role and txt:
            out.append({"role": role, "text": _redact(txt)[:CTX_TURN_CHARS]})
    out = out[-CTX_TURNS:]
    while out and sum(len(x["text"]) for x in out) > CTX_TOTAL_CHARS:
        out.pop(0)
    return out
LOG_PATH = Path(__file__).resolve().parents[2] / ".deer-flow" / "jev_shadow.jsonl"

MODEL_CRITERIA = {
    "claude-opus-5-5": "Claude Opus 5.5: strongest and most expensive. Multi-step financial "
    "analysis, SQL against a data warehouse, borrowing base / covenant math, contract "
    "review, debugging code, anything lender-facing where an error is costly.",
    "claude-sonnet-5-5": "Claude Sonnet 5.5: cheaper and faster. Greetings, status checks, "
    "lookups, short factual answers, simple edits, quick opinions on a link.",
}
EFFORT_CRITERIA = {
    "low": "Trivial or conversational; one step.",
    "medium": "Moderate; a few steps or a short analysis.",
    "high": "Hard; many steps, numbers or correctness critical.",
}


def _post(body: bytes, key: str) -> dict:
    req = urllib.request.Request(
        TYPESAFE_URL, data=body, method="POST",
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=TIMEOUT_S) as resp:
        return json.loads(resp.read())


def _payload(text: str, has_image: bool, has_files: bool, new_thread: bool,
             context: list[dict] | None = None) -> bytes:
    return json.dumps({
        "model": JEV_MODEL,
        "state": {
            "messages": (context or []) + [{"role": "user", "text": _redact(text or "")[:MAX_CHARS]}],
            "signals": {"image_input": has_image, "files": has_files,
                        "tools": True, "new_thread": new_thread},
        },
        "questions": {
            "model": {"type": "choice", "criteria": MODEL_CRITERIA,
                      "instructions": "Choose the model for this whole task. Prefer the "
                      "cheaper model only when it clearly clears the quality bar."},
            "effort": {"type": "choice", "criteria": EFFORT_CRITERIA,
                       "instructions": "Choose the reasoning effort for this task."},
        },
    }).encode("utf-8")


async def record(*, thread_id: str, text: str, channel: str, has_image: bool = False,
                 has_files: bool = False, new_thread: bool = False,
                 actual_model: str = "claude-opus-5-5") -> None:
    if os.environ.get("JEV_SHADOW", "on").lower() == "off":
        return
    key = os.environ.get("TYPESAFE_API_KEY")
    entry = {"ts": datetime.now(timezone.utc).isoformat(), "thread_id": thread_id,
             "channel": channel, "new_thread": new_thread, "actual_model": actual_model,
             "text_preview": (text or "")[:160]}
    try:
        if not key:
            raise RuntimeError("TYPESAFE_API_KEY not set")
        context: list[dict] = []
        if not new_thread:
            try:
                context = await asyncio.to_thread(_thread_context, thread_id)
            except Exception as cexc:
                entry["ctx_error"] = f"{type(cexc).__name__}: {cexc}"[:200]
        entry.update({"payload_version": PAYLOAD_VERSION, "ctx_msgs": len(context),
                      "ctx_chars": sum(len(c["text"]) for c in context)})
        t0 = time.monotonic()
        ans = (await asyncio.to_thread(
            _post, _payload(text, has_image, has_files, new_thread, context), key)).get("answers", {})
        entry.update({
            "latency_s": round(time.monotonic() - t0, 3),
            "jev_model": ans.get("model", {}).get("choice"),
            "jev_model_conf": ans.get("model", {}).get("confidence"),
            "jev_effort": ans.get("effort", {}).get("choice"),
            "jev_effort_conf": ans.get("effort", {}).get("confidence"),
        })
    except Exception as exc:  # shadow must never affect the run
        entry["error"] = f"{type(exc).__name__}: {exc}"[:300]
        logger.warning("[JevShadow] %s", entry["error"])
    try:
        LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        with LOG_PATH.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry) + "\n")
    except Exception as exc:
        logger.warning("[JevShadow] log write failed: %s", exc)


def fire(**kwargs) -> None:
    """Schedule record() without awaiting it. Safe to call from any coroutine."""
    try:
        task = asyncio.get_running_loop().create_task(record(**kwargs))
        task.add_done_callback(lambda t: t.exception() if not t.cancelled() else None)
    except Exception as exc:
        logger.warning("[JevShadow] could not schedule: %s", exc)
