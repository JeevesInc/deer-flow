"""Regression: blocking gateway handlers must be sync `def` (P1-3 / GW-F6).

These endpoints do synchronous blocking I/O (httpx to LangGraph, mem0/Qdrant,
JSONL parsing). As `async def` they run ON the event loop and starve
/livez//readyz — the documented 2026-06-16 supervisor kill-loop. FastAPI runs
plain `def` handlers in a threadpool, so they must NOT be coroutine functions.
Also guards the mem0-count fix (count_memories, not len(get_all_memories)).
"""
from __future__ import annotations

import inspect

from app.gateway.routers import memory as mem_router


def _endpoints_by_path(routes):
    return {getattr(r, "path", None): getattr(r, "endpoint", None) for r in routes}


def test_app_metrics_and_active_runs_are_sync():
    from app.gateway.app import create_app

    by_path = _endpoints_by_path(create_app().routes)
    for path in ("/metrics", "/api/admin/active-runs"):
        ep = by_path.get(path)
        assert ep is not None, f"{path} route missing"
        assert not inspect.iscoroutinefunction(ep), f"{path} must be sync (blocking I/O → threadpool)"


def test_blocking_memory_endpoints_are_sync():
    by_path = _endpoints_by_path(mem_router.router.routes)
    for path in ("/api/memory", "/api/memory/reload", "/api/memory/status", "/api/memory/mem0"):
        ep = by_path.get(path)
        assert ep is not None, f"{path} route missing"
        assert not inspect.iscoroutinefunction(ep), f"{path} must be sync (blocking mem0 I/O → threadpool)"


def test_get_mem0_count_uses_count_not_fetch_all(monkeypatch):
    import deerflow.agents.memory.mem0_store as store

    calls = {"count": 0, "get_all": 0}
    monkeypatch.setattr(store, "count_memories", lambda **k: calls.__setitem__("count", calls["count"] + 1) or 4462)
    monkeypatch.setattr(store, "get_all_memories", lambda **k: calls.__setitem__("get_all", calls["get_all"] + 1) or [1, 2, 3])

    assert mem_router._get_mem0_count() == 4462   # true count, not the ~20 get_all reports
    assert calls == {"count": 1, "get_all": 0}    # cheap count, never fetches all
