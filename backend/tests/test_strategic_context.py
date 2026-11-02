"""Tests for the strategic-context semantic retrieval layer.

Covers the pure logic (section splitting, hash-change detection, threshold
gating) and the middleware's system-message merge, all without a real mem0/
Qdrant backend (the mem0 calls are monkeypatched).
"""

import importlib

import pytest

store = importlib.import_module("deerflow.agents.memory.strategic_context_store")


SAMPLE = """# Strategic Context — Title

> Preamble note, last update 2026-07-01.

---

## Company Overview

Jeeves is a fintech. Brian runs Capital Markets.

---

## Active Facilities & Deals

| Facility | Counterparty | Status |
|----------|-------------|--------|
| US LOC | CIM | Closed May 29 |
| MX SOFOM | CIM | Active |
| NB SOFOM | Neuberger Berman | Target close end of June |

---

## Business Context

- Redshift data lags 1 day
- Portfolio reports: report date = 1st of month
- Jeeves uses Metabase for BI (NOT Sigma)
"""


# ── split_sections ────────────────────────────────────────────────────────


def test_split_produces_section_chunks():
    chunks = store.split_sections(SAMPLE, max_chunk_chars=10_000)
    sections = {c["section"] for c in chunks}
    assert "Company Overview" in sections
    assert "Active Facilities & Deals" in sections
    assert "Business Context" in sections


def test_each_chunk_carries_its_header():
    chunks = store.split_sections(SAMPLE, max_chunk_chars=10_000)
    for c in chunks:
        if c["section"]:
            assert c["text"].startswith(f"## {c['section']}")


def test_small_section_is_single_chunk():
    chunks = store.split_sections(SAMPLE, max_chunk_chars=10_000)
    overview = [c for c in chunks if c["section"] == "Company Overview"]
    assert len(overview) == 1
    assert "Jeeves is a fintech" in overview[0]["text"]


def test_large_section_subsplits_and_repeats_table_header():
    # Force a tiny max so the facilities table splits across multiple chunks.
    chunks = store.split_sections(SAMPLE, max_chunk_chars=120)
    fac = [c for c in chunks if c["section"] == "Active Facilities & Deals"]
    assert len(fac) > 1, "large table section should sub-split"
    # Every table sub-chunk repeats the markdown table header row.
    for c in fac:
        assert "| Facility | Counterparty | Status |" in c["text"]
    # No content rows should be lost across the sub-chunks.
    joined = "\n".join(c["text"] for c in fac)
    assert "US LOC" in joined and "MX SOFOM" in joined and "NB SOFOM" in joined


def test_horizontal_rules_and_title_are_dropped():
    chunks = store.split_sections(SAMPLE, max_chunk_chars=10_000)
    for c in chunks:
        assert c["text"].strip() != "---"
    # H1 title line is not emitted as its own strategic chunk.
    assert not any(c["text"].startswith("# Strategic Context — Title") for c in chunks)


# ── file_hash ──────────────────────────────────────────────────────────────


def test_hash_changes_with_content():
    assert store.file_hash("a") == store.file_hash("a")
    assert store.file_hash("a") != store.file_hash("b")


# ── config knobs ─────────────────────────────────────────────────────────


def test_enabled_toggle(monkeypatch):
    monkeypatch.setenv("STRATEGIC_CONTEXT_ENABLED", "false")
    assert store.is_enabled() is False
    monkeypatch.setenv("STRATEGIC_CONTEXT_ENABLED", "true")
    assert store.is_enabled() is True


def test_min_score_and_top_k_defaults(monkeypatch):
    monkeypatch.delenv("STRATEGIC_CONTEXT_MIN_SCORE", raising=False)
    monkeypatch.delenv("STRATEGIC_CONTEXT_TOP_K", raising=False)
    assert store.get_min_score() == pytest.approx(0.22)
    assert store.get_top_k() == 3
    monkeypatch.setenv("STRATEGIC_CONTEXT_MIN_SCORE", "0.5")
    monkeypatch.setenv("STRATEGIC_CONTEXT_TOP_K", "5")
    assert store.get_min_score() == pytest.approx(0.5)
    assert store.get_top_k() == 5


