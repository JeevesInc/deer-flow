"""Tests for scripts/memory_derive.py (P0-7a / GW-F7 regression).

Root cause fixed here: memory_derive used to read a dead embedded-Qdrant SQLite
(frozen at the 2026-07-01 server migration) and imported the SLL skill via
`$SKILLS_PATH` (unset outside the sandbox), so on this box it failed at import
and never ran. It now reads the live Qdrant server via mem0_store helpers and
resolves memory.json `__file__`-relative.

These tests exercise run()'s decision logic with the mem0/Anthropic boundaries
mocked, so they need neither Qdrant nor an API key.
"""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

BACKEND_DIR = Path(__file__).resolve().parent.parent
MEMORY_DERIVE = BACKEND_DIR / "scripts" / "memory_derive.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("memory_derive_under_test", MEMORY_DERIVE)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture()
def md(tmp_path, monkeypatch):
    """memory_derive module with _paths pointed at a temp memory.json."""
    mod = _load_module()
    mem_path = tmp_path / "memory.json"
    sc_path = tmp_path / "STRATEGIC_CONTEXT.md"
    mem_path.write_text(
        json.dumps({"version": "2.0", "history": {"recentMonths": {"summary": "OLD"}}}),
        encoding="utf-8",
    )
    monkeypatch.setattr(mod, "_paths", lambda: (mem_path, sc_path))
    mod._mem_path = mem_path  # convenience for assertions
    return mod


def _read_summary(mem_path: Path) -> str:
    return json.loads(mem_path.read_text(encoding="utf-8"))["history"]["recentMonths"]["summary"]


def test_skips_when_too_few_facts(md, monkeypatch):
    monkeypatch.setattr(md, "count_mem0_facts", lambda: 10)  # below MIN_FACTS_TO_DERIVE
    called = {"read": False, "derive": False}
    monkeypatch.setattr(md, "read_recent_mem0_facts", lambda k=30: called.__setitem__("read", True) or [])
    monkeypatch.setattr(md, "derive_summary_from_facts", lambda f: called.__setitem__("derive", True) or "NEW")
    md.run()
    assert _read_summary(md._mem_path) == "OLD"  # unchanged
    assert called == {"read": False, "derive": False}  # short-circuited on count


def test_derives_and_writes_when_enough_facts(md, monkeypatch):
    monkeypatch.setattr(md, "count_mem0_facts", lambda: 4462)
    monkeypatch.setattr(md, "read_recent_mem0_facts", lambda k=30: ["fact a", "fact b"])
    monkeypatch.setattr(md, "derive_summary_from_facts", lambda facts: "NEW SUMMARY")
    md.run()
    assert _read_summary(md._mem_path) == "NEW SUMMARY"


def test_noop_when_summary_unchanged(md, monkeypatch):
    monkeypatch.setattr(md, "count_mem0_facts", lambda: 4462)
    monkeypatch.setattr(md, "read_recent_mem0_facts", lambda k=30: ["x"])
    monkeypatch.setattr(md, "derive_summary_from_facts", lambda facts: "OLD")  # same as existing
    before = md._mem_path.read_text(encoding="utf-8")
    md.run()
    assert md._mem_path.read_text(encoding="utf-8") == before  # not rewritten (no lastUpdated bump)


def test_count_failure_degrades_quietly(md, monkeypatch):
    def boom():
        raise RuntimeError("qdrant down")

    monkeypatch.setattr(md, "count_mem0_facts", boom)
    monkeypatch.setattr(md, "derive_summary_from_facts", lambda facts: "NEW")
    md.run()  # must not raise
    assert _read_summary(md._mem_path) == "OLD"


def test_missing_memory_json_skips(md, monkeypatch):
    md._mem_path.unlink()
    monkeypatch.setattr(md, "count_mem0_facts", lambda: 4462)
    md.run()  # must not raise, nothing to write
    assert not md._mem_path.exists()
