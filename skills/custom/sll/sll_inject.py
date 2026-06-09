#!/usr/bin/env python3
"""SLL lesson injection — retrieve relevant past lessons for a task.

    sll_inject.py --task "<task description>" [--top-k 5]

Prints lesson lines to stdout (one per line). Empty output = no lessons apply.
The agent treats any output here as hard constraints for the turn:
  [!] AVOID ... = mandatory prohibition
  [+] DO    ... = required pattern

Retrieval mechanism: all stored lessons are sent to Haiku with the task,
and Haiku returns ranked indices. This avoids the mem0/Qdrant lock
contention that prevents subprocess access to the shared vector store.
"""

import argparse
import json
import logging
import re
import sys

import storage

logging.basicConfig(
    level=logging.WARNING,
    format="[sll-inject %(levelname)s] %(message)s",
    stream=sys.stderr,
)
log = logging.getLogger("sll_inject")

DEFAULT_TOP_K = 5
# If we have <= this many lessons, skip the ranker entirely and return them all.
RANK_THRESHOLD = 3

RANKER_PROMPT = """You are ranking past lessons by relevance to a new task an AI assistant is about to perform.

## The new task
{task}

## Available lessons (indexed)
{lesson_list}

## Your job
Return the indices of the {top_k} MOST RELEVANT lessons (or fewer if not enough are clearly relevant).

Relevance criteria:
- A lesson is relevant if it directly applies to this kind of task, tool, topic, or data.
- A lesson is NOT relevant just because it shares a word; require concrete overlap of CONTEXT.
- Prefer lessons that warn against a specific failure mode this task could trigger ([!] AVOID).
- Include success patterns ([+] DO) only when the task is the same kind of work they describe.

Output ONLY valid JSON, no preamble, no code fence:
{{"indices": [0, 3, 7]}}

If no lessons are clearly relevant, return:
{{"indices": []}}"""


def normalize_lesson_text(text: str) -> str:
    """Ensure lesson starts with [!] or [+]."""
    t = text.strip()
    if t.startswith("[!]") or t.startswith("[+]"):
        return t
    upper = t.upper()
    if upper.startswith("AVOID"):
        return "[!] " + t
    if upper.startswith("DO ") or upper.startswith("DO\t"):
        return "[+] " + t
    return "[+] " + t


def rank_with_haiku(task: str, lessons: list[dict], top_k: int) -> list[int]:
    """Return list of indices into `lessons`, ordered by Haiku-judged relevance."""
    client = storage.anthropic_client()
    if client is None:
        return list(range(min(top_k, len(lessons))))

    rendered = "\n".join(
        f"[{i}] {storage.truncate(l.get('text', ''), 200)}"
        for i, l in enumerate(lessons)
    )
    prompt = RANKER_PROMPT.format(
        task=storage.truncate(task, 600),
        lesson_list=rendered,
        top_k=top_k,
    )
    try:
        resp = client.messages.create(
            model=storage.RANKER_MODEL,
            max_tokens=200,
            messages=[{"role": "user", "content": prompt}],
        )
        raw = resp.content[0].text.strip()
    except Exception as e:
        log.warning("ranker call failed: %s", e)
        return list(range(min(top_k, len(lessons))))

    # Strip markdown fences if Haiku adds them despite instructions.
    if raw.startswith("```"):
        match = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", raw, re.DOTALL)
        if match:
            raw = match.group(1)
    try:
        data = json.loads(raw)
    except Exception:
        # Fall back to plain index extraction if JSON parsing fails
        nums = re.findall(r"\d+", raw)
        return [int(n) for n in nums if int(n) < len(lessons)][:top_k]

    indices = data.get("indices") or []
    return [i for i in indices if isinstance(i, int) and 0 <= i < len(lessons)][:top_k]


def log_retrieval(task: str, chosen: list[dict]) -> None:
    if not chosen:
        return
    storage.append_jsonl(
        storage.RETRIEVALS_PATH,
        {
            "task": storage.truncate(task, 300),
            "retrieved_at": storage.now_iso(),
            "lessons": [
                {"text": storage.truncate(c.get("text", ""), 200), "id": c.get("id")}
                for c in chosen
            ],
        },
    )
    storage.update_lesson_retrieval({c.get("id") for c in chosen if c.get("id")})


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="SLL lesson injection")
    p.add_argument("--task", required=True, help="Task description to retrieve lessons for")
    p.add_argument("--top-k", type=int, default=DEFAULT_TOP_K)
    args = p.parse_args(argv)

    try:
        task = args.task.strip()
        if not task:
            return 0

        lessons = storage.all_lessons()
        if not lessons:
            return 0

        if len(lessons) <= RANK_THRESHOLD:
            # Small enough that everything is relevant context — return all.
            chosen = lessons
        else:
            indices = rank_with_haiku(task, lessons, args.top_k)
            if not indices:
                return 0
            chosen = [lessons[i] for i in indices]

        for c in chosen:
            print(normalize_lesson_text(c.get("text", "")))

        log_retrieval(task, chosen)
        return 0
    except Exception as e:
        log.error("sll_inject unhandled error: %s", e, exc_info=True)
        return 0


if __name__ == "__main__":
    sys.exit(main())
