"""Tests for slack_content.py + approval_queue.py (P3-2 split, 2026-07-02).

The three most incident-prone Slack areas — forwarded-message extraction,
archive-link resolution, and the approval queue — previously lived inside the
1,400-line SlackChannel with no direct tests. Now they're standalone units.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.channels import slack_content as sc
from app.channels.approval_queue import ApprovalQueue, build_approval_prompt


# ---------------------------------------------------------------------------
# clean_slack_text
# ---------------------------------------------------------------------------

def test_clean_unwraps_urls():
    assert sc.clean_slack_text("see <https://example.com>") == "see https://example.com"
    assert sc.clean_slack_text("see <https://example.com|Example>") == "see https://example.com"


def test_clean_preserves_mentions_and_simplifies_channels():
    assert sc.clean_slack_text("hey <@U123>") == "hey <@U123>"
    assert sc.clean_slack_text("in <#C123|general>") == "in #general"


# ---------------------------------------------------------------------------
# Forwarded-content extraction
# ---------------------------------------------------------------------------

def test_forwarded_email_attachment_extracted():
    event = {"attachments": [{
        "author_name": "Jane Doe",
        "title": "Q3 covenant waiver",
        "pretext": "FYI",
        "text": "Please confirm the waiver terms by Friday.",
        "footer": "Sent from Gmail",
    }]}
    out = sc.extract_forwarded_text(event)
    assert "[From Jane Doe]" in out
    assert "Subject: Q3 covenant waiver" in out
    assert "Please confirm the waiver terms by Friday." in out
    assert "— Sent from Gmail" in out


def test_forwarded_falls_back_to_fallback_field():
    event = {"attachments": [{"fallback": "fallback body only"}]}
    assert "fallback body only" in sc.extract_forwarded_text(event)


def test_unfurl_attachments_keep_only_text():
    event = {"attachments": [{"service_name": "YouTube", "title": "cat video",
                              "text": "preview text"}]}
    out = sc.extract_forwarded_text(event)
    assert out == "preview text"          # no Subject/author scaffolding


def test_quoted_blocks_extracted_sections_ignored():
    event = {"blocks": [{
        "type": "rich_text",
        "elements": [
            {"type": "rich_text_section",           # duplicates event["text"]
             "elements": [{"type": "text", "text": "my own words"}]},
            {"type": "rich_text_quote",
             "elements": [{"type": "text", "text": "forwarded: numbers due "},
                          {"type": "link", "url": "https://x.com", "text": "here"},
                          {"type": "user", "user_id": "U9"}]},
        ],
    }]}
    out = sc.extract_quoted_blocks_text(event)
    assert "forwarded: numbers due here<@U9>" == out
    assert "my own words" not in out


def test_attachment_urls_exclude_slack_hosted_files():
    event = {
        "attachments": [{"original_url": "https://doc.example.com/a"}],
        "files": [{"url_private": "https://files.slack.com/x", "permalink_public": None},
                  {"permalink_public": "https://slack-files.com/pub"}],
    }
    assert sc.extract_attachment_urls(event) == [
        "https://doc.example.com/a", "https://slack-files.com/pub"]


# ---------------------------------------------------------------------------
# Archive-link resolution (fake client)
# ---------------------------------------------------------------------------

class FakeSlackClient:
    def __init__(self, text="original message <https://x.com|link>", fail_history=False):
        self.text = text
        self.fail_history = fail_history

    def conversations_history(self, **kw):
        if self.fail_history:
            raise RuntimeError("not_in_channel")
        return {"messages": [{"text": self.text}]}

    def conversations_replies(self, **kw):
        return {"messages": [{"text": self.text}]}


def test_archive_ts_conversion():
    assert sc.archive_ts_to_message_ts("1234567890123456") == "1234567890.123456"
    assert sc.archive_ts_to_message_ts("123") == "123"


def test_resolve_archive_urls_appends_content():
    text = "look at https://acme.slack.com/archives/C042/p1234567890123456"
    out = sc.resolve_slack_archive_urls(FakeSlackClient(), text)
    assert out.startswith(text)
    assert "[Slack message from channel C042]" in out
    assert "original message https://x.com" in out   # cleaned mrkdwn


def test_resolve_falls_back_to_replies_when_history_fails():
    out = sc.resolve_single_slack_url(
        FakeSlackClient(fail_history=True),
        "https://acme.slack.com/archives/C042/p1234567890123456")
    assert out and "original message" in out


def test_resolvers_are_none_safe():
    assert sc.fetch_slack_message(None, "C1", "1.0") is None
    assert sc.fetch_root_message_text(None, "C1", "1.0") is None
    text = "https://acme.slack.com/archives/C042/p1234567890123456"
    assert sc.resolve_slack_archive_urls(None, text) == text  # unchanged


def test_root_message_combines_body_blocks_and_forwarded():
    class Client(FakeSlackClient):
        def conversations_history(self, **kw):
            return {"messages": [{
                "text": "root body",
                "blocks": [{"type": "section", "text": {"type": "mrkdwn", "text": "block text"}}],
                "attachments": [{"title": "Fwd subject", "text": "fwd body"}],
            }]}
    out = sc.fetch_root_message_text(Client(), "C1", "1.0")
    assert "root body" in out
    assert "block text" in out
    assert "Forwarded message:" in out and "fwd body" in out


# ---------------------------------------------------------------------------
# ApprovalQueue as a standalone unit
# ---------------------------------------------------------------------------

def _queue(tmp_path, owner="UOWNER", posts=None, dispatches=None):
    return ApprovalQueue(
        owner_user_id=owner,
        log_dir=tmp_path,
        post_thread=lambda c, t, x: (posts is not None) and posts.append((c, t, x)),
        dispatch_approval=lambda p, c, t, x: (dispatches is not None) and dispatches.append((p, c, t, x)),
    )


def _write_proposal(tmp_path, ts="10.1", channel="CQUEUE", **extra):
    rec = {"slack_ts": ts, "slack_channel": channel,
           "proposed_action": "Reply confirming the amount", **extra}
    with (tmp_path / "proposal_log.jsonl").open("a", encoding="utf-8") as f:
        f.write(json.dumps(rec) + "\n")
    return rec


def _reaction(ts="10.1", user="UOWNER", reaction="+1"):
    return {"type": "reaction_added", "user": user, "reaction": reaction,
            "item": {"type": "message", "channel": "CQUEUE", "ts": ts}}


def test_approve_records_outcome_and_dispatches(tmp_path, monkeypatch):
    monkeypatch.setenv("ANALYST_QUEUE_CHANNEL_ID", "CQUEUE")
    posts, dispatches = [], []
    q = _queue(tmp_path, posts=posts, dispatches=dispatches)
    _write_proposal(tmp_path)

    q.handle_reaction_event(_reaction())

    assert len(dispatches) == 1
    _, _, _, text = dispatches[0]
    assert "APPROVED ACTION" in text
    assert "Reply confirming the amount" in text
    outcomes = (tmp_path / "proposal_outcomes.jsonl").read_text()
    assert '"approved"' in outcomes
    assert any("Approved" in p[2] for p in posts)


def test_reject_records_without_dispatch(tmp_path, monkeypatch):
    monkeypatch.setenv("ANALYST_QUEUE_CHANNEL_ID", "CQUEUE")
    dispatches = []
    q = _queue(tmp_path, dispatches=dispatches)
    _write_proposal(tmp_path)

    q.handle_reaction_event(_reaction(reaction="-1"))

    assert dispatches == []
    assert '"rejected"' in (tmp_path / "proposal_outcomes.jsonl").read_text()


def test_skin_tone_variants_normalized(tmp_path, monkeypatch):
    monkeypatch.setenv("ANALYST_QUEUE_CHANNEL_ID", "CQUEUE")
    dispatches = []
    q = _queue(tmp_path, dispatches=dispatches)
    _write_proposal(tmp_path)
    q.handle_reaction_event(_reaction(reaction="+1::skin-tone-3"))
    assert len(dispatches) == 1


def test_inert_without_queue_channel(tmp_path, monkeypatch):
    monkeypatch.delenv("ANALYST_QUEUE_CHANNEL_ID", raising=False)
    dispatches = []
    q = _queue(tmp_path, dispatches=dispatches)
    _write_proposal(tmp_path)
    q.handle_reaction_event(_reaction())
    assert dispatches == []


def test_approval_prompt_includes_gmail_pointer():
    text = build_approval_prompt({"proposed_action": "Send the tape",
                                  "gmail_msg_id": "abc123"})
    assert "Send the tape" in text
    assert "gmail_msg_id: abc123" in text
    assert "DRAFT" in text  # external sends stay human-gated
