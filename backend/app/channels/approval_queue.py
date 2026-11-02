"""Approval-queue reaction handling (P3-2 split from slack.py).

The owner's emoji reactions on proposal cards drive execution: 👍/✅ executes
the proposed action, 👎/❌ rejects. All the security-sensitive gating (owner
check, fail-closed without owner id, durable dedup) lives here so it can be
unit-tested without a live SlackChannel; the channel injects two callbacks —
`post_thread` (Slack write) and `dispatch_approval` (message-bus publish).
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Callable

logger = logging.getLogger(__name__)


def build_approval_prompt(proposal: dict) -> str:
    """Agent dispatch text for an approved proposal (pure, testable)."""
    action = (proposal.get("proposed_action") or "").strip()
    lines = [
        "APPROVED ACTION from the analyst approval queue. Carry this out now:",
        "",
        action or "(no concrete action was named — infer the right next step from the context below)",
        "",
        "Context (the inbound email that triggered this):",
        f"- From: {proposal.get('sender_display', '')} <{proposal.get('sender_email', '')}>",
        f"- Subject: {proposal.get('subject', '')}",
        f"- Summary: {proposal.get('summary', '')}",
    ]
    gid = proposal.get("gmail_msg_id")
    if gid:
        lines.append(f"- gmail_msg_id: {gid} (read the full email thread with the Gmail tool before acting)")
    lines.append("")
    lines.append(
        "If this requires sending an external email, create a Gmail DRAFT and post it in this "
        "thread for a final check rather than sending it outright. For reversible/internal steps "
        "(pull data, build a file, internal forward, calendar hold), just do them and report back here."
    )
    return "\n".join(lines)


class ApprovalQueue:
    """Reaction → approve/reject flow for the analyst approval queue.

    Inert unless ANALYST_QUEUE_CHANNEL_ID is set (read per event so it
    hot-follows env changes) and the Slack app has reactions:read + the
    reaction_added event subscription.
    """

    def __init__(self, *, owner_user_id: str, log_dir: Path,
                 post_thread: Callable[[str, str, str], None],
                 dispatch_approval: Callable[[dict, str, str, str], None]) -> None:
        self._owner_user_id = owner_user_id
        self._log_dir = log_dir
        self._post_thread = post_thread
        self._dispatch_approval = dispatch_approval
        # Dedup so an added/removed/re-added emoji only fires once. In-memory
        # only — the durable guard is the proposal_outcomes.jsonl check, so
        # bounding/clearing this set is safe.
        self._handled_reactions: set[str] = set()

    # -- paths ---------------------------------------------------------------

    def _queue_channel(self) -> str:
        return os.environ.get("ANALYST_QUEUE_CHANNEL_ID", "")

    def _proposal_log_path(self) -> Path:
        return self._log_dir / "proposal_log.jsonl"

    # -- event entry point -----------------------------------------------------

    def handle_reaction_event(self, event: dict) -> None:
        """Drive the approval queue from the owner's emoji reactions.

        Only the owner's reactions in the configured queue channel are
        honored; everything else is ignored.
        """
        queue_channel = self._queue_channel()
        if not queue_channel:
            return
        if not self._owner_user_id:
            # Fail CLOSED: with no owner configured, honoring reactions would
            # let ANY workspace user approve agent actions (Chan-M4).
            logger.warning("[Queue] SLACK_OWNER_USER_ID unset — ignoring reaction (fail closed)")
            return
        if event.get("user", "") != self._owner_user_id:
            return
        item = event.get("item", {})
        if item.get("type") != "message":
            return
        channel_id = item.get("channel", "")
        msg_ts = item.get("ts", "")
        if channel_id != queue_channel or not msg_ts:
            return

        # Normalize skin-tone variants like "+1::skin-tone-3".
        reaction = (event.get("reaction") or "").split("::", 1)[0].lower()
        approve = reaction in ("+1", "thumbsup", "white_check_mark", "heavy_check_mark")
        reject = reaction in ("-1", "thumbsdown", "x", "no_entry")
        if not (approve or reject):
            return

        dedup_key = f"{msg_ts}:{'approve' if approve else 'reject'}"
        if dedup_key in self._handled_reactions:
            return

        proposal = self._lookup_proposal(channel_id, msg_ts)
        if not proposal:
            logger.info("[Queue] reaction %s on %s/%s — no matching proposal in log",
                        reaction, channel_id, msg_ts)
            return

        # Durable executed-outcome dedup: the in-memory set dies with the
        # process, so a gateway restart + re-added reaction would re-execute
        # an already-approved action. proposal_outcomes.jsonl is the durable
        # record.
        if self._has_reaction_outcome(proposal.get("slack_ts") or msg_ts):
            logger.info("[Queue] %s already has a recorded reaction outcome — skipping", msg_ts)
            self._handled_reactions.add(dedup_key)
            return

        self._handled_reactions.add(dedup_key)
        if len(self._handled_reactions) > 2000:
            # Bounded memory — safe to drop because the durable check above
            # prevents re-execution.
            self._handled_reactions.clear()
            self._handled_reactions.add(dedup_key)
        if approve:
            self._approve_proposal(proposal, channel_id, msg_ts)
        else:
            self._reject_proposal(proposal, channel_id, msg_ts)

    # -- proposal log / outcomes ------------------------------------------------

    def _lookup_proposal(self, channel_id: str, msg_ts: str) -> dict | None:
        """Find the proposal_log record for a queue card by (channel, ts)."""
        path = self._proposal_log_path()
        if not path.exists():
            return None
        try:
            match = None
            with path.open("r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rec = json.loads(line)
                    except Exception:
                        continue
                    if rec.get("slack_ts") == msg_ts and rec.get("slack_channel") == channel_id:
                        match = rec  # last write wins
            return match
        except Exception as exc:
            logger.warning("[Queue] failed reading proposal log: %s", exc)
            return None

    def _has_reaction_outcome(self, proposal_ts: str) -> bool:
        """True when proposal_outcomes.jsonl already records a reaction outcome
        for this proposal — the durable half of the approval dedup."""
        if not proposal_ts:
            return False
        path = self._log_dir / "proposal_outcomes.jsonl"
        if not path.exists():
            return False
        try:
            with path.open("r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rec = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if (rec.get("proposal_slack_ts") == proposal_ts
                            and rec.get("source") == "reaction"):
                        return True
        except Exception as exc:
            logger.warning("[Queue] failed reading outcomes log: %s", exc)
        return False

    def _record_outcome(self, proposal: dict, outcome: str) -> None:
        """Append a reaction-sourced outcome to proposal_outcomes.jsonl.

        Keyed by proposal_slack_ts so the nightly proposal_learner skips
        re-labeling it from the thread and the signal is retained for synthesis.
        """
        try:
            from datetime import datetime, timezone
            path = self._log_dir / "proposal_outcomes.jsonl"
            path.parent.mkdir(parents=True, exist_ok=True)
            entry = {
                "proposal_slack_ts": proposal.get("slack_ts"),
                "labeled_at": datetime.now(timezone.utc).isoformat(),
                "outcome": outcome,            # 'approved' | 'rejected'
                "source": "reaction",
                "brian_reply": "",
                "brian_reply_ts": "",
                "agent_response": "",
                "lag_seconds": 0,
                "label_reasoning": f"Owner reaction in approval queue ({outcome}).",
            }
            with path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(entry) + "\n")
        except Exception as exc:
            logger.warning("[Queue] failed recording outcome: %s", exc)

    # -- approve / reject --------------------------------------------------------

    def _approve_proposal(self, proposal: dict, channel_id: str, msg_ts: str) -> None:
        logger.info("[Queue] APPROVED ts=%s category=%s", msg_ts, proposal.get("category", ""))
        self._record_outcome(proposal, "approved")
        self._post_thread(channel_id, msg_ts, ":arrow_forward: *Approved* — executing…")
        self._dispatch_approval(proposal, channel_id, msg_ts, build_approval_prompt(proposal))

    def _reject_proposal(self, proposal: dict, channel_id: str, msg_ts: str) -> None:
        logger.info("[Queue] REJECTED ts=%s category=%s", msg_ts, proposal.get("category", ""))
        self._record_outcome(proposal, "rejected")
        self._post_thread(channel_id, msg_ts,
                          ":no_entry_sign: *Rejected* — no action taken. Logged for learning.")