# ── search threshold gating ─────────────────────────────────────────────────


def test_search_filters_below_threshold(monkeypatch):
    monkeypatch.setenv("STRATEGIC_CONTEXT_ENABLED", "true")
    fake = [
        {"memory": "high", "score": 0.9},
        {"memory": "mid", "score": 0.4},
        {"memory": "low", "score": 0.1},
    ]
    monkeypatch.setattr(
        "deerflow.agents.memory.mem0_store.search_memories",
        lambda query, top_k, user_id: fake,
    )
    out = store.search_strategic_context("q", top_k=3, min_score=0.35)
    kept = {r["memory"] for r in out}
    assert kept == {"high", "mid"}


def test_search_empty_when_nothing_clears(monkeypatch):
    monkeypatch.setenv("STRATEGIC_CONTEXT_ENABLED", "true")
    monkeypatch.setattr(
        "deerflow.agents.memory.mem0_store.search_memories",
        lambda query, top_k, user_id: [{"memory": "x", "score": 0.1}],
    )
    assert store.search_strategic_context("q", top_k=3, min_score=0.35) == []


def test_search_disabled_returns_empty(monkeypatch):
    monkeypatch.setenv("STRATEGIC_CONTEXT_ENABLED", "false")
    # Should short-circuit before touching mem0.
    def _boom(*a, **k):
        raise AssertionError("mem0 should not be queried when disabled")

    monkeypatch.setattr("deerflow.agents.memory.mem0_store.search_memories", _boom)
    assert store.search_strategic_context("q") == []


# ── sync_if_changed ─────────────────────────────────────────────────────────


def test_sync_reindexes_on_change_and_noops_when_unchanged(monkeypatch, tmp_path):
    monkeypatch.setenv("STRATEGIC_CONTEXT_ENABLED", "true")
    sc = tmp_path / "STRATEGIC_CONTEXT.md"
    sc.write_text(SAMPLE, encoding="utf-8")

    monkeypatch.setattr(store, "get_file_path", lambda: sc)
    monkeypatch.setattr(store, "_state_path", lambda: tmp_path / "_strategic_context_index.json")

    added: list[dict] = []
    deleted: list[str] = []
    monkeypatch.setattr(
        "deerflow.agents.memory.mem0_store.add_memories",
        lambda messages, user_id, infer, metadata: added.append({"messages": messages, "meta": metadata}),
    )
    monkeypatch.setattr(
        "deerflow.agents.memory.mem0_store.delete_all_memories",
        lambda user_id: deleted.append(user_id),
    )

    # First sync: file is new → re-index.
    assert store.sync_if_changed() is True
    assert len(deleted) == 1
    assert len(added) > 0
    first_add_count = len(added)

    # Second sync: unchanged → no-op (no new adds/deletes).
    assert store.sync_if_changed() is False
    assert len(added) == first_add_count

    # Change the file → re-index again.
    sc.write_text(SAMPLE + "\n\n## New Section\n\nfresh content", encoding="utf-8")
    assert store.sync_if_changed() is True
    assert len(added) > first_add_count


def test_sync_stores_chunks_verbatim_with_infer_false(monkeypatch, tmp_path):
    monkeypatch.setenv("STRATEGIC_CONTEXT_ENABLED", "true")
    sc = tmp_path / "STRATEGIC_CONTEXT.md"
    sc.write_text(SAMPLE, encoding="utf-8")
    monkeypatch.setattr(store, "get_file_path", lambda: sc)
    monkeypatch.setattr(store, "_state_path", lambda: tmp_path / "_state.json")

    calls: list[dict] = []
    monkeypatch.setattr(
        "deerflow.agents.memory.mem0_store.add_memories",
        lambda messages, user_id, infer, metadata: calls.append({"infer": infer, "user_id": user_id, "content": messages[0]["content"]}),
    )
    monkeypatch.setattr("deerflow.agents.memory.mem0_store.delete_all_memories", lambda user_id: None)

    store.sync_if_changed()
    assert calls, "expected chunks to be added"
    assert all(c["infer"] is False for c in calls), "chunks must be stored raw (infer=False)"
    assert all(c["user_id"] == "strategic-context" for c in calls)


