"""Tests for P2-1 memory consolidation (Mem-H2, §4.1 SLL fold-in)."""
from __future__ import annotations

import importlib.util
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from langchain_core.messages import AIMessage, HumanMessage

from deerflow.agents.middlewares.memory_middleware import (
    MEMORY_OPT_OUT_SENTINEL,
    MemoryMiddleware,
    _is_ops_thread,
)

BACKEND = Path(__file__).resolve().parent.parent


# ---------------------------------------------------------------------------
# Ops-noise exclusion (Mem-H2)
# ---------------------------------------------------------------------------

class TestOpsThreadExclusion:
    def test_cron_headers_detected(self):
        for header in ("DREAM SESSION #42 — Tuesday", "END OF DAY REVIEW #9 -- x",
                       "WEEKLY OPEN ITEMS REVIEW #3 -- x", "LATENT-LEARNING REVIEW #1 -- x"):
            assert _is_ops_thread([HumanMessage(content=header + "\nbody")]) is True

    def test_sentinel_detected_anywhere_in_first_message(self):
        msg = HumanMessage(content="<memory:off>\nRun the portfolio report for 2026-06-01.")
        assert _is_ops_thread([msg]) is True
        assert MEMORY_OPT_OUT_SENTINEL in msg.content

    def test_normal_conversation_not_excluded(self):
        assert _is_ops_thread([HumanMessage(content="what's the NB facility balance?")]) is False

    def test_header_must_open_the_thread(self):
        # A user merely MENTIONING a review mid-sentence is not an ops thread.
        assert _is_ops_thread(
            [HumanMessage(content="hey, why did END OF DAY REVIEW #9 miss the CIM email?")]
        ) is False

    @patch("deerflow.agents.middlewares.memory_middleware.get_memory_queue")
    @patch("deerflow.agents.middlewares.memory_middleware.get_memory_config")
    def test_after_agent_skips_queue_for_ops_thread(self, mock_config, mock_queue):
        mock_config.return_value = SimpleNamespace(enabled=True)
        mw = MemoryMiddleware()
        state = {"messages": [
            HumanMessage(content="DREAM SESSION #42 — nightly"),
            AIMessage(content="consolidated."),
        ]}
        runtime = SimpleNamespace(context={"thread_id": "t1"})

        mw.after_agent(state, runtime)

        assert not mock_queue.called

    @patch("deerflow.agents.middlewares.memory_middleware.get_memory_queue")
    @patch("deerflow.agents.middlewares.memory_middleware.get_memory_config")
    def test_after_agent_queues_normal_thread(self, mock_config, mock_queue):
        mock_config.return_value = SimpleNamespace(enabled=True)
        mw = MemoryMiddleware()
        state = {"messages": [
            HumanMessage(content="pull the NB balance"),
            AIMessage(content="here it is"),
        ]}
        runtime = SimpleNamespace(context={"thread_id": "t1"})

        mw.after_agent(state, runtime)

        assert mock_queue.return_value.add.called


# ---------------------------------------------------------------------------
# SLL capture hook
# ---------------------------------------------------------------------------

class TestSLLCaptureHook:
    def test_disabled_by_env(self, monkeypatch):
        monkeypatch.setenv("SLL_CAPTURE_DISABLE", "1")
        started = []
        monkeypatch.setattr("threading.Thread",
                            lambda **kw: started.append(kw) or MagicMock())
        MemoryMiddleware._capture_sll(
            [HumanMessage(content="task")], [AIMessage(content="resp")])
        assert started == []

    def test_spawns_background_thread_when_enabled(self, monkeypatch):
        monkeypatch.delenv("SLL_CAPTURE_DISABLE", raising=False)
        monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
        started = []

        class FakeThread:
            def __init__(self, **kw):
                started.append(kw)

            def start(self):
                pass

        monkeypatch.setattr("threading.Thread", FakeThread)
        MemoryMiddleware._capture_sll(
            [HumanMessage(content="build the BB")], [AIMessage(content="done, link here")])
        assert len(started) == 1
        assert started[0]["daemon"] is True

    def test_wired_into_after_agent(self):
        src = (BACKEND / "packages" / "harness" / "deerflow" / "agents"
               / "middlewares" / "memory_middleware.py").read_text(encoding="utf-8")
        assert "self._capture_sll(user_messages, assistant_messages)" in src


