"""Persistent dispatch retry queue.

When autonomous_dispatch() returns False (at capacity), callers can use
enqueue_or_dispatch() to queue work for retry instead of silently dropping it.

Queue file: backend/.deer-flow/dispatch_queue.jsonl
Drain interval: 60s | Max depth: 20 items | Max item age: 12h

Usage:
    from dispatch_queue import enqueue_or_dispatch
    enqueue_or_dispatch(prompt="...", notification="...", category="diligence")
"""
import json, logging, os, threading, time
from datetime import datetime, timezone
from pathlib import Path

log = logging.getLogger("dispatch_queue")

_HERE = Path(__file__).resolve()
_BACKEND = _HERE.parents[3] / "backend"
QUEUE_PATH = _BACKEND / ".deer-flow" / "dispatch_queue.jsonl"
MAX_QUEUE_DEPTH = 20
RETRY_INTERVAL = 60
MAX_ITEM_AGE_HOURS = 12
_queue_lock = threading.Lock()
_drain_thread = None


def _now_iso():
    return datetime.now(timezone.utc).isoformat()

def _read_queue():
    if not QUEUE_PATH.exists():
        return []
    items = []
    for line in QUEUE_PATH.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            try:
                items.append(json.loads(line))
            except Exception:
                pass
    return items

def _write_queue(items):
    QUEUE_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = QUEUE_PATH.with_suffix(".jsonl.tmp")
    with tmp.open("w", encoding="utf-8") as f:
        for item in items:
            f.write(json.dumps(item) + "\n")
    tmp.replace(QUEUE_PATH)

def _is_stale(item):
    ts = item.get("queued_at", "")
    if not ts:
        return False
    try:
        queued = datetime.fromisoformat(ts)
        age = datetime.now(timezone.utc) - queued
        return age.total_seconds() > MAX_ITEM_AGE_HOURS * 3600
    except Exception:
        return False

def enqueue_or_dispatch(prompt, *, notification, category="general",
                        source_id=None, source_metadata=None):
    """Try dispatch immediately; queue on capacity rejection. Returns True if dispatched now."""
    from autonomous_dispatch import dispatch
    ok = dispatch(prompt, notification=notification, category=category,
                  source_id=source_id, source_metadata=source_metadata)
    if not ok:
        _enqueue(prompt, notification=notification, category=category,
                 source_id=source_id, source_metadata=source_metadata)
    return ok

def _enqueue(prompt, *, notification, category="general", source_id=None, source_metadata=None):
    item = {"queued_at": _now_iso(), "prompt": prompt, "notification": notification,
            "category": category, "source_id": source_id, "source_metadata": source_metadata or {}}
    with _queue_lock:
        items = [i for i in _read_queue() if not _is_stale(i)]
        items.append(item)
        if len(items) > MAX_QUEUE_DEPTH:
            log.warning("Queue overflow: dropping %d oldest items", len(items) - MAX_QUEUE_DEPTH)
            items = items[-MAX_QUEUE_DEPTH:]
        _write_queue(items)
        log.info("Enqueued %s (depth: %d)", category, len(items))
    _ensure_drain_thread()

def _drain_once():
    from autonomous_dispatch import dispatch, active_run_count, MAX_CONCURRENT_RUNS
    if active_run_count() >= MAX_CONCURRENT_RUNS:
        return 0
    with _queue_lock:
        items = [i for i in _read_queue() if not _is_stale(i)]
        if not items:
            _write_queue([])
            return 0
        item = items.pop(0)
        _write_queue(items)
    ok = dispatch(item["prompt"], notification=item["notification"],
                  category=item.get("category","general"),
                  source_id=item.get("source_id"), source_metadata=item.get("source_metadata"))
    if not ok:
        with _queue_lock:
            current = _read_queue()
            _write_queue([item] + current)
        return 0
    log.info("Drained queued %s (queued %s)", item.get("category"), item.get("queued_at"))
    return 1

def _drain_loop():
    while True:
        time.sleep(RETRY_INTERVAL)
        try:
            _drain_once()
        except Exception as e:
            log.warning("drain_loop error: %s", e)

def _ensure_drain_thread():
    global _drain_thread
    if _drain_thread and _drain_thread.is_alive():
        return
    _drain_thread = threading.Thread(target=_drain_loop, name="dispatch-queue-drain", daemon=True)
    _drain_thread.start()

def queue_depth():
    with _queue_lock:
        return len([i for i in _read_queue() if not _is_stale(i)])

def queue_status():
    with _queue_lock:
        items = _read_queue()
        active = [i for i in items if not _is_stale(i)]
        return {"pending": len(active), "oldest": active[0].get("queued_at") if active else None,
                "categories": [i.get("category") for i in active]}
