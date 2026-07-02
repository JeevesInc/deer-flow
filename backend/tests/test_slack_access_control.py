"""Tests for Slack access control hardening (P2-10 / Chan-H2, Chan-M4).

- Bot-to-bot loop breaker: bot authors bypass the user allowlist, so two
  bots replying to each other used to ping-pong unbounded.
- Approval queue: with SLACK_OWNER_USER_ID unset, ANY workspace user's
  reaction used to execute proposals; and the in-memory reaction dedup died
  with the process, so a restart + re-added reaction re-executed actions.
"""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from app.channels.message_bus import MessageBus
from app.channels.slack import SlackChannel


class _Reached(Exception):
    """Raised by the downstream sentinel to prove the gate let a message through."""


def _channel(**config):
    cfg = {"bot_token": "xoxb-test", "app_token": "xapp-test"}
    cfg.update(config)
    ch = SlackChannel(bus=MessageBus(), config=cfg)
    ch._own_user_id = "U09PQTZ5DHC"
    ch._own_bot_id = "BOWN"
    # Sentinel: first downstream call after the gates.
    ch._download_event_files = MagicMock(side_effect=_Reached())
    return ch


def _bot_event(ts, *, text="hello", channel_type="channel"):
    return {"bot_id": "BOTHER", "user": "UBOT", "ts": ts, "text": text,
            "channel": "C1", "channel_type": channel_type}


class TestBotToBotLoopBreaker:
    def test_bot_channel_message_without_mention_dropped(self):
        ch = _channel()
        ch._handle_message_event(_bot_event("1.0"))  # no exception → gated
        assert not ch._download_event_files.called

    def test_bot_channel_message_with_mention_passes(self):
        ch = _channel()
        with pytest.raises(_Reached):
            ch._handle_message_event(_bot_event("2.0", text="hey <@U09PQTZ5DHC> look"))

    def test_bot_dm_passes_without_mention(self):
        ch = _channel()
        with pytest.raises(_Reached):
            ch._handle_message_event(_bot_event("3.0", channel_type="im"))

    def test_rate_limit_bounds_bot_triggers(self):
        ch = _channel()
        allowed = 0
        for i in range(ch._BOT_TRIGGER_MAX + 3):
            try:
                ch._handle_message_event(_bot_event(f"10.{i}", channel_type="im"))
            except _Reached:
                allowed += 1
        assert allowed == ch._BOT_TRIGGER_MAX

    def test_own_bot_still_filtered(self):
        ch = _channel()
        ch._handle_message_event({"bot_id": "BOWN", "ts": "4.0", "text": "self",
                                  "channel": "C1", "channel_type": "im"})
        assert not ch._download_event_files.called

    def test_human_messages_unaffected(self):
        ch = _channel()
        with pytest.raises(_Reached):
            ch._handle_message_event({"user": "U05B5HGNCN9", "ts": "5.0",
                                      "text": "hi", "channel": "D1", "channel_type": "im"})


class TestApprovalQueueHardening:
    def _reaction(self, user="UOWNER", ts="100.1"):
        return {"type": "reaction_added", "user": user, "reaction": "+1",
                "item": {"type": "message", "channel": "CQUEUE", "ts": ts}}

    def _queue_channel_env(self, monkeypatch):
        monkeypatch.setenv("ANALYST_QUEUE_CHANNEL_ID", "CQUEUE")

    def test_fails_closed_without_owner_id(self, monkeypatch):
        self._queue_channel_env(monkeypatch)
        ch = _channel(owner_user_id="")
        ch._approval_queue._owner_user_id = ""
        ch._approval_queue._lookup_proposal = MagicMock()
        ch._handle_reaction_event(self._reaction())
        assert not ch._approval_queue._lookup_proposal.called  # nobody can approve

    def test_non_owner_reaction_ignored(self, monkeypatch):
        self._queue_channel_env(monkeypatch)
        ch = _channel(owner_user_id="UOWNER")
        ch._approval_queue._lookup_proposal = MagicMock()
        ch._handle_reaction_event(self._reaction(user="USOMEONE"))
        assert not ch._approval_queue._lookup_proposal.called

    def test_durable_outcome_dedup_survives_restart(self, monkeypatch, tmp_path):
        """A recorded reaction outcome must block re-execution even with a
        fresh in-memory dedup set (i.e. after a gateway restart)."""
        self._queue_channel_env(monkeypatch)
        ch = _channel(owner_user_id="UOWNER")
        ch._approval_queue._log_dir = tmp_path
        (tmp_path / "proposal_outcomes.jsonl").write_text(
            json.dumps({"proposal_slack_ts": "100.1", "source": "reaction",
                        "outcome": "approved"}) + "\n", encoding="utf-8")
        ch._approval_queue._lookup_proposal = MagicMock(return_value={"slack_ts": "100.1"})
        ch._approval_queue._approve_proposal = MagicMock()

        ch._handle_reaction_event(self._reaction(ts="100.1"))

        assert not ch._approval_queue._approve_proposal.called

    def test_fresh_approval_executes_once(self, monkeypatch, tmp_path):
        self._queue_channel_env(monkeypatch)
        ch = _channel(owner_user_id="UOWNER")
        ch._approval_queue._log_dir = tmp_path
        ch._approval_queue._lookup_proposal = MagicMock(return_value={"slack_ts": "200.1"})
        ch._approval_queue._approve_proposal = MagicMock()

        ch._handle_reaction_event(self._reaction(ts="200.1"))
        ch._handle_reaction_event(self._reaction(ts="200.1"))  # re-added emoji

        assert ch._approval_queue._approve_proposal.call_count == 1
