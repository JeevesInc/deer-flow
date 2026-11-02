"""Tests for the canonical identity module (P3-5, 2026-07-02).

The Brian-vs-bot Slack-id mixup is a documented recurring bug class —
webhook_receiver shipped with the BOT's id as the owner default. identity.py
is the single source of truth and guards against the misconfig even when the
env itself is wrong.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

_SHARED = Path(__file__).resolve().parent.parent.parent / "skills" / "custom" / "_shared"

OWNER = "U05B5HGNCN9"
BOT = "U09PQTZ5DHC"


def _load(name: str):
    spec = importlib.util.spec_from_file_location(f"{name}_identity_test", _SHARED / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture()
def identity():
    return _load("identity")


def test_fallback_when_env_unset(identity, monkeypatch):
    monkeypatch.delenv("SLACK_OWNER_USER_ID", raising=False)
    assert identity.owner_slack_id() == OWNER


def test_bot_id_in_env_is_corrected(identity, monkeypatch):
    # The recurring misconfig: env points at the bot's own id.
    monkeypatch.setenv("SLACK_OWNER_USER_ID", BOT)
    assert identity.owner_slack_id() == OWNER


def test_valid_env_value_respected(identity, monkeypatch):
    monkeypatch.setenv("SLACK_OWNER_USER_ID", "U0OTHERUSER")
    assert identity.owner_slack_id() == "U0OTHERUSER"


def test_constants_are_distinct(identity):
    assert identity.OWNER_SLACK_ID_FALLBACK != identity.BOT_SLACK_ID


def test_webhook_receiver_owner_is_never_the_bot():
    """Regression: webhook_receiver.py:79 used to default SLACK_OWNER_USER_ID
    to the BOT's own id."""
    import webhook_receiver as wr
    assert wr.SLACK_OWNER_USER_ID != BOT
    # And the source must not carry the bot id as an owner default anymore.
    src = (Path(wr.__file__)).read_text(encoding="utf-8")
    assert f"'SLACK_OWNER_USER_ID', '{BOT}'" not in src


def test_notify_owner_user_id_uses_identity_guard(monkeypatch):
    import sys
    monkeypatch.setenv("SLACK_OWNER_USER_ID", BOT)
    monkeypatch.syspath_prepend(str(_SHARED))
    sys.modules.pop("identity", None)  # ensure fresh import under our env
    notify = _load("notify")
    assert notify.owner_user_id() == OWNER
