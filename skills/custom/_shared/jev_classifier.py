"""Jev pre-gate for the inbound-email classifier (webhook_receiver.classify_with_llm).

Jev answers ONE question cheaply: could this email plausibly need Brian or the
agent to act? Below JEV_GATE_THRESHOLD the Opus call is skipped and the email is
logged non-actionable. At/above it, Opus runs exactly as before (it still writes
summary / proposed_action, which Jev cannot).

The threshold is chosen for RECALL, not accuracy: the gate may only remove
emails Opus would also have dropped. Tuned on jeeves-eval golden set 2026-09-23.
Any JevError -> gate open (Opus runs) — Jev never drops mail on its own failure.
"""

import os
import re

GATE_THRESHOLD = float(os.environ.get("JEV_GATE_THRESHOLD", "0.50"))

INSTRUCTIONS = (
    "The recipient is Brian Mauck, Head of Capital Markets at Jeeves (a fintech lender). "
    "Does this message need Brian (or an assistant acting for him) to do something: reply, "
    "send a document or data, make a decision, review/approve, sign, or follow up?"
)
CRITERIA_TRUE = (
    "A lender, investor, bank, trustee, counsel, auditor or counterparty asks Brian "
    "(or a group including him) for information, documents, a confirmation, a decision or a call; "
    "a Jeeves executive or colleague asks Brian for something; a deliverable Brian owes is "
    "requested or chased; a counterparty sends materials that expect Brian's review or reply."
)
CRITERIA_FALSE = (
    "Automated notifications, sign-in links, newsletters, marketing, calendar/Zoom invites, "
    "meeting-notes digests, payment confirmations, daily bank-balance or servicer/trustee reports "
    "sent for information, broadcast minutes, or counsel-to-counsel traffic where Jeeves is only copied."
)


def build_state(sender: str, subject: str, body: str, to: str = "", cc: str = "") -> dict:
    body = re.sub(r"\s+", " ", body or "")[:6000]
    return {"from": sender, "to": to, "cc": cc[:600], "subject": subject, "body": body}


def p_actionable(sender, subject, body, to="", cc="", caller="email-gate") -> float:
    import jev  # _shared on sys.path in webhook_receiver and run_eval
    q = {"actionable": {"type": "noul", "instructions": INSTRUCTIONS, "criteria": {"true": CRITERIA_TRUE, "false": CRITERIA_FALSE}}}
    return float(jev.ask(build_state(sender, subject, body, to, cc), q, caller=caller)["actionable"]["noul"])
