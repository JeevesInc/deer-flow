"""Idle LangGraph thread reaper.

The checkpoint cleanup cron (``checkpoint_cleanup_cron.py``) only prunes
rows out of ``checkpoints.db``. LangGraph's own thread/ops registry lives
in ``backend/.langgraph_api/.langgraph_ops.pckl`` and is *not* touched —
which is why ``deerflow_langgraph_threads{status="idle"}`` keeps climbing
even after the checkpoint cleanup has run.

This cron closes the loop: every few hours it lists idle threads via
``POST /threads/search``, drops anything older than ``MAX_AGE_DAYS`` whose
``thread_id`` is **not** referenced by ``channels/store.json``, and then
also nukes the per-thread local directory via the gateway. The Slack /
Telegram store.json check is the safety net — see Brian's standing rule
that Slack thread context must be preserved across restarts (a thread the
channel store still points at could be reopened tomorrow when somebody
replies to an old thread).

``DELETE /threads/{id}`` on the LangGraph dev server removes both the ops
entry and its checkpoints, so this also obsoletes most of what the older
checkpoint cron does — but we keep that one running because it also
sweeps orphan ``writes`` rows that pre-date this cron.
"""

from __future__ import annotations

import json
import logging
import os
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx

logger = logging.getLogger(__name__)

INTERVAL_SEC = 6 * 60 * 60  # 6 hours
INITIAL_DELAY_SEC = 10 * 60  # let gateway + langgraph settle
MAX_AGE_DAYS = 5
PAGE_SIZE = 200

_BACKEND_DIR = Path(__file__).resolve().parent.parent
_STATE_DIR = _BACKEND_DIR / ".deer-flow"
_STORE_PATHS = (
    _STATE_DIR / "channels" / "store.json",
    _STATE_DIR / "store.json",  # legacy layout
)

LANGGRAPH_URL = os.environ.get("LANGGRAPH_URL", "http://localhost:2024").rstrip("/")
GATEWAY_URL = os.environ.get("GATEWAY_URL", "http://localhost:8001").rstrip("/")


def _protected_thread_ids() -> set[str]:
    """thread_ids that any IM channel still maps to — never delete these."""
    protected: set[str] = set()
    for path in _STORE_PATHS:
        if not path.exists():
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            logger.warning("[idle-thread-cleanup] could not parse %s; treating as empty", path)
            continue
        if not isinstance(data, dict):
            continue
        for entry in data.values():
            if isinstance(entry, dict):
                tid = entry.get("thread_id")
                if isinstance(tid, str):
                    protected.add(tid)
    return protected


def _parse_ts(raw: str | None) -> datetime | None:
    if not isinstance(raw, str):
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None


def _list_idle_threads(client: httpx.Client) -> list[dict]:
    out: list[dict] = []
    offset = 0
    while True:
        resp = client.post(
            f"{LANGGRAPH_URL}/threads/search",
            json={"status": "idle", "limit": PAGE_SIZE, "offset": offset},
        )
        resp.raise_for_status()
        batch = resp.json()
        if not isinstance(batch, list) or not batch:
            break
        out.extend(batch)
        if len(batch) < PAGE_SIZE:
            break
        offset += PAGE_SIZE
    return out


def _delete_langgraph_thread(client: httpx.Client, thread_id: str) -> bool:
    try:
        resp = client.delete(f"{LANGGRAPH_URL}/threads/{thread_id}")
    except httpx.HTTPError as exc:
        logger.warning("[idle-thread-cleanup] langgraph delete failed for %s: %s", thread_id, exc)
        return False
    if resp.status_code in (200, 204, 404):
        return True
    logger.warning(
        "[idle-thread-cleanup] langgraph delete %s returned %d: %s",
        thread_id,
        resp.status_code,
        resp.text[:200],
    )
    return False


def _delete_gateway_thread_dir(client: httpx.Client, thread_id: str) -> None:
    try:
        client.delete(f"{GATEWAY_URL}/api/threads/{thread_id}")
    except httpx.HTTPError as exc:
        logger.debug("[idle-thread-cleanup] gateway dir cleanup failed for %s: %s", thread_id, exc)


def _run_once() -> None:
    protected = _protected_thread_ids()
    cutoff = datetime.now(timezone.utc).timestamp() - MAX_AGE_DAYS * 86400

    with httpx.Client(timeout=10.0) as client:
        try:
            idle = _list_idle_threads(client)
        except httpx.HTTPError as exc:
            logger.warning("[idle-thread-cleanup] threads/search failed: %s", exc)
            return

        total = len(idle)
        candidates: list[str] = []
        skipped_protected = 0
        skipped_young = 0
        for t in idle:
            tid = t.get("thread_id")
            if not isinstance(tid, str):
                continue
            if tid in protected:
                skipped_protected += 1
                continue
            ts = _parse_ts(t.get("updated_at") or t.get("created_at"))
            if ts is None or ts.timestamp() > cutoff:
                skipped_young += 1
                continue
            candidates.append(tid)

        deleted = 0
        for tid in candidates:
            if _delete_langgraph_thread(client, tid):
                _delete_gateway_thread_dir(client, tid)
                deleted += 1

    logger.info(
        "[idle-thread-cleanup] idle=%d protected=%d young=%d deleted=%d max_age_days=%d",
        total,
        skipped_protected,
        skipped_young,
        deleted,
        MAX_AGE_DAYS,
    )


def run_loop() -> None:
    """Entry point invoked by cron_supervisor."""
    logger.info(
        "[idle-thread-cleanup] cron started; interval=%ds max_age_days=%d langgraph=%s",
        INTERVAL_SEC,
        MAX_AGE_DAYS,
        LANGGRAPH_URL,
    )
    time.sleep(INITIAL_DELAY_SEC)
    while True:
        try:
            _run_once()
        except Exception:
            logger.exception("[idle-thread-cleanup] cycle failed; will retry next interval")
        time.sleep(INTERVAL_SEC)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    _run_once()
