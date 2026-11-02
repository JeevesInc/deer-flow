"""Classifier addressee context (2026-07-21).

The framework prompt tells the model to run an addressee check against the
To: field, but classify_with_llm never received To/Cc — the model guessed
the addressee from the greeting line and misattributed asks (flagged an
email addressed to Aaron as "Alex asking Brian for details"). These tests
pin the To/Cc headers flowing from the Gmail handler into the prompt.
"""
from __future__ import annotations

import pytest

import webhook_receiver as wr


class _FakeMessages:
    def __init__(self, captured):
        self._captured = captured

    def create(self, **kwargs):
        self._captured.update(kwargs)
        raise RuntimeError("stop after prompt capture")


class _FakeAnthropic:
    last_call: dict = {}

    def __init__(self, api_key=None):
        _FakeAnthropic.last_call = {}
        self.messages = _FakeMessages(_FakeAnthropic.last_call)


@pytest.fixture()
def captured_prompt(monkeypatch):
    monkeypatch.setattr(wr, "Anthropic", _FakeAnthropic)
    monkeypatch.setattr(wr, "ANTHROPIC_API_KEY", "test-key")
    monkeypatch.setattr(wr, "_get_proposal_patterns", lambda *a, **k: [])

    def _run(**kwargs):
        wr.classify_with_llm(
            source="gmail",
            sender="Alex Melikian <alex@tryjeeves.com>",
            subject="Q2 numbers",
            body_preview="Hi Aaron, can you send over the details?",
            **kwargs,
        )
        return _FakeAnthropic.last_call["messages"][0]["content"]

    return _run


def test_to_and_cc_rendered_in_prompt(captured_prompt):
    prompt = captured_prompt(
        to="Aaron Smith <aaron@tryjeeves.com>",
        cc="Brian Mauck <brian.mauck@tryjeeves.com>",
    )
    assert "To: Aaron Smith <aaron@tryjeeves.com>" in prompt
    assert "Cc: Brian Mauck <brian.mauck@tryjeeves.com>" in prompt


def test_missing_to_cc_leaves_no_empty_lines(captured_prompt):
    prompt = captured_prompt()
    assert "To: " not in prompt.split("## Message to classify")[1]
    assert "Cc: " not in prompt.split("## Message to classify")[1]
    assert "Sender: Alex Melikian <alex@tryjeeves.com>\nSubject: Q2 numbers" in prompt


def test_gmail_handler_passes_to_and_cc(monkeypatch):
    captured = {}

    def fake_classify(**kwargs):
        captured.update(kwargs)
        return {"actionable": False, "reasoning": "test"}

    monkeypatch.setattr(wr, "classify_with_llm", fake_classify)
    monkeypatch.setattr(wr, "_extract_gmail_body", lambda full: "body text")

    msg = {
        "meta": {
            "snippet": "Hi Aaron, can you send over the details?",
            "payload": {
                "headers": [
                    {"name": "From", "value": "Alex Melikian <alex@tryjeeves.com>"},
                    {"name": "To", "value": "Aaron Smith <aaron@tryjeeves.com>"},
                    {"name": "Cc", "value": "Brian Mauck <brian.mauck@tryjeeves.com>"},
                    {"name": "Subject", "value": "Q2 numbers"},
                    {"name": "Date", "value": "Mon, 21 Jul 2026 09:00:00 -0400"},
                ]
            },
        },
        "full": {},
    }
    wr._handle_gmail_message(msg)
    assert captured["to"] == "Aaron Smith <aaron@tryjeeves.com>"
    assert captured["cc"] == "Brian Mauck <brian.mauck@tryjeeves.com>"