def test_sync_missing_file_is_noop(monkeypatch, tmp_path):
    monkeypatch.setenv("STRATEGIC_CONTEXT_ENABLED", "true")
    monkeypatch.setattr(store, "get_file_path", lambda: tmp_path / "does_not_exist.md")
    assert store.sync_if_changed() is False


# ── middleware ───────────────────────────────────────────────────────────────


def _make_request(messages):
    """Minimal ModelRequest-like stub with the attributes the middleware uses."""

    class _Req:
        def __init__(self, msgs):
            self.messages = msgs
            self.overridden_with = None

        def override(self, messages):
            self.overridden_with = messages
            return _Req(messages)

    return _Req(messages)


def test_middleware_merges_into_leading_system_message(monkeypatch):
    from langchain_core.messages import HumanMessage, SystemMessage

    from deerflow.agents.middlewares.strategic_context_middleware import StrategicContextMiddleware

    monkeypatch.setenv("STRATEGIC_CONTEXT_ENABLED", "true")
    monkeypatch.setattr(store, "sync_if_changed", lambda force=False: False)
    monkeypatch.setattr(
        store,
        "search_strategic_context",
        lambda query, top_k=None, min_score=None: [{"memory": "## NB SOFOM\nTarget close end of June", "score": 0.8}],
    )

    mw = StrategicContextMiddleware()
    req = _make_request([SystemMessage(content="BASE PROMPT"), HumanMessage(content="where does NB stand?")])
    new_req = mw._inject(req)

    sys_msg = new_req.messages[0]
    assert sys_msg.type == "system"
    assert "BASE PROMPT" in sys_msg.content
    assert "<strategic_context>" in sys_msg.content
    assert "NB SOFOM" in sys_msg.content
    # No second system message inserted.
    assert sum(1 for m in new_req.messages if getattr(m, "type", None) == "system") == 1


def test_middleware_noop_when_nothing_retrieved(monkeypatch):
    from langchain_core.messages import HumanMessage, SystemMessage

    from deerflow.agents.middlewares.strategic_context_middleware import StrategicContextMiddleware

    monkeypatch.setenv("STRATEGIC_CONTEXT_ENABLED", "true")
    monkeypatch.setattr(store, "sync_if_changed", lambda force=False: False)
    monkeypatch.setattr(store, "search_strategic_context", lambda query, top_k=None, min_score=None: [])

    mw = StrategicContextMiddleware()
    original = [SystemMessage(content="BASE"), HumanMessage(content="restart the gateway")]
    req = _make_request(list(original))
    new_req = mw._inject(req)
    # Request returned unchanged (no override happened).
    assert new_req.messages[0].content == "BASE"
    assert "<strategic_context>" not in new_req.messages[0].content


def test_middleware_disabled_is_noop(monkeypatch):
    from langchain_core.messages import HumanMessage, SystemMessage

    from deerflow.agents.middlewares.strategic_context_middleware import StrategicContextMiddleware

    monkeypatch.setenv("STRATEGIC_CONTEXT_ENABLED", "false")

    def _boom(*a, **k):
        raise AssertionError("should not search when disabled")

    monkeypatch.setattr(store, "search_strategic_context", _boom)
    mw = StrategicContextMiddleware()
    req = _make_request([SystemMessage(content="BASE"), HumanMessage(content="hi")])
    new_req = mw._inject(req)
    assert new_req.messages[0].content == "BASE"


# ── production-faithful tests against a REAL ModelRequest ────────────────────
# In production the static system prompt is NOT in request.messages — it lives
# in request.system_message and is prepended only at model-invoke time. So
# messages[0] is the first Human message (unless a prior middleware injected a
# system message). These tests use the real ModelRequest to lock that behavior.


