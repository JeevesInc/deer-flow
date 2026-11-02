"""Tests for holding-reply suppression in proposal drafts.

Brian, 2026-08-27, on two proposal cards:

    "this is only helpful if you already have the answer when you draft the response"
    "useless. I don't need a proposal to send an email that says I'll get the info you need."

Both cards carried a correctly-shaped proposed_action ("pull X and reply with
it") but a draft written WITHOUT doing the pull — so the draft could only
promise follow-up and leave [confirm ...] placeholders where the facts belonged.
Approving one sends a promise and triggers no work: the thread looks handled
while nothing has happened.

The drafter is now told to return NONE in that situation, and `is_holding_reply`
is the code-level backstop — earlier the same day, a prompt-only rule against
no-op actions was defeated by the model rewording the action while keeping the
behavior. A rule the model can talk itself out of is not a control.

The two verbatim drafts Brian rejected are pinned below as regression cases.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

BACKEND_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND_DIR))

from webhook_receiver import is_holding_reply  # noqa: E402

# -- the exact drafts that were rejected -------------------------------------

BTG_DRAFT = (
    "Brandon — pulling the BTG escrow details now. It's the account titled "
    "[confirm titular — Jeeves Brasil vs. Jeeves Holding], BTG Pactual, agência "
    "[confirm agência] / conta [confirm conta], currently under escrow with CELF 30-I LLC "
    "and moving to NB.\n\n"
    "Let me confirm against the CIM escrow docs before we send anything to Maria Clara — "
    "I'll come back today with the verified numbers so PN can drop them into the termo de liberação."
)

TRUSTEE_DRAFT = (
    "Fees look fine to me — structuring and monthly admin are immaterial at this size.\n\n"
    "On the transfers, the only line I'd push on is the tiers. Let me pull our recent "
    "Colombia/Cobre outbound volumes so we're arguing from actual counts rather than a guess — "
    "I'll get you monthly restitution and USD transfer numbers by [date]."
)


def test_btg_draft_is_suppressed():
    assert is_holding_reply(BTG_DRAFT) is True


def test_trustee_draft_is_suppressed():
    assert is_holding_reply(TRUSTEE_DRAFT) is True


# -- the shapes that must still get through ----------------------------------


def test_substantive_reply_with_real_numbers_passes():
    """The whole point is to keep real answers, not to suppress drafts."""
    text = (
        "Ag. 0030, Conta 3526343-30, titular Jeeves Brasil Instituição de Pagamento. "
        "That's the CELF 30-I escrow moving to NB — PN can put it straight into the "
        "termo de liberação."
    )
    assert is_holding_reply(text) is False


def test_direct_answer_with_a_decision_passes():
    text = (
        "10 COP restitutions a month is too tight — we averaged 31 in July and 27 in August. "
        "Push for 30 included, and the 2 non-COP tier should go to 5. The overage rates are "
        "trivial either way, so this is about not renegotiating in six months."
    )
    assert is_holding_reply(text) is False


def test_confirmation_without_a_promise_passes():
    assert is_holding_reply("Confirmed for 3:30pm ET Thursday. Dial-in works on my end.") is False


def test_declining_is_not_a_holding_reply():
    assert is_holding_reply("We're not going to pursue that structure — Brandon owns it now.") is False


def test_empty_is_not_a_holding_reply():
    assert is_holding_reply("") is False


# -- individual markers ------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "Let me pull the tape and revert.",
        "I'll confirm with Goodwin and come back to you.",
        "Pulling the numbers now.",
        "I'll get you the file shortly.",
        "Will follow up with the detail today.",
        "Once I have the figures I'll send them over.",
        "I'll circle back on this.",
    ],
)
def test_promise_phrasings_are_caught(text):
    assert is_holding_reply(text) is True


@pytest.mark.parametrize(
    "text",
    [
        "The balance is [confirm amount] as of Friday.",
        "Account [insert account number] at BTG.",
        "Due on [date].",
        "Titular is [TBD].",
    ],
)
def test_material_placeholders_are_caught(text):
    assert is_holding_reply(text) is True


def test_bracketed_prose_that_is_not_a_placeholder_passes():
    """Brackets alone must not condemn a draft — only brackets standing in for
    a fact Brian would have to fill in."""
    assert is_holding_reply("Per the credit agreement [Section 7.01(a)], liquidity is tested monthly.") is False
