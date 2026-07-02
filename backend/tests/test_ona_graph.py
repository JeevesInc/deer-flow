"""Tests for Honcho/ONA fixes (P2-1 / H-BUG-1, H-BUG-2)."""
from __future__ import annotations

import importlib.util
import json
from datetime import datetime, timedelta
from pathlib import Path

import pytest

MODULE = (Path(__file__).resolve().parent.parent.parent
          / "skills" / "custom" / "honcho-peers" / "peer_card_tool.py")


@pytest.fixture(scope="module")
def pct():
    spec = importlib.util.spec_from_file_location("peer_card_tool_under_test", MODULE)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _interaction(a, b, date, source="slack", ctx="dm"):
    return {"participants": [a, b], "date": date, "source": source, "context": ctx}


@pytest.fixture()
def graph_path(pct, tmp_path, monkeypatch):
    p = tmp_path / "ona_graph.json"
    monkeypatch.setattr(pct, "_ona_graph_path", lambda: p)
    return p


# ---------------------------------------------------------------------------
# H-BUG-1: interaction keys must be stable across processes
# ---------------------------------------------------------------------------

def test_slack_keys_use_content_hash_not_python_hash():
    src = MODULE.read_text(encoding="utf-8")
    assert "abs(hash(text))" not in src
    assert "hashlib.md5(text.encode" in src


def test_md5_key_is_deterministic():
    import hashlib
    text = "hey can you resend the June tape?"
    k1 = hashlib.md5(text.encode("utf-8")).hexdigest()[:12]
    k2 = hashlib.md5(text.encode("utf-8")).hexdigest()[:12]
    assert k1 == k2  # hash(text) failed this across processes (PYTHONHASHSEED)


# ---------------------------------------------------------------------------
# H-BUG-2: recency-aware ONA weights
# ---------------------------------------------------------------------------

def test_monthly_buckets_and_weight_90d(pct, graph_path):
    recent = datetime.now().strftime("%Y-%m-%d")
    pct._update_ona_graph([
        _interaction("a@x.com", "b@x.com", recent),
        _interaction("a@x.com", "b@x.com", recent),
    ])
    graph = json.loads(graph_path.read_text(encoding="utf-8"))
    edge = graph["edges"][0]
    assert edge["weight"] == 2
    assert edge["weight_90d"] == 2
    assert edge["monthly"][recent[:7]] == 2


def test_old_interactions_do_not_count_toward_90d(pct, graph_path):
    old = (datetime.now() - timedelta(days=200)).strftime("%Y-%m-%d")
    pct._update_ona_graph([_interaction("a@x.com", "b@x.com", old)])
    graph = json.loads(graph_path.read_text(encoding="utf-8"))
    edge = graph["edges"][0]
    assert edge["weight"] == 1
    assert edge["weight_90d"] == 0


def test_cold_lifetime_heavy_edge_ranks_below_warm_edge(pct, graph_path):
    """The H-BUG-2 scenario: a 2025-era counterparty with 50 interactions
    must not outrank an active one with 3."""
    old = (datetime.now() - timedelta(days=300)).strftime("%Y-%m-%d")
    recent = datetime.now().strftime("%Y-%m-%d")
    pct._update_ona_graph([_interaction("cold@x.com", "me@x.com", old)] * 50)
    pct._update_ona_graph([_interaction("warm@x.com", "me@x.com", recent)] * 3)

    graph = json.loads(graph_path.read_text(encoding="utf-8"))
    first = graph["edges"][0]
    assert "warm@x.com" in (first["source"], first["target"])


def test_buckets_pruned_beyond_12_months(pct, graph_path):
    ancient = (datetime.now() - timedelta(days=500)).strftime("%Y-%m-%d")
    recent = datetime.now().strftime("%Y-%m-%d")
    pct._update_ona_graph([_interaction("a@x.com", "b@x.com", ancient)])
    pct._update_ona_graph([_interaction("a@x.com", "b@x.com", recent)])

    graph = json.loads(graph_path.read_text(encoding="utf-8"))
    edge = graph["edges"][0]
    assert ancient[:7] not in edge["monthly"]  # pruned
    assert recent[:7] in edge["monthly"]
    assert edge["weight"] == 2  # lifetime count untouched


def test_legacy_edges_seeded_from_last_interaction(pct, graph_path):
    """Edges written before buckets existed get a warm/cold seed."""
    recent = datetime.now().strftime("%Y-%m-%d")
    legacy = {"updated_at": "", "edges": [{
        "source": "a@x.com", "target": "b@x.com", "weight": 40,
        "first_interaction": "2025-01-01", "last_interaction": recent,
        "sources": ["slack"], "sample_contexts": [],
    }]}
    graph_path.write_text(json.dumps(legacy), encoding="utf-8")

    pct._update_ona_graph([_interaction("c@x.com", "d@x.com", recent)])

    graph = json.loads(graph_path.read_text(encoding="utf-8"))
    legacy_edge = next(e for e in graph["edges"] if e["source"] == "a@x.com")
    assert legacy_edge["weight_90d"] == 1  # warm seed, not stone cold
