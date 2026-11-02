"""Tests for peer-aware drafting (PRD §7 H1, 2026-07-02).

The webhook drafter (and via it the owner-slack-draft cron) conditions
outbound drafts on the correspondent's Honcho peer context. Everything must
fail OPEN: a dead Honcho or unknown sender yields the exact pre-H1 behavior.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

import webhook_receiver as wr

_SKILLS = Path(__file__).resolve().parent.parent.parent / "skills" / "custom"


def _load(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def pct():
    return _load(_SKILLS / "honcho-peers" / "peer_card_tool.py", "pct_h1_test")


@pytest.fixture(scope="module")
def osd():
    return _load(_SKILLS / "slack-search" / "owner_slack_draft_cron.py", "osd_h1_test")


# ---------------------------------------------------------------------------
# _extract_email
# ---------------------------------------------------------------------------

def test_extract_email_from_display_header():
    assert wr._extract_email("Jane Doe <Jane.Doe@CIM.com>") == "jane.doe@cim.com"


def test_extract_email_bare_address():
    assert wr._extract_email("jane@cim.com") == "jane@cim.com"


def test_extract_email_rejects_names_and_garbage():
    assert wr._extract_email("Jane Doe") == ""
    assert wr._extract_email("") == ""
    assert wr._extract_email(None) == ""


# ---------------------------------------------------------------------------
# _peer_context_for fails open
# ---------------------------------------------------------------------------

def test_peer_context_empty_when_module_missing(monkeypatch):
    monkeypatch.setattr(wr, "_peer_card_module", lambda: None)
    assert wr._peer_context_for("Jane <jane@cim.com>") == ""


def test_peer_context_empty_when_no_email(monkeypatch):
    called = []
    monkeypatch.setattr(wr, "_peer_card_module", lambda: called.append(1))
    assert wr._peer_context_for("Jane Doe") == ""
    assert not called  # short-circuits before loading the module


def test_peer_context_empty_when_peer_context_raises(monkeypatch):
    class Boom:
        @staticmethod
        def peer_context(email):
            raise RuntimeError("honcho down")
    monkeypatch.setattr(wr, "_peer_card_module", lambda: Boom)
    assert wr._peer_context_for("jane@cim.com") == ""


def test_peer_context_passes_through(monkeypatch):
    class Mod:
        @staticmethod
        def peer_context(email):
            assert email == "jane@cim.com"
            return "Prefers short bullet emails."
    monkeypatch.setattr(wr, "_peer_card_module", lambda: Mod)
    assert wr._peer_context_for("Jane <jane@cim.com>") == "Prefers short bullet emails."


# ---------------------------------------------------------------------------
# draft_reply prompt conditioning
# ---------------------------------------------------------------------------

class _FakeAnthropic:
    last_prompt = None

    def __init__(self, api_key=None):
        pass

    class _Resp:
        class _Block:
            text = "Sounds good — I'll confirm the amount tomorrow."
        content = [_Block()]

    @property
    def messages(self):
        outer = self

        class _Messages:
            @staticmethod
            def create(model=None, max_tokens=None, messages=None):
                _FakeAnthropic.last_prompt = messages[0]["content"]
                return outer._Resp()
        return _Messages()


@pytest.fixture()
def fake_anthropic(monkeypatch):
    monkeypatch.setattr(wr, "Anthropic", _FakeAnthropic)
    monkeypatch.setattr(wr, "ANTHROPIC_API_KEY", "test-key")
    _FakeAnthropic.last_prompt = None
    return _FakeAnthropic


def test_draft_includes_peer_block_when_context_known(fake_anthropic, monkeypatch):
    monkeypatch.setattr(wr, "_peer_context_for",
                        lambda sender: "Prefers short, numbers-first emails.")
    out = wr.draft_reply("Jane <jane@cim.com>", "Facility", "please confirm",
                         "confirm request", "reply confirming")
    assert out
    assert "What we know about this correspondent" in fake_anthropic.last_prompt
    assert "Prefers short, numbers-first emails." in fake_anthropic.last_prompt


def test_draft_prompt_unchanged_when_no_context(fake_anthropic, monkeypatch):
    monkeypatch.setattr(wr, "_peer_context_for", lambda sender: "")
    out = wr.draft_reply("Unknown <new@nowhere.com>", "Hi", "hello",
                         "greeting", "reply")
    assert out
    assert "What we know about this correspondent" not in fake_anthropic.last_prompt


# ---------------------------------------------------------------------------
# peer_context cache (positive AND negative results)
# ---------------------------------------------------------------------------

def test_peer_context_caches_positive(pct, monkeypatch):
    calls = []

    class FakePeer:
        def chat(self, prompt):
            calls.append(1)
            return "Formal, likes detail."

    class FakeClient:
        def peer(self, peer_id, metadata=None):
            return FakePeer()

    monkeypatch.setattr(pct, "_honcho_enabled", lambda: True)
    monkeypatch.setattr(pct, "_get_honcho_client", lambda: FakeClient())
    assert pct.peer_context("cache-pos@x.com") == "Formal, likes detail."
    assert pct.peer_context("cache-pos@x.com") == "Formal, likes detail."
    assert len(calls) == 1


def test_peer_context_caches_negative(pct, monkeypatch):
    calls = []

    def boom():
        calls.append(1)
        raise RuntimeError("honcho down")

    monkeypatch.setattr(pct, "_honcho_enabled", lambda: True)
    monkeypatch.setattr(pct, "_get_honcho_client", boom)
    assert pct.peer_context("cache-neg@x.com") is None
    assert pct.peer_context("cache-neg@x.com") is None
    assert len(calls) == 1  # dead Honcho must not add latency per draft


def test_peer_context_rejects_invalid_email(pct, monkeypatch):
    monkeypatch.setattr(pct, "_honcho_enabled", lambda: True)
    assert pct.peer_context("") is None
    assert pct.peer_context("not-an-email") is None


# ---------------------------------------------------------------------------
# owner_slack_draft: sender identity resolution
# ---------------------------------------------------------------------------

def test_sender_ident_returns_name_and_email(osd):
    class FakeUsers:
        @staticmethod
        def users_info(user):
            return {"user": {"profile": {
                "display_name": "Jane", "real_name": "Jane Doe",
                "email": "Jane@CIM.com"}}}
    name, email = osd._sender_ident(FakeUsers(), "U123")
    assert name == "Jane"
    assert email == "jane@cim.com"


def test_sender_ident_fails_open(osd):
    class Boom:
        @staticmethod
        def users_info(user):
            raise RuntimeError("missing scope")
    name, email = osd._sender_ident(Boom(), "U123")
    assert name == "U123"
    assert email == ""
