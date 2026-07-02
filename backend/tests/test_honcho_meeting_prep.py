"""Tests for Honcho meeting-prep integrations (PRD §7 H2 + H5, 2026-07-02).

H2: theory-of-mind query presets in peer_card_tool + peer_brief() for the
dossier cron. H5: warmest_mutuals() in ona_tool + the dossier briefing's
prep-extras rendering. All library entry points must fail OPEN — a missing
graph or dead Honcho must never block a briefing.
"""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

_SKILLS = Path(__file__).resolve().parent.parent.parent / "skills" / "custom"


def _load(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def pct():
    return _load(_SKILLS / "honcho-peers" / "peer_card_tool.py", "pct_prep_test")


@pytest.fixture(scope="module")
def ona():
    return _load(_SKILLS / "honcho-peers" / "ona_tool.py", "ona_prep_test")


@pytest.fixture(scope="module")
def cron():
    return _load(_SKILLS / "jeeves-dossier" / "dossier_cron.py", "dossier_cron_prep_test")


# ---------------------------------------------------------------------------
# H2: query presets
# ---------------------------------------------------------------------------

def test_concerns_preset_resolves(pct):
    q = pct._resolve_query_question(["--concerns"])
    assert "unstated" in q


def test_commitments_preset_resolves(pct):
    q = pct._resolve_query_question(["--commitments"])
    assert "commit" in q.lower()


def test_react_to_preset_injects_scenario(pct):
    q = pct._resolve_query_question(["--react-to", "a 15% rate increase"])
    assert "a 15% rate increase" in q
    assert "react" in q.lower()


def test_react_to_without_scenario_is_malformed(pct):
    assert pct._resolve_query_question(["--react-to"]) is None
    assert pct._resolve_query_question(["--react-to", "  "]) is None


def test_free_text_question_passes_through(pct):
    assert pct._resolve_query_question(["what deals are open?"]) == "what deals are open?"


def test_empty_args_is_malformed(pct):
    assert pct._resolve_query_question([]) is None


# ---------------------------------------------------------------------------
# H2: peer_brief fails open
# ---------------------------------------------------------------------------

def test_peer_brief_none_when_honcho_disabled(pct, monkeypatch):
    monkeypatch.setattr(pct, "_honcho_enabled", lambda: False)
    assert pct.peer_brief("a@b.com") is None


def test_peer_brief_none_on_client_error(pct, monkeypatch):
    monkeypatch.setattr(pct, "_honcho_enabled", lambda: True)
    def boom():
        raise RuntimeError("honcho down")
    monkeypatch.setattr(pct, "_get_honcho_client", boom)
    assert pct.peer_brief("a@b.com") is None


def test_peer_brief_returns_chat_text(pct, monkeypatch):
    class FakePeer:
        def chat(self, prompt):
            assert "meet" in prompt.lower() or "concerns" in prompt.lower()
            return "  They care about covenant headroom.  "

    class FakeClient:
        def peer(self, peer_id, metadata=None):
            return FakePeer()

    monkeypatch.setattr(pct, "_honcho_enabled", lambda: True)
    monkeypatch.setattr(pct, "_get_honcho_client", lambda: FakeClient())
    assert pct.peer_brief("A@B.com") == "They care about covenant headroom."


# ---------------------------------------------------------------------------
# H5: warmest_mutuals
# ---------------------------------------------------------------------------

BRIAN = "brian.mauck@tryjeeves.com"
PEER = "counterparty@cim.com"


def _edge(a, b, weight=1, weight_90d=0, last=""):
    return {"source": a, "target": b, "weight": weight,
            "weight_90d": weight_90d, "last_interaction": last}


def _write_graph(ona, tmp_path, monkeypatch, edges):
    monkeypatch.setattr(ona, "_dossier_dir", lambda: tmp_path)
    (tmp_path / "ona_graph.json").write_text(json.dumps({"edges": edges}))


def test_mutuals_ranked_by_colder_leg_recency_first(ona, tmp_path, monkeypatch):
    _write_graph(ona, tmp_path, monkeypatch, [
        # warm@x: both legs active recently -> best
        _edge(BRIAN, "warm@x.com", weight=10, weight_90d=5, last="2026-06-30"),
        _edge(PEER, "warm@x.com", weight=8, weight_90d=4, last="2026-06-29"),
        # stale@x: heavy lifetime weight but cold 90d -> outranked
        _edge(BRIAN, "stale@x.com", weight=100, weight_90d=0, last="2025-08-01"),
        _edge(PEER, "stale@x.com", weight=90, weight_90d=0, last="2025-07-01"),
        # onesided@x: warm to Brian only -> warmth = colder (peer) leg
        _edge(BRIAN, "onesided@x.com", weight=50, weight_90d=9, last="2026-06-30"),
        _edge(PEER, "onesided@x.com", weight=1, weight_90d=0, last="2026-01-01"),
    ])
    result = ona.warmest_mutuals(BRIAN, PEER, limit=3)
    assert [m["email"] for m in result][0] == "warm@x.com"
    assert {m["email"] for m in result} == {"warm@x.com", "stale@x.com", "onesided@x.com"}


def test_mutuals_excludes_non_person_and_endpoints(ona, tmp_path, monkeypatch):
    _write_graph(ona, tmp_path, monkeypatch, [
        _edge(BRIAN, "noreply@calendar.google.com", weight=50, weight_90d=9),
        _edge(PEER, "noreply@calendar.google.com", weight=50, weight_90d=9),
        _edge(BRIAN, PEER, weight=5, weight_90d=2),
    ])
    assert ona.warmest_mutuals(BRIAN, PEER) == []


def test_mutuals_fail_open(ona, tmp_path, monkeypatch):
    monkeypatch.setattr(ona, "_dossier_dir", lambda: tmp_path)
    assert ona.warmest_mutuals(BRIAN, PEER) == []          # no graph file
    (tmp_path / "ona_graph.json").write_text("{corrupt")
    assert ona.warmest_mutuals(BRIAN, PEER) == []          # corrupt graph
    assert ona.warmest_mutuals(BRIAN, BRIAN) == []         # same person
    assert ona.warmest_mutuals("", PEER) == []             # empty input


def test_mutuals_limit_respected(ona, tmp_path, monkeypatch):
    edges = []
    for i in range(5):
        edges.append(_edge(BRIAN, f"m{i}@x.com", weight=i + 1, weight_90d=i))
        edges.append(_edge(PEER, f"m{i}@x.com", weight=i + 1, weight_90d=i))
    _write_graph(ona, tmp_path, monkeypatch, edges)
    assert len(ona.warmest_mutuals(BRIAN, PEER, limit=2)) == 2


# ---------------------------------------------------------------------------
# Dossier cron: prep-extras gathering + rendering
# ---------------------------------------------------------------------------

def test_format_prep_extras_renders_brief_and_mutuals(cron):
    text = cron.format_prep_extras({
        "brief": "Wants clarity on the amendment timeline.",
        "mutuals": [{"email": "warm@x.com"}, {"email": "also@x.com"}],
    })
    assert "Read on them" in text
    assert "Wants clarity on the amendment timeline." in text
    assert "warm@x.com, also@x.com" in text


def test_format_prep_extras_empty_when_nothing(cron):
    assert cron.format_prep_extras({}) == ""
    assert cron.format_prep_extras({"brief": None, "mutuals": []}) == ""


def test_gather_prep_extras_fails_open_without_modules(cron, monkeypatch):
    monkeypatch.setattr(cron, "_load_honcho_modules", lambda: None)
    assert cron.gather_prep_extras(["a@b.com"]) == {}


def test_gather_prep_extras_collects_per_attendee(cron, monkeypatch):
    class FakePct:
        @staticmethod
        def peer_brief(email):
            return f"brief for {email}" if email != "empty@x.com" else None

    class FakeOna:
        @staticmethod
        def warmest_mutuals(a, b, limit=2):
            return [{"email": "warm@x.com"}] if b != "empty@x.com" else []

    monkeypatch.setattr(cron, "_load_honcho_modules", lambda: (FakePct, FakeOna))
    extras = cron.gather_prep_extras(["p1@x.com", "empty@x.com"])
    assert extras["p1@x.com"]["brief"] == "brief for p1@x.com"
    assert extras["p1@x.com"]["mutuals"] == [{"email": "warm@x.com"}]
    assert "empty@x.com" not in extras  # nothing found -> omitted entirely


def test_gather_prep_extras_survives_raising_modules(cron, monkeypatch):
    class Boom:
        @staticmethod
        def peer_brief(email):
            raise RuntimeError("honcho down")

        @staticmethod
        def warmest_mutuals(a, b, limit=2):
            raise RuntimeError("graph corrupt")

    monkeypatch.setattr(cron, "_load_honcho_modules", lambda: (Boom, Boom))
    assert cron.gather_prep_extras(["a@b.com"]) == {}


# ---------------------------------------------------------------------------
# H3: dialectic read feeds dossier synthesis
# ---------------------------------------------------------------------------

def test_synthesis_msg_includes_honcho_read(cron):
    msg = cron.build_synthesis_user_msg(
        "p@x.com", {"days_back": 14}, None,
        honcho_read="Relationship warm; open item: Q3 covenant waiver.")
    assert "Honcho dialectic" in msg
    assert "Q3 covenant waiver" in msg
    assert "prefer the raw data" in msg  # gathered data stays authoritative


def test_synthesis_msg_unchanged_without_honcho_read(cron):
    msg = cron.build_synthesis_user_msg("p@x.com", {"days_back": 14}, {"email": "p@x.com"})
    assert "Honcho dialectic" not in msg
    assert "## Existing dossier:" in msg


def test_honcho_dossier_read_fails_open(cron, monkeypatch):
    monkeypatch.setattr(cron, "_load_honcho_modules", lambda: None)
    assert cron._honcho_dossier_read("p@x.com") is None

    class Boom:
        @staticmethod
        def peer_dossier_read(email):
            raise RuntimeError("honcho down")
    monkeypatch.setattr(cron, "_load_honcho_modules", lambda: (Boom, None))
    assert cron._honcho_dossier_read("p@x.com") is None


def test_honcho_dossier_read_passes_through(cron, monkeypatch):
    class Mod:
        @staticmethod
        def peer_dossier_read(email):
            return f"read for {email}"
    monkeypatch.setattr(cron, "_load_honcho_modules", lambda: (Mod, None))
    assert cron._honcho_dossier_read("p@x.com") == "read for p@x.com"


def test_peer_dossier_read_fails_open(pct, monkeypatch):
    monkeypatch.setattr(pct, "_honcho_enabled", lambda: False)
    assert pct.peer_dossier_read("a@b.com") is None

    monkeypatch.setattr(pct, "_honcho_enabled", lambda: True)
    def boom():
        raise RuntimeError("down")
    monkeypatch.setattr(pct, "_get_honcho_client", boom)
    assert pct.peer_dossier_read("a@b.com") is None
