#!/usr/bin/env python3
"""SLL scoring — two modes.

End-of-turn (after substantive output):
    sll_score.py --task "<what brian asked>" --response "<1-2 sentence summary>" [--verbose]
    Writes a pending entry with default composite 0.6 and a candidate lesson.

Start-of-turn (when a new user reply arrives and a pending entry exists):
    sll_score.py --apply-sentiment --user-reply "<brian's new message>" [--verbose]
    Classifies sentiment, applies to pending composite, stores lesson if threshold hit.

Both modes are best-effort and exit 0 on any error so the agent's bash loop is
never blocked.
"""

import argparse
import json
import logging
import sys
from typing import Any

import storage

logging.basicConfig(
    level=logging.WARNING,
    format="[sll-score %(levelname)s] %(message)s",
    stream=sys.stderr,
)
log = logging.getLogger("sll_score")

DEFAULT_COMPOSITE = 0.6

# Sensitivity tuning (2026-06-09):
# - implicit_positive boost 0.15 -> 0.25: building-on-output now reaches success threshold
# - implicit_negative penalty 0.35 -> 0.40: dismissive replies sting appropriately
# - SUCCESS_THRESHOLD 0.80 -> 0.72: good work gets reinforced, not just great work
# - FAILURE_THRESHOLD 0.40 -> 0.45: borderline failures register as learning events
SUCCESS_THRESHOLD = 0.72
FAILURE_THRESHOLD = 0.45

SENTIMENT_RULES = {
    "explicit_correction": ("override", 0.1),
    "explicit_praise": ("override", 0.95),
    "implicit_negative": ("delta", -0.40),
    "implicit_positive": ("delta", 0.25),
    "no_signal": ("delta", 0.0),
}

LESSON_PROMPT = """You are extracting a single, actionable lesson from one turn of an AI assistant's behavior.

## Context
- The user is Brian Mauck, a finance professional.
- The assistant is a Slack bot ("the analyst") that helps with Redshift queries, deal analysis, and ops.
- This is for a self-improvement memory: the assistant should AVOID failures and REPEAT successes.

## The turn
TASK FROM BRIAN: {task}
ASSISTANT'S RESPONSE: {response}
OUTCOME: {outcome}

## Your job
Write ONE concise lesson (one sentence, max ~30 words) that captures what to do differently next time.
- For OUTCOME=failure: format as "AVOID <specific behavior>" — what to stop doing.
- For OUTCOME=success: format as "DO <specific behavior>" — what to repeat.
- Bind the lesson to a CONCRETE trigger (a tool, a topic, a phrase, a data type). Generic advice ("be careful", "think harder") is useless.
- Do NOT mention this specific turn; the lesson must apply to FUTURE turns.

BAD lessons:
- "Be more careful with calculations." (generic)
- "Brian wanted X this time." (one-off, not a pattern)
- "Listen to user feedback." (vacuous)

GOOD lessons:
- "AVOID using today's date for borrowing base queries — Redshift data has a 1-day lag."
- "DO use os.environ.get('OUTPUTS_PATH') in bash subprocess scripts instead of hardcoded /mnt paths."
- "AVOID summarizing tool output verbatim when the user only asked for the answer."

Respond with ONLY the lesson text. No preamble, no quotes, no markdown."""

SENTIMENT_PROMPT = """Classify the user's reply to the assistant's last output into ONE of these categories:

  explicit_correction  — user directly corrects, contradicts, or says the assistant was wrong
                          ("no that's wrong", "you misread", "actually it should be", "stop", "don't")
  explicit_praise      — user explicitly praises the output
                          ("perfect", "great", "exactly right", "nailed it", "thanks, this is good")
  implicit_negative    — minimal/dismissive reply with no engagement
                          ("ok", "got it", "k", "noted", "alright" — short and disengaged)
  implicit_positive    — user builds on the output, asks a follow-up that takes it as valid,
                          or proceeds to use the result
                          ("ok now do X with that", "send it", "schedule it", "great, also ...")
  no_signal            — user changes topic entirely or message is unrelated to the prior turn

## The assistant's prior task
{task}

## The assistant's prior response (summary)
{response}

## Brian's new message
{user_reply}

Respond with ONLY one of: explicit_correction, explicit_praise, implicit_negative, implicit_positive, no_signal
No punctuation, no explanation."""


def call_llm(client: Any, model: str, prompt: str, max_tokens: int = 200) -> str | None:
    if client is None:
        return None
    try:
        resp = client.messages.create(
            model=model,
            max_tokens=max_tokens,
            messages=[{"role": "user", "content": prompt}],
        )
        return resp.content[0].text.strip()
    except Exception as e:
        log.warning("LLM call failed: %s", e)
        return None


def extract_lesson(task: str, response: str, outcome: str) -> str | None:
    client = storage.anthropic_client()
    prompt = LESSON_PROMPT.format(
        task=storage.truncate(task, 600),
        response=storage.truncate(response, 600),
        outcome=outcome,
    )
    raw = call_llm(client, storage.LESSON_MODEL, prompt, max_tokens=120)
    if not raw:
        return None
    raw = raw.strip().strip('"').strip("'")
    if not raw:
        return None
    upper = raw.upper()
    if outcome == "failure":
        if not upper.startswith("AVOID"):
            raw = "AVOID " + raw
        return "[!] " + raw
    if not upper.startswith("DO"):
        raw = "DO " + raw
    return "[+] " + raw


