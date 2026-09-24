#!/usr/bin/env python3
"""LLM-as-judge for proposed_action quality.

The actionable/not-actionable label is scored arithmetically — it is a boolean
against ground truth and needs no model. What arithmetic cannot score is the
part that actually determines whether a proposal is useful: given that the email
WAS worth surfacing, was the proposed next step the right one?

Grounding is what keeps this from being a vibe check. The judge never scores a
proposed action in the abstract; it scores it against what Brian actually did —
his reply text where we have one, the Gmail disposition where we do not. A judge
with no ground truth to compare against would just reward confident phrasing.

Scoring is deliberately coarse (0/1/2). Finer scales invite the judge to split
hairs it cannot actually resolve, and the resulting numbers look precise while
moving on noise.

    2  right next step — matches what Brian wanted, and the agent can execute it
    1  defensible but off — right topic, wrong scope, or needs Brian personally
    0  wrong — misreads the ask, or restates the email back at Brian
"""

from __future__ import annotations

import json
import logging
import os
import re

log = logging.getLogger("eval_judge")

JUDGE_MODEL = os.environ.get("EVAL_JUDGE_MODEL", "claude-opus-5-5")
MAX_SCORE = 2


def _text_from_response(resp) -> str:
    """Concatenate text blocks. Adaptive thinking is always on for the Claude 5
    models, so content[0] is a ThinkingBlock with no .text — indexing it is the
    bug that silently zeroed the proposal synthesizer for weeks."""
    return "".join(
        getattr(b, "text", "") or "" for b in resp.content if getattr(b, "type", None) == "text"
    ).strip()


def _parse_json(text: str) -> dict | None:
    s = text.strip()
    if s.startswith("```"):
        s = re.sub(r"^```[a-zA-Z]*\n?", "", s)
        s = re.sub(r"\n?```$", "", s).strip()
    try:
        return json.loads(s)
    except ValueError:
        m = re.search(r"\{.*\}", s, re.DOTALL)
        if m:
            try:
                return json.loads(m.group(0))
            except ValueError:
                return None
    return None


def judge_proposed_action(case: dict, classification: dict, client=None) -> dict:
    """Score one proposed action against what actually happened.

    Returns {"score": int|None, "reasoning": str}. score is None when the case
    carries no usable ground truth — scoring it anyway would manufacture a
    number out of nothing.
    """
    proposed = (classification.get("proposed_action") or "").strip()
    owner_reply = (case.get("owner_reply") or "").strip()
    disposition = case.get("gmail_disposition", "")

    if not proposed:
        return {"score": None, "reasoning": "No proposed_action to judge."}
    # A bare "owner replied in Gmail" tells us the email mattered but not what
    # the right action was, so there is nothing to grade the action against.
    if not owner_reply and disposition not in ("owner_replied", "thread_progressed"):
        return {"score": None, "reasoning": "No ground truth about what Brian wanted."}

    if client is None:
        from anthropic import Anthropic

        api_key = os.environ.get("ANTHROPIC_API_KEY", "")
        if not api_key:
            return {"score": None, "reasoning": "No ANTHROPIC_API_KEY."}
        client = Anthropic(api_key=api_key)

    inputs = case.get("inputs", {})
    ground_truth = (
        f"Brian's actual reply in the Slack thread:\n{owner_reply[:800]}"
        if owner_reply
        else f"Brian did not reply in Slack, but Gmail shows: {disposition} "
             f"({case.get('label_reasoning', '')})"
    )

    prompt = f"""You are grading ONE proposed next step that an email classifier
generated for Brian Mauck, CFO-org lead at Jeeves. Grade it against what Brian
actually did — not against your own opinion of what a good action would be.

## The email
From: {inputs.get('sender', '')}
To: {inputs.get('to', '')}
Cc: {inputs.get('cc', '')}
Subject: {inputs.get('subject', '')}
Body:
{(inputs.get('body') or '')[:2500]}

## What the classifier proposed
{proposed}

## What actually happened
{ground_truth}

---

Score the proposed action:
  2 — Right next step. It matches what Brian actually wanted AND the agent could
      execute it itself (draft a reply, pull data, prep a brief, search history).
  1 — Defensible but off. Right topic, but wrong scope, wrong recipient, over- or
      under-reaching, or it requires Brian personally rather than the agent.
  0 — Wrong. Misreads what is being asked, targets the wrong person or deal, or
      just restates the email's ask back at Brian instead of proposing work.

Judge the ACTION, not the summary or the priority. If Brian's reply redirected
the agent, that is evidence the proposed action was NOT what he wanted — score 1
or 0 depending on how far off it was.

Respond with ONLY valid JSON:
{{"score": 0 | 1 | 2, "reasoning": "one sentence"}}"""

    try:
        resp = client.messages.create(
            model=JUDGE_MODEL,
            max_tokens=500,
            messages=[{"role": "user", "content": prompt}],
        )
        data = _parse_json(_text_from_response(resp))
        if not data or "score" not in data:
            return {"score": None, "reasoning": "Judge returned unparseable output."}
        score = data["score"]
        if not isinstance(score, int) or not 0 <= score <= MAX_SCORE:
            return {"score": None, "reasoning": f"Judge returned out-of-range score {score!r}."}
        return {"score": score, "reasoning": str(data.get("reasoning", ""))[:300]}
    except Exception as e:
        log.warning("judge call failed for %s: %s", case.get("case_id"), e)
        return {"score": None, "reasoning": f"Judge error: {e}"}
