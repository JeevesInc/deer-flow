"""Unit tests for the LangGraph thread-status collector in app.gateway.metrics.

Covers the two behaviours added when the "Threads by status over time" panel was
pinned at the old 200-row page cap:
  1. pagination — counts sum across pages instead of clamping at one page;
  2. don't-zero-on-error — a failed sub-request leaves the gauge at its prior
     value (collected[status] == False) rather than publishing a spurious 0.
"""

from __future__ import annotations

from app.gateway import metrics


class _FakeResp:
    def __init__(self, status_code: int, payload):
        self.status_code = status_code
        self._payload = payload

    def json(self):
        return self._payload


class _FakeClient:
    """Stands in for httpx.Client; routes .post() to a handler(payload)->_FakeResp."""

    def __init__(self, handler):
        self._handler = handler

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def post(self, url, json):  # noqa: A002 - mirror httpx signature
        return self._handler(json)


def _install_fake_client(monkeypatch, handler):
    monkeypatch.setattr(metrics.httpx, "Client", lambda *a, **k: _FakeClient(handler))


def _paged_handler(counts: dict[str, int], *, fail: set[str] | None = None):
    fail = fail or set()

    def handler(payload):
        status = payload["status"]
        if status in fail:
            return _FakeResp(500, None)
        offset = payload["offset"]
        limit = payload["limit"]
        n = counts.get(status, 0)
        page = [
            {"thread_id": f"{status}-{i}", "created_at": "2026-06-17T00:00:00Z"}
            for i in range(offset, min(offset + limit, n))
        ]
        return _FakeResp(200, page)

    return handler


def test_counts_paginate_beyond_one_page(monkeypatch):
    # idle far exceeds the 200-row page size — the old code clamped this to 200.
    counts = {"busy": 1, "idle": 450, "error": 23, "interrupted": 10}
    _install_fake_client(monkeypatch, _paged_handler(counts))

    info = metrics._collect_thread_status("http://langgraph")

    assert info["counts"] == counts
    assert all(info["collected"].values())


def test_failed_substatus_is_not_reported_as_zero(monkeypatch):
    counts = {"busy": 2, "idle": 300, "error": 23, "interrupted": 10}
    _install_fake_client(monkeypatch, _paged_handler(counts, fail={"idle"}))

    info = metrics._collect_thread_status("http://langgraph")

    assert info["collected"]["idle"] is False
    assert info["counts"]["idle"] == 0  # sentinel only — caller must skip it
    assert info["collected"]["error"] is True
    assert info["counts"]["error"] == 23


def test_refresh_leaves_gauge_untouched_when_status_uncollected(monkeypatch):
    # Seed a known idle value, then refresh with a run where idle fails to collect.
    metrics.g_threads_by_status.labels(status="idle").set(99)
    metrics._thread_status_cache = None  # bypass memo from any prior test
    counts = {"busy": 0, "idle": 300, "error": 23, "interrupted": 10}
    _install_fake_client(monkeypatch, _paged_handler(counts, fail={"idle"}))

    metrics._refresh_langgraph_threads("http://langgraph")

    # idle stayed at its prior value; error was updated from the live count.
    assert metrics.g_threads_by_status.labels(status="idle")._value.get() == 99
    assert metrics.g_threads_by_status.labels(status="error")._value.get() == 23


def test_refresh_memoizes_within_ttl(monkeypatch):
    metrics._thread_status_cache = None
    calls = {"n": 0}

    def handler(payload):
        if payload["status"] == "busy" and payload["offset"] == 0:
            calls["n"] += 1
        n = {"busy": 0, "idle": 5, "error": 0, "interrupted": 0}[payload["status"]]
        page = [{"thread_id": f"x-{i}"} for i in range(payload["offset"], min(payload["offset"] + payload["limit"], n))]
        return _FakeResp(200, page)

    _install_fake_client(monkeypatch, handler)

    metrics._refresh_langgraph_threads("http://langgraph")
    metrics._refresh_langgraph_threads("http://langgraph")  # within TTL → cached

    assert calls["n"] == 1