# ---------------------------------------------------------------------------
# Lessons injection (Mem0InjectionMiddleware)
# ---------------------------------------------------------------------------

def _search_router(facts, lessons):
    def _search(query, top_k=10, user_id="deerflow_user"):
        return lessons if user_id == "lessons" else facts
    return _search


class TestLessonsInjection:
    def _middleware(self):
        from deerflow.agents.middlewares.mem0_injection_middleware import Mem0InjectionMiddleware
        return Mem0InjectionMiddleware()

    @patch("deerflow.agents.memory.mem0_store.search_memories")
    def test_lessons_appear_in_separate_block(self, mock_search):
        mock_search.side_effect = _search_router(
            facts=[{"memory": "NB facility is 100MM", "score": 0.6}],
            lessons=[{"memory": "[!] AVOID pasting >5 items inline", "score": 0.5, "id": "l1"}],
        )
        out = self._middleware()._build_injection("draft the email", min_score=0.22)
        assert "<recalled_memories>" in out
        assert "<learned_lessons>" in out
        assert "AVOID pasting" in out

    @patch("deerflow.agents.memory.mem0_store.search_memories")
    def test_lessons_alone_still_inject(self, mock_search):
        mock_search.side_effect = _search_router(
            facts=[],
            lessons=[{"memory": "[+] DO attach files for list data", "score": 0.4, "id": "l2"}],
        )
        out = self._middleware()._build_injection("send list to axel", min_score=0.22)
        assert out is not None
        assert "<learned_lessons>" in out
        assert "<recalled_memories>" not in out

    @patch("deerflow.agents.memory.mem0_store.search_memories")
    def test_lessons_respect_score_gate(self, mock_search):
        mock_search.side_effect = _search_router(
            facts=[],
            lessons=[{"memory": "off-topic lesson", "score": 0.05, "id": "l3"}],
        )
        out = self._middleware()._build_injection("unrelated question", min_score=0.22)
        assert out is None

    @patch("deerflow.agents.memory.mem0_store.search_memories")
    def test_lessons_failure_does_not_break_facts(self, mock_search):
        def _search(query, top_k=10, user_id="deerflow_user"):
            if user_id == "lessons":
                raise RuntimeError("qdrant hiccup")
            return [{"memory": "fact", "score": 0.6}]
        mock_search.side_effect = _search
        out = self._middleware()._build_injection("q", min_score=0.22)
        assert "<recalled_memories>" in out


# ---------------------------------------------------------------------------
# sll_sync meta-lesson heuristic
# ---------------------------------------------------------------------------

def _load_sll_sync():
    spec = importlib.util.spec_from_file_location(
        "sll_sync_under_test", BACKEND / "scripts" / "sll_sync.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class TestMetaLessonHeuristic:
    def test_generic_verify_before_acting_skipped(self):
        sync = _load_sll_sync()
        assert sync._is_generic_meta_lesson(
            "[!] AVOID acting on assumptions — verify state before proceeding with changes.")

    def test_domain_anchored_verify_lesson_kept(self):
        sync = _load_sll_sync()
        assert not sync._is_generic_meta_lesson(
            "[!] AVOID reporting dashboard metrics as live without verifying "
            "data freshness before publishing.")

    def test_non_verify_lesson_kept(self):
        sync = _load_sll_sync()
        assert not sync._is_generic_meta_lesson(
            "[+] DO attach a file instead of pasting long lists into an email body.")