def test_real_request_inserts_system_before_human_and_leaves_static_untouched(monkeypatch):
    from langchain.agents.middleware.types import ModelRequest
    from langchain_core.messages import HumanMessage, SystemMessage

    from deerflow.agents.middlewares.strategic_context_middleware import StrategicContextMiddleware

    monkeypatch.setenv("STRATEGIC_CONTEXT_ENABLED", "true")
    monkeypatch.setattr(store, "sync_if_changed", lambda force=False: False)
    monkeypatch.setattr(
        store,
        "search_strategic_context",
        lambda query, top_k=None, min_score=None: [{"memory": "## NB SOFOM\nclose end June", "score": 0.8}],
    )

    req = ModelRequest(
        model=None,
        messages=[HumanMessage("where does NB stand?")],
        system_message=SystemMessage("STATIC SYSTEM PROMPT"),
        tools=[],
    )
    out = StrategicContextMiddleware()._inject(req)

    # Static system prompt is untouched (we never mutate system_message).
    assert out.system_message.content == "STATIC SYSTEM PROMPT"
    # Injection was inserted as a system message ahead of the human message.
    assert out.messages[0].type == "system"
    assert "<strategic_context>" in out.messages[0].content
    assert out.messages[1].type == "human"

    # The list the model actually sees: [static_system, injected_system, human]
    # — two consecutive system messages, which the Anthropic adapter merges.
    final = [out.system_message, *out.messages]
    assert [m.type for m in final] == ["system", "system", "human"]


def test_middleware_memoizes_retrieval_and_sync_per_instance(monkeypatch):
    """Within a run (one instance), the same query must retrieve + sync once,
    not on every model call in the tool loop."""
    from langchain.agents.middleware.types import ModelRequest
    from langchain_core.messages import HumanMessage, SystemMessage

    from deerflow.agents.middlewares.strategic_context_middleware import StrategicContextMiddleware

    monkeypatch.setenv("STRATEGIC_CONTEXT_ENABLED", "true")

    sync_calls = {"n": 0}
    search_calls = {"n": 0}

    def _sync(force=False):
        sync_calls["n"] += 1
        return False

    def _search(query, top_k=None, min_score=None):
        search_calls["n"] += 1
        return [{"memory": "## NB SOFOM\nclose end June", "score": 0.8}]

    monkeypatch.setattr(store, "sync_if_changed", _sync)
    monkeypatch.setattr(store, "search_strategic_context", _search)

    mw = StrategicContextMiddleware()

    def fresh_req():
        return ModelRequest(
            model=None,
            messages=[HumanMessage("where does NB stand?")],
            system_message=SystemMessage("STATIC"),
            tools=[],
        )

    # Simulate three model calls within one run (same query each time).
    for _ in range(3):
        out = mw._inject(fresh_req())
        assert "<strategic_context>" in out.messages[0].content

    assert sync_calls["n"] == 1, "sync should run once per run, not per model call"
    assert search_calls["n"] == 1, "retrieval should be memoized within a run"


def test_real_request_final_messages_pass_anthropic_formatter(monkeypatch):
    """The injected layout must survive langchain_anthropic's _format_messages
    (i.e. consecutive system messages get merged, not rejected)."""
    from langchain.agents.middleware.types import ModelRequest
    from langchain_core.messages import HumanMessage, SystemMessage

    from deerflow.agents.middlewares.strategic_context_middleware import StrategicContextMiddleware

    monkeypatch.setenv("STRATEGIC_CONTEXT_ENABLED", "true")
    monkeypatch.setattr(store, "sync_if_changed", lambda force=False: False)
    monkeypatch.setattr(
        store,
        "search_strategic_context",
        lambda query, top_k=None, min_score=None: [{"memory": "## Business Context\nRedshift lags 1 day", "score": 0.6}],
    )

    req = ModelRequest(
        model=None,
        messages=[HumanMessage("data freshness?")],
        system_message=SystemMessage("STATIC"),
        tools=[],
    )
    out = StrategicContextMiddleware()._inject(req)
    final = [out.system_message, *out.messages]

    from langchain_anthropic.chat_models import _format_messages

    system, formatted = _format_messages(final)
    # No exception, single merged system containing both static + injected text.
    system_text = system if isinstance(system, str) else " ".join(b.get("text", "") for b in system)
    assert "STATIC" in system_text
    assert "<strategic_context>" in system_text
    # Exactly one human turn remains.
    assert sum(1 for m in formatted if m["role"] == "user") == 1
