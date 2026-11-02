"""Owner-only capability gate for tools invoked inside an agent run.

Background (2026-08-28). Inbound Slack DMs are allowlisted in
``app/channels/slack.py``, but that allowlist is binary: once someone was on
it they got the SAME agent as Brian -- his inbox, his calendar, his memory,
his dossiers, and the ability to make the bot message third parties. Brian's
rule: allowlisted or not, nobody except Brian gets owner-gated capabilities.
Other users may query data, ask questions and work with files.

Mechanism
---------
``ChannelManager._write_actor_marker`` writes ``.actor.json`` into the
per-thread sandbox workspace of the LangGraph thread that is about to run
(``.deer-flow`` -> ``threads`` -> ``<thread_id>`` -> ``user-data`` ->
``workspace`` -> ``.actor.json``).

Inside an agent run the sandbox workspace path resolves to exactly that
directory, so a tool launched by the agent can read the marker and learn who
it is acting for. Crons and CLI runs execute outside any sandbox, so the
marker is absent -- they keep working unchanged (fail-open ONLY when no marker
exists; a marker naming a non-owner always denies).

Usage in a tool::

    import os, sys
    sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))), '_shared'))
    from actor_guard import require_owner
    require_owner("Gmail / Brian's inbox")
"""

from __future__ import annotations

import json
import os
import sys

_MARKER_NAME = ".actor.json"
_SANDBOX_WS = "/mnt" + "/user-data" + "/workspace"

_MARKER_CANDIDATES = [os.path.join(_SANDBOX_WS, _MARKER_NAME)]
_ws = os.environ.get("WORKSPACE_PATH")
if _ws and os.path.join(_ws, _MARKER_NAME) not in _MARKER_CANDIDATES:
    _MARKER_CANDIDATES.append(os.path.join(_ws, _MARKER_NAME))

# Escape hatch for gateway crons that legitimately act as the owner.
_OWNER_OVERRIDE = "DEERFLOW_ACTOR_OVERRIDE"


def actor():
    """Return the actor record for this run, or None outside a sandbox."""
    if (os.environ.get(_OWNER_OVERRIDE) or "").strip().lower() == "owner":
        return {"user_id": "owner-override", "name": "owner (env override)",
                "is_owner": True, "source": _OWNER_OVERRIDE}
    for path in _MARKER_CANDIDATES:
        try:
            with open(path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            if isinstance(data, dict) and "is_owner" in data:
                return data
        except FileNotFoundError:
            continue
        except Exception:
            # A corrupt marker must not silently grant access.
            return {"user_id": "?", "name": "unreadable actor marker",
                    "is_owner": False, "source": path}
    return None


def is_owner():
    """True when this run is Brian's (or has no actor context at all)."""
    rec = actor()
    return True if rec is None else bool(rec.get("is_owner"))


def describe_actor():
    rec = actor()
    if rec is None:
        return "no actor context (cron/CLI)"
    return "{0} ({1}){2}".format(
        rec.get("name") or "?", rec.get("user_id") or "?",
        "" if rec.get("is_owner") else " [non-owner]")


def require_owner(capability, quiet=False):
    """Exit 2 unless the current run is acting for Brian."""
    rec = actor()
    if rec is None or rec.get("is_owner"):
        return
    who = rec.get("name") or rec.get("user_id") or "this user"
    if not quiet:
        sys.stderr.write(
            "ACCESS DENIED - {cap} is owner-only.\n"
            "This conversation is with {who}, not Brian Mauck. Tools that read "
            "Brian's inbox, calendar, memory or dossiers, or that send messages "
            "on his behalf, are blocked for every non-owner including "
            "allowlisted users.\nTell the requester you cannot help with that "
            "one and do not attempt another route to the same data.\n".format(
                cap=capability, who=who))
    sys.exit(2)
