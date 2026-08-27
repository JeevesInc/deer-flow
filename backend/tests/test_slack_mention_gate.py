"""Tests for the channel addressing gate.

Before 2026-08-26 the analyst treated EVERY message in any room it was a member
of as a task addressed to it. In a 1:1 DM that is correct — every message there
is for it. In a channel or group DM it meant the analyst fired a run on other
people's conversations, and it had to be kicked out of a channel.

The gate: shared rooms require an explicit @mention, an `app_mention` event, or
a reply inside a thread it was already mentioned into. DMs are untouched.

Failure direction matters: when in doubt the analyst should stay silent. A
missed mention costs one repeated message; a false trigger costs a run, a Slack
post in front of colleagues, and trust.
"""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from app.channels.message_bus import MessageBus
from app.channels.slack import SlackChannel

BOT = "U09PQTZ5DHC"


class _Reached(Exception):
    """Raised by the downstream sentinel to prove the gate let a message through."""


def _channel(**config):
    cfg = {"bot_token": "xoxb-test", "app_token": "xapp-test"}
    cfg.update(config)
    ch = SlackChannel(bus=MessageBus(), config=cfg)
    ch._own_user_id = BOT
    ch._own_bot_id = "BOWN"
    ch._download_event_files = MagicMock(side_effect=_Reached())
    return ch


def _event(ts="1.0", *, text="hello", channel_type="channel", etype="message",
           thread_ts=None, user="U05B5HGNCN9"):
    ev = {"type": etype, "user": user, "ts": ts, "text": text,
          "channel": "C1", "channel_type": channel_type}
    if thread_ts:
        ev["thread_ts"] = thread_ts
    return ev


def _passes(ch, event) -> bool:
    """True when the message reached downstream processing."""
    try:
        ch._handle_message_event(event)
    except _Reached:
        return True
    return False


# -- the bug being fixed ------------------------------------------------------


@pytest.mark.parametrize("channel_type", ["channel", "group", "mpim"])
def test_unaddressed_message_in_shared_room_is_ignored(channel_type):
    """The exact behavior that got the analyst kicked out of a channel."""
    ch = _channel()
    assert not _passes(ch, _event(text="anyone seen the tape?", channel_type=channel_type))


@pytest.mark.parametrize("channel_type", ["channel", "group", "mpim"])
def test_mention_in_shared_room_passes(channel_type):
    ch = _channel()
    assert _passes(ch, _event(text=f"<@{BOT}> pull the BB", channel_type=channel_type))


def test_dm_still_passes_without_a_mention():
    """Every message in a 1:1 DM is for the analyst by definition."""
    ch = _channel()
    assert _passes(ch, _event(text="pull the BB", channel_type="im"))


def test_app_mention_event_passes_regardless_of_channel_type():
    ch = _channel()
    assert _passes(ch, _event(etype="app_mention", text="hey", channel_type="channel"))


def test_owner_gets_no_exemption_in_a_channel():
    """Explicitly requested: in a channel or group DM it must be addressed
    directly, whoever is speaking."""
    ch = _channel(owner_user_id="U05B5HGNCN9")
    assert not _passes(ch, _event(text="run the tape", user="U05B5HGNCN9"))


# -- thread continuation ------------------------------------------------------


def test_thread_replies_continue_after_a_mention():
    """Re-@mentioning on every turn of a threaded conversation is miserable
    UX, so a mention engages the thread."""
    ch = _channel()
    assert _passes(ch, _event(ts="100.0", text=f"<@{BOT}> what's the headroom?"))
    assert _passes(ch, _event(ts="101.0", text="and for MX?", thread_ts="100.0"))


def test_unrelated_thread_stays_ignored():
    ch = _channel()
    _passes(ch, _event(ts="100.0", text=f"<@{BOT}> hi"))
    assert not _passes(ch, _event(ts="200.0", text="different thread", thread_ts="199.0"))


def test_engagement_expires():
    """Fails toward silence: an old thread stops being live."""
    ch = _channel()
    _passes(ch, _event(ts="100.0", text=f"<@{BOT}> hi"))
    ch._ENGAGED_THREAD_TTL = -1
    assert not _passes(ch, _event(ts="101.0", text="still there?", thread_ts="100.0"))


def test_engaged_thread_map_is_bounded():
    ch = _channel()
    for i in range(600):
        ch._remember_engaged_thread(f"{i}.0")
    assert len(ch._engaged_threads) <= 600, "must not grow without bound"


def test_mention_inside_a_thread_engages_that_thread():
    ch = _channel()
    assert _passes(ch, _event(ts="55.0", text=f"<@{BOT}> take a look", thread_ts="50.0"))
    assert _passes(ch, _event(ts="56.0", text="follow-up", thread_ts="50.0"))


def test_assistant_threads_are_exempt():
    """Slack's assistant container is 1:1 even though channel_type is not im."""
    ch = _channel()
    ch._assistant_threads = {"900.0": 0.0}
    assert _passes(ch, _event(ts="901.0", text="no mention", thread_ts="900.0"))


# -- escape hatch and edge cases ---------------------------------------------


def test_gate_can_be_disabled():
    ch = _channel()
    ch._require_mention_in_channels = False
    assert _passes(ch, _event(text="no mention anywhere"))


def test_mention_is_read_from_raw_text_not_cleaned_text():
    """clean_slack_text strips <@Uxxx>, so a gate reading cleaned text would
    reject every mention. Guard against that regression."""
    ch = _channel()
    assert ch._is_addressed_to_us(_event(text=f"<@{BOT}> hello"))


def test_missing_channel_type_defaults_to_gated():
    """An event with no channel_type must not be assumed to be a DM."""
    ch = _channel()
    ev = _event(text="hello")
    ev.pop("channel_type")
    assert not ch._is_addressed_to_us(ev)


def test_unknown_own_user_id_does_not_crash():
    """Auth can fail; the gate must degrade rather than throw."""
    ch = _channel()
    ch._own_user_id = None
    assert not ch._is_addressed_to_us(_event(text=f"<@{BOT}> hi"))
    assert ch._is_addressed_to_us(_event(etype="app_mention", text="hi"))


def test_another_users_mention_does_not_trigger_us():
    ch = _channel()
    assert not _passes(ch, _event(text="<@U0OTHERUSER> can you check this?"))
