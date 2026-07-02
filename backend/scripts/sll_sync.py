#!/usr/bin/env python3
"""Sync SLL lessons (JSONL) into the mem0 `lessons` namespace (P2-1 / §4.1).

Architecture after the fold-in:
- `.deer-flow/sll/lessons.jsonl` stays the WRITE path (sll_score.py runs as a
  subprocess; append-to-JSONL is atomic and dependency-free).
- The mem0 `lessons` namespace (shared Qdrant server) is the RETRIEVAL path —
  Mem0InjectionMiddleware vector-searches it per turn, replacing the old
  Haiku-ranking bash ritual the agent was mandated (and failed) to run.
- This script bridges the two: idempotent by sll_id, so it can run on every
  daily maintenance pass and after any capture burst.

Generic meta-lessons are NOT synced: ~25 of 68 lessons were the same
"verify-before-acting / confirm-with-user" lesson re-derived in different
task clothing. That rule is now a SOUL.md hard behavior (P2-3); syncing the
variants would crowd every lessons search with near-identical vectors.
A lesson matching the meta-pattern is still synced when it carries a
concrete domain anchor (dashboard/tape/tracker/...), because those encode
real, specific failure modes. Every skip is logged (no-silent-caps).
"""

import json
import logging
import re
import sys
from pathlib import Path

_BACKEND = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_BACKEND / "packages" / "harness"))
sys.path.insert(0, str(_BACKEND.parent / "skills" / "custom" / "_shared"))
try:
    from env_loader import load_env

    load_env()
except Exception:
    pass

logging.basicConfig(level=logging.INFO, format="[SLL-SYNC %(asctime)s] %(message)s",
                    datefmt="%H:%M:%S")
log = logging.getLogger("sll_sync")

LESSONS_PATH = _BACKEND / ".deer-flow" / "sll" / "lessons.jsonl"
LESSONS_USER_ID = "lessons"

# The promoted-to-SOUL meta-lesson shape: verify/confirm/check ... before ...
_META_PATTERN = re.compile(
    r"(?i)\b(verify|verifying|confirm|confirming|validat\w*|check\w*)\b[^.]{0,80}\bbefore\b"
    r"|\bbefore\b[^.]{0,80}\b(verify|verifying|confirm|confirming|validat\w*|check\w*)\b")
# Domain anchors that make a verify-shaped lesson concrete enough to keep.
_DOMAIN_ANCHORS = (
    "dashboard", "grafana", "redshift", "sql", "query", "tape", "borrowing",
    "bb", "sofom", "cico", "gmail", "email", "slack", "drive", "sheet",
    "xlsx", "tracker", "dossier", "eod", "briefing", "calendar", "webhook",
    "cron", "registry", "diligence", "vdr", "redline", "deck", "report",
    "zscaler", "mem0", "qdrant", "checkpoint", "drafts", "recipient",
)


def _is_generic_meta_lesson(text: str) -> bool:
    if not _META_PATTERN.search(text):
        return False
    lowered = text.lower()
    return not any(a in lowered for a in _DOMAIN_ANCHORS)


def _read_lessons() -> list[dict]:
    if not LESSONS_PATH.exists():
        return []
    out = []
    for line in LESSONS_PATH.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out


def _existing_sll_ids() -> set[str]:
    from deerflow.agents.memory.mem0_store import MEM0_COLLECTION, _qdrant_client, _user_filter

    client = _qdrant_client()
    ids: set[str] = set()
    offset = None
    while True:
        batch, offset = client.scroll(
            collection_name=MEM0_COLLECTION,
            scroll_filter=_user_filter(LESSONS_USER_ID),
            with_payload=True,
            with_vectors=False,
            limit=1000,
            offset=offset,
        )
        for p in batch:
            sll_id = (p.payload or {}).get("sll_id")
            if sll_id:
                ids.add(str(sll_id))
        if offset is None:
            break
    return ids


def sync() -> dict:
    from deerflow.agents.memory.mem0_store import add_memories

    lessons = _read_lessons()
    if not lessons:
        log.info("No lessons in %s — nothing to sync", LESSONS_PATH)
        return {"synced": 0, "skipped_meta": 0, "already": 0}

    existing = _existing_sll_ids()
    synced = skipped_meta = already = 0
    for lesson in lessons:
        sll_id = str(lesson.get("id") or "")
        text = (lesson.get("text") or "").strip()
        if not sll_id or not text:
            continue
        if sll_id in existing:
            already += 1
            continue
        if _is_generic_meta_lesson(text):
            skipped_meta += 1
            log.info("skip (generic meta-lesson, now a SOUL.md rule): %s", text[:110])
            continue
        add_memories(
            [{"role": "user", "content": text}],
            user_id=LESSONS_USER_ID,
            infer=False,  # keep lesson wording verbatim — do not atomize
            metadata={
                "sll_id": sll_id,
                "outcome": lesson.get("outcome"),
                "composite": lesson.get("composite"),
                "source_task": (lesson.get("source_task") or "")[:200],
                "sll_created_at": lesson.get("created_at"),
                "sll_retrieval_count": lesson.get("retrieval_count", 0),
            },
        )
        synced += 1

    summary = {"synced": synced, "skipped_meta": skipped_meta, "already": already,
               "total_jsonl": len(lessons)}
    log.info("SLL sync: %s", summary)
    return summary


if __name__ == "__main__":
    print(sync())
