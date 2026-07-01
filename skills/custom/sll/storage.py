"""Shared helpers for the SLL skill — paths, JSONL I/O, Anthropic.

Storage rationale: SLL scripts run as bash subprocesses fired by the agent.
The LangGraph worker already holds the on-disk Qdrant lock used by mem0,
so a subprocess cannot share that store. We therefore keep lessons in a
plain JSONL file and use a Haiku call for retrieval ranking. Lesson counts
stay small enough (steady state ~100-200) that fitting them all into one
prompt is fine.
"""

import json
import logging
import os
import secrets
import sys
from datetime import datetime, timezone
from pathlib import Path

_HERE = Path(__file__).resolve()
BACKEND_DIR = _HERE.parents[3] / "backend"
_SHARED = _HERE.parent.parent / "_shared"
if _SHARED.exists() and str(_SHARED) not in sys.path:
    sys.path.insert(0, str(_SHARED))
try:
    from env_loader import load_env
    load_env()
except Exception:
    pass

SLL_DIR = BACKEND_DIR / ".deer-flow" / "sll"
PENDING_PATH = SLL_DIR / "pending.json"
LOG_PATH = SLL_DIR / "log.jsonl"
LESSONS_PATH = SLL_DIR / "lessons.jsonl"
RETRIEVALS_PATH = SLL_DIR / "retrievals.jsonl"

ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
SENTIMENT_MODEL = "claude-haiku-4-5-20251001"
LESSON_MODEL = "claude-haiku-4-5-20251001"
RANKER_MODEL = "claude-haiku-4-5-20251001"

log = logging.getLogger("sll")


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def ensure_dir() -> None:
    SLL_DIR.mkdir(parents=True, exist_ok=True)


def read_pending() -> dict | None:
    if not PENDING_PATH.exists():
        return None
    try:
        return json.loads(PENDING_PATH.read_text(encoding="utf-8"))
    except Exception as e:
        log.warning("pending.json unreadable: %s", e)
        return None


def write_pending(entry: dict) -> None:
    ensure_dir()
    tmp = PENDING_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(entry, indent=2), encoding="utf-8")
    tmp.replace(PENDING_PATH)


def clear_pending() -> None:
    if PENDING_PATH.exists():
        try:
            PENDING_PATH.unlink()
        except Exception:
            pass


def append_jsonl(path: Path, entry: dict) -> None:
    ensure_dir()
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry) + "\n")


def read_jsonl(path: Path) -> list[dict]:
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
            except Exception:
                continue
    return out


def write_jsonl(path: Path, entries: list[dict]) -> None:
    """Atomic rewrite of a jsonl file (used by --prune)."""
    ensure_dir()
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        for e in entries:
            f.write(json.dumps(e) + "\n")
    tmp.replace(path)


def new_lesson_id() -> str:
    """Synthetic ID for a lesson — not backed by mem0."""
    return secrets.token_hex(8)


def all_lessons() -> list[dict]:
    return read_jsonl(LESSONS_PATH)


def add_lesson(entry: dict) -> str:
    """Append a lesson entry. Returns the assigned id."""
    if not entry.get("id"):
        entry["id"] = new_lesson_id()
    append_jsonl(LESSONS_PATH, entry)
    return entry["id"]


def delete_lessons_by_id(ids_to_drop: set[str]) -> int:
    """Atomic rewrite without the given ids. Returns count removed."""
    entries = all_lessons()
    keep = [e for e in entries if e.get("id") not in ids_to_drop]
    removed = len(entries) - len(keep)
    if removed:
        write_jsonl(LESSONS_PATH, keep)
    return removed


def update_lesson_retrieval(ids_returned: set[str]) -> None:
    """Bump retrieval_count and last_retrieved_at for each id."""
    if not ids_returned:
        return
    entries = all_lessons()
    changed = False
    now = now_iso()
    for e in entries:
        if e.get("id") in ids_returned:
            e["retrieval_count"] = int(e.get("retrieval_count") or 0) + 1
            e["last_retrieved_at"] = now
            changed = True
    if changed:
        write_jsonl(LESSONS_PATH, entries)


def anthropic_client():
    if not ANTHROPIC_API_KEY:
        return None
    try:
        from anthropic import Anthropic
        return Anthropic(api_key=ANTHROPIC_API_KEY)
    except Exception as e:
        log.warning("anthropic unavailable: %s", e)
        return None


def truncate(s: str, limit: int) -> str:
    s = (s or "").strip()
    if len(s) <= limit:
        return s
    return s[:limit].rstrip() + "..."
