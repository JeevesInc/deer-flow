"""Tests for proposal_learner.run_backfill (P0-7b / Mem-CRIT-2 regression).

The proposal-patterns mem0 namespace was empty because run_daily() only
synthesizes the batch it just labeled, and the synthesizer 404'd for weeks — so
the 48 historical outcomes were never turned into patterns. run_backfill()
replays them in batches with text dedup. These tests mock the Anthropic
synthesizer and the mem0 write, so they need no API key or Qdrant.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

BACKEND_DIR = Path(__file__).resolve().parent.parent
LEARNER = BACKEND_DIR.parent / "skills" / "custom" / "gmail" / "proposal_learner.py"


def _load():
    spec = importlib.util.spec_from_file_location("proposal_learner_under_test", LEARNER)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture()
def pl():
    return _load()


def _feed(mod, monkeypatch, n_pairs):
    """Point _read_jsonl at n matchable proposal/outcome pairs."""
    props = [{"slack_ts": str(i), "subject": f"s{i}"} for i in range(n_pairs)]
    outs = [{"proposal_slack_ts": str(i), "outcome": "ignored"} for i in range(n_pairs)]
    monkeypatch.setattr(mod, "_read_jsonl", lambda path: outs if "outcomes" in str(path) else props)


def test_backfill_batches_and_dedupes(pl, monkeypatch):
    _feed(pl, monkeypatch, 25)
    calls = {"n": 0}

    def fake_synth(chunk):
        calls["n"] += 1
        return [f"pattern-{calls['n']}", "SHARED-DUP"]  # a unique + a repeated pattern

    writes: list[str] = []
    monkeypatch.setattr(pl, "synthesize_patterns", fake_synth)
    monkeypatch.setattr(pl, "write_pattern_to_mem0", writes.append)

    summary = pl.run_backfill(dry_run=False, batch_size=12)

    assert summary["pairs"] == 25
    assert summary["batches"] == 3  # 12 + 12 + 1
    # 3 unique "pattern-N" + one "SHARED-DUP" survives dedup = 4
    assert summary["patterns_added"] == 4
    assert sorted(writes) == ["SHARED-DUP", "pattern-1", "pattern-2", "pattern-3"]


def test_backfill_dry_run_writes_nothing(pl, monkeypatch):
    _feed(pl, monkeypatch, 5)
    writes: list[str] = []
    monkeypatch.setattr(pl, "synthesize_patterns", lambda chunk: ["only-pattern"])
    monkeypatch.setattr(pl, "write_pattern_to_mem0", writes.append)

    summary = pl.run_backfill(dry_run=True, batch_size=12)

    assert writes == []                     # nothing written to mem0
    assert summary["patterns_added"] == 1   # but the count still reflects what would be added


def test_backfill_no_pairs_skips_synth(pl, monkeypatch):
    monkeypatch.setattr(pl, "_read_jsonl", lambda path: [])
    called = {"synth": False}
    monkeypatch.setattr(pl, "synthesize_patterns", lambda c: called.__setitem__("synth", True) or [])
    summary = pl.run_backfill(dry_run=False)
    assert summary["pairs"] == 0
    assert called["synth"] is False


# --- response parsing (the two live bugs the backfill surfaced) ---------------


class _Block:
    def __init__(self, type_, **kw):
        self.type = type_
        for k, v in kw.items():
            setattr(self, k, v)


class _Resp:
    def __init__(self, content):
        self.content = content


def test_text_from_response_skips_thinking_block(pl):
    # claude-sonnet-5 returns a ThinkingBlock first (no .text) then the TextBlock.
    resp = _Resp([_Block("thinking", thinking="reasoning..."), _Block("text", text='{"patterns": []}')])
    assert pl._text_from_response(resp) == '{"patterns": []}'


def test_parse_patterns_json_handles_fences_and_trailing_prose(pl):
    fenced = '```json\n{"patterns": [{"text": "P1"}]}\n```'
    assert pl._parse_patterns_json(fenced)["patterns"][0]["text"] == "P1"
    trailing = 'Here you go:\n{"patterns": [{"text": "P2"}]}\nHope that helps!'
    assert pl._parse_patterns_json(trailing)["patterns"][0]["text"] == "P2"
    assert pl._parse_patterns_json("not json at all") is None