def classify_sentiment(task: str, response: str, user_reply: str) -> str:
    client = storage.anthropic_client()
    prompt = SENTIMENT_PROMPT.format(
        task=storage.truncate(task, 400),
        response=storage.truncate(response, 400),
        user_reply=storage.truncate(user_reply, 600),
    )
    raw = call_llm(client, storage.SENTIMENT_MODEL, prompt, max_tokens=20)
    if not raw:
        return "no_signal"
    label = raw.strip().lower().split()[0] if raw.strip() else "no_signal"
    label = label.strip(".,!?:")
    if label not in SENTIMENT_RULES:
        return "no_signal"
    return label


def store_lesson(
    text: str,
    outcome: str,
    composite: float,
    boost: float,
    source_task: str,
) -> str | None:
    """Append lesson to lessons.jsonl. Returns the assigned id."""
    return storage.add_lesson({
        "text": text,
        "outcome": outcome,
        "composite": composite,
        "boost": boost,
        "source_task": storage.truncate(source_task, 300),
        "created_at": storage.now_iso(),
        "retrieval_count": 0,
        "last_retrieved_at": None,
    })


def mode_end_of_turn(args: argparse.Namespace) -> int:
    """End-of-turn: write pending entry with default composite and candidate lesson."""
    task = args.task or ""
    response = args.response or ""
    if not task.strip() and not response.strip():
        if args.verbose:
            print("[sll] skipped — empty task and response")
        return 0

    # Candidate lessons for both directions; selection happens at sentiment apply.
    failure_lesson = extract_lesson(task, response, "failure")
    success_lesson = extract_lesson(task, response, "success")

    pending = {
        "task": task,
        "response": response,
        "composite": DEFAULT_COMPOSITE,
        "candidate_failure_lesson": failure_lesson,
        "candidate_success_lesson": success_lesson,
        "scored_at": storage.now_iso(),
    }
    storage.write_pending(pending)

    if args.verbose:
        print(f"[sll] turn scored — composite={DEFAULT_COMPOSITE} (pending sentiment)")
        if failure_lesson:
            print(f"[sll] candidate failure lesson: {storage.truncate(failure_lesson, 120)}")
        if success_lesson:
            print(f"[sll] candidate success lesson: {storage.truncate(success_lesson, 120)}")
    return 0


def mode_apply_sentiment(args: argparse.Namespace) -> int:
    """Start-of-turn: apply sentiment from user reply to the pending entry."""
    pending = storage.read_pending()
    if pending is None:
        if args.verbose:
            print("[sll] no pending turn — nothing to apply")
        return 0

    user_reply = args.user_reply or ""
    sentiment = classify_sentiment(
        pending.get("task", ""),
        pending.get("response", ""),
        user_reply,
    )
    mode, value = SENTIMENT_RULES[sentiment]
    initial = float(pending.get("composite", DEFAULT_COMPOSITE))
    if mode == "override":
        final = value
    else:
        final = max(0.0, min(1.0, initial + value))

    # Determine outcome and lesson selection
    outcome = "neutral"
    boost = 0.0
    lesson = None
    lesson_id = None
    if final < FAILURE_THRESHOLD:
        outcome = "failure"
        boost = 2.5
        lesson = pending.get("candidate_failure_lesson")
    elif final >= SUCCESS_THRESHOLD:
        outcome = "success"
        boost = 2.0
        lesson = pending.get("candidate_success_lesson")

    if lesson:
        lesson_id = store_lesson(
            text=lesson,
            outcome=outcome,
            composite=final,
            boost=boost,
            source_task=pending.get("task", ""),
        )

    log_entry = {
        "scored_at": pending.get("scored_at"),
        "applied_at": storage.now_iso(),
        "task": storage.truncate(pending.get("task", ""), 300),
        "response_summary": storage.truncate(pending.get("response", ""), 300),
        "user_reply": storage.truncate(user_reply, 300),
        "sentiment": sentiment,
        "initial_composite": initial,
        "final_composite": final,
        "outcome": outcome,
        "lesson_stored": lesson if lesson and outcome != "neutral" else None,
        "lesson_id": lesson_id,
    }
    storage.append_jsonl(storage.LOG_PATH, log_entry)
    storage.clear_pending()

    if args.verbose:
        print(f"[sll] sentiment={sentiment} initial={initial:.2f} final={final:.2f} outcome={outcome}")
        if lesson and outcome != "neutral":
            print(f"[sll] stored lesson (boost={boost}): {storage.truncate(lesson, 140)}")
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="SLL turn scoring")
    p.add_argument("--apply-sentiment", action="store_true")
    p.add_argument("--user-reply", default="")
    p.add_argument("--task", default="")
    p.add_argument("--response", default="")
    p.add_argument("--verbose", action="store_true")
    args = p.parse_args(argv)
    try:
        if args.apply_sentiment:
            return mode_apply_sentiment(args)
        return mode_end_of_turn(args)
    except Exception as e:
        # Never fail — log and exit 0 so agent bash loop continues.
        log.error("sll_score unhandled error: %s", e, exc_info=True)
        return 0


if __name__ == "__main__":
    sys.exit(main())
