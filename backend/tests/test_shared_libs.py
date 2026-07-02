"""Tests for the _shared client libraries (P2-4 / Skills-H5, GW-F11/F13)."""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

SHARED = Path(__file__).resolve().parent.parent.parent / "skills" / "custom" / "_shared"


def _load(name):
    spec = importlib.util.spec_from_file_location(f"{name}_shared_under_test", SHARED / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ---------------------------------------------------------------------------
# llm.py
# ---------------------------------------------------------------------------

def test_llm_tier_constants_present():
    llm = _load("llm")
    assert llm.SONNET.startswith("claude-")
    assert llm.HAIKU.startswith("claude-")
    assert llm.OPUS.startswith("claude-")


def test_text_from_response_skips_thinking_blocks():
    llm = _load("llm")
    resp = SimpleNamespace(content=[
        SimpleNamespace(type="thinking", thinking="hmm"),
        SimpleNamespace(type="text", text="hello "),
        SimpleNamespace(type="text", text="world"),
    ])
    assert llm.text_from_response(resp) == "hello world"


def test_text_from_response_empty_content():
    llm = _load("llm")
    assert llm.text_from_response(SimpleNamespace(content=[])) == ""
    assert llm.text_from_response(SimpleNamespace(content=None)) == ""


# ---------------------------------------------------------------------------
# cron_state.py
# ---------------------------------------------------------------------------

def test_atomic_write_then_load_roundtrip(tmp_path):
    cs = _load("cron_state")
    p = tmp_path / "state.json"
    cs.atomic_write_json(p, {"a": 1})
    assert cs.load_json_state(p) == {"a": 1}
    assert not p.with_suffix(".json.tmp").exists()


def test_load_missing_returns_default(tmp_path):
    cs = _load("cron_state")
    assert cs.load_json_state(tmp_path / "nope.json", {"fresh": True}) == {"fresh": True}


def test_corrupt_state_quarantined_not_crash_loop(tmp_path):
    cs = _load("cron_state")
    p = tmp_path / "state.json"
    p.write_text("{ this is not json", encoding="utf-8")
    out = cs.load_json_state(p, {"fresh": True})
    assert out == {"fresh": True}
    assert not p.exists()  # moved aside, not left to re-fail every tick
    quarantined = list(tmp_path.glob("state.json.corrupt-*"))
    assert len(quarantined) == 1
    assert "not json" in quarantined[0].read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# notify.py
# ---------------------------------------------------------------------------

def test_owner_id_falls_back_to_brian_not_bot(monkeypatch):
    notify = _load("notify")
    monkeypatch.delenv("SLACK_OWNER_USER_ID", raising=False)
    assert notify.owner_user_id() == "U05B5HGNCN9"  # Brian, NOT the bot's U09PQTZ5DHC


def test_dm_owner_never_raises_without_token(monkeypatch):
    notify = _load("notify")
    monkeypatch.delenv("SLACK_BOT_TOKEN", raising=False)
    assert notify.dm_owner("hello") is False


# ---------------------------------------------------------------------------
# redshift.py
# ---------------------------------------------------------------------------

def test_connect_requires_env(monkeypatch):
    rs = _load("redshift")
    for k in ("REDSHIFT_HOST", "REDSHIFT_PORT", "REDSHIFT_DB", "REDSHIFT_USER", "REDSHIFT_PASSWORD"):
        monkeypatch.delenv(k, raising=False)
    with pytest.raises(EnvironmentError, match="REDSHIFT_HOST"):
        rs.connect()


def test_connect_uses_required_ssl_settings(monkeypatch):
    """Windows gotcha: sslmode=require + sslrootcert=disable is load-bearing."""
    rs = _load("redshift")
    captured = {}

    def fake_connect(**kwargs):
        captured.update(kwargs)
        return "conn"

    import sys
    monkeypatch.setitem(sys.modules, "psycopg2", SimpleNamespace(connect=fake_connect))
    for k, v in {"REDSHIFT_HOST": "h", "REDSHIFT_PORT": "5439", "REDSHIFT_DB": "d",
                 "REDSHIFT_USER": "u", "REDSHIFT_PASSWORD": "p"}.items():
        monkeypatch.setenv(k, v)

    assert rs.connect(statement_timeout_ms=45_000) == "conn"
    assert captured["sslmode"] == "require"
    assert captured["sslrootcert"] == "disable"
    assert "statement_timeout=45000" in captured["options"]


def test_redshift_util_shim_delegates():
    """Existing `from redshift_util import connect` imports must keep working."""
    shim_path = (SHARED.parent / "jeeves-borrowing-base" / "redshift_util.py")
    src = shim_path.read_text(encoding="utf-8")
    assert "from redshift import connect, ensure_deps" in src
    spec = importlib.util.spec_from_file_location("redshift_util_shim_under_test", shim_path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    assert callable(mod.connect)
    assert callable(mod.ensure_deps)
