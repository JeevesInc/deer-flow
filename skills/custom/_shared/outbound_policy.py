"""Outbound policy: nothing leaves Jeeves unless Brian has approved that message.

Rule of record (Brian, 2026-11-02): "at no point in the chain, even if there's a
subscription, should the agent be able to communicate externally even if they
did previously in the chain. each external communication must be explicitly
approved by me. otherwise all external messages are just drafts in my email."

The only way the agent can produce an external message is as a Gmail DRAFT in
Brian's mailbox. Brian approves it by sending it himself. Every agent-side send
path (slack_tool send / send-as-owner / upload, cron emails) calls into this
module and refuses anything that would reach someone outside Jeeves. There is
deliberately no override flag; Brian is the override, by sending the draft.

This module is the code-level layer. The credential-level layer, where the
agent cannot reach any token that is able to send, is the outbound service in
deer-flow/outbound/.
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path

INTERNAL_DOMAINS = {
    d.strip().lower()
    for d in os.environ.get("JEEVES_INTERNAL_DOMAINS", "tryjeeves.com").split(",")
    if d.strip()
}


class ExternalSendBlocked(PermissionError):
    pass


def _log_path() -> Path | None:
    cur = Path(__file__).resolve().parent
    for _ in range(8):
        cand = cur / "backend" / ".deer-flow"
        if cand.is_dir():
            return cand / "outbound_blocked.jsonl"
        if cur.parent == cur:
            break
        cur = cur.parent
    return None


def _record(channel: str, target: str, reason: str, context: str) -> None:
    p = _log_path()
    if not p:
        return
    try:
        with p.open("a", encoding="utf-8") as f:
            f.write(json.dumps({
                "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "channel": channel, "target": target, "reason": reason, "context": context,
            }) + "\n")
    except Exception:
        pass


def _block(channel: str, target: str, reason: str, context: str):
    _record(channel, target, reason, context)
    raise ExternalSendBlocked(
        f"BLOCKED by outbound policy: {channel} to {target} is external ({reason}). "
        "External messages can only be Gmail drafts in Brian's mailbox, and Brian sends them himself. "
        "Create the draft with gmail_tool.py (draft / draft-new), then post the draft link and the "
        "exact text to Brian. There is no override."
    )


def _domain(addr: str) -> str:
    addr = (addr or "").strip().lower()
    if "<" in addr:
        addr = addr.split("<", 1)[1].split(">", 1)[0]
    return addr.rsplit("@", 1)[-1] if "@" in addr else ""


def is_internal_email(addr: str) -> bool:
    return _domain(addr) in INTERNAL_DOMAINS


def assert_internal_emails(addrs, context: str = "") -> None:
    """Raise unless every address is internal. An empty list also raises."""
    flat = []
    for a in addrs or []:
        flat.extend(x.strip() for x in str(a).split(",") if x.strip())
    if not flat:
        _block("email", "(no recipients)", "no recipients to verify", context)
    ext = [a for a in flat if not is_internal_email(a)]
    if ext:
        _block("email", ", ".join(ext), "recipient outside " + "/".join(sorted(INTERNAL_DOMAINS)), context)


_team_cache: dict[int, str] = {}


def _our_team(client) -> str:
    k = id(client)
    if k not in _team_cache:
        _team_cache[k] = client.auth_test().get("team_id", "")
    return _team_cache[k]


def slack_user_is_internal(client, user_id: str) -> tuple[bool, str]:
    try:
        u = client.users_info(user=user_id)["user"]
    except Exception as e:  # fail closed
        return False, f"could not verify user {user_id}: {e}"
    if u.get("is_bot") or user_id == "USLACKBOT":
        return True, "bot"
    if u.get("is_stranger"):
        return False, "Slack Connect user"
    team = u.get("team_id") or ""
    if team and team != _our_team(client):
        return False, f"user from another workspace ({team})"
    email = (u.get("profile") or {}).get("email", "")
    if (u.get("is_restricted") or u.get("is_ultra_restricted")) and not is_internal_email(email):
        return False, "guest account"
    if email and not is_internal_email(email):
        return False, f"email domain {_domain(email)}"
    return True, "internal"


def assert_slack_conversation_internal(client, channel_id: str, context: str = "", user_client=None) -> None:
    """Raise (fail closed) unless every member of the conversation is a Jeeves member.
    `user_client` does users.info lookups (the bot token has no users:read)."""
    uc = user_client or client
    channel_id = (channel_id or "").strip()
    if channel_id.startswith(("U", "W")):
        ok, why = slack_user_is_internal(uc, channel_id)
        if not ok:
            _block("slack", channel_id, why, context)
        return
    try:
        c = client.conversations_info(channel=channel_id)["channel"]
    except Exception as e:
        _block("slack", channel_id, f"could not verify conversation: {e}", context)
        return
    if c.get("is_ext_shared") or c.get("is_pending_ext_shared") or (c.get("is_shared") and not c.get("is_org_shared")):
        _block("slack", channel_id, "Slack Connect / externally shared conversation", context)
    if c.get("is_im"):
        ok, why = slack_user_is_internal(uc, c.get("user", ""))
        if not ok:
            _block("slack", channel_id, why, context)
        return
    if c.get("is_mpim"):
        try:
            members = client.conversations_members(channel=channel_id, limit=200).get("members", [])
        except Exception as e:
            _block("slack", channel_id, f"could not list members: {e}", context)
            return
        for m in members:
            ok, why = slack_user_is_internal(uc, m)
            if not ok:
                _block("slack", channel_id, f"member {m}: {why}", context)
