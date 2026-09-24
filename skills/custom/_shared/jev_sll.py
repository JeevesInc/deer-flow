"""Jev back-ends for two of the SLL's three model calls (2026-09-23).

- sentiment(): 5-way Choice replacing the Opus SENTIMENT_MODEL call.
- rank(): per-lesson Noul relevance over ALL lessons (fan-out, batched),
  replacing the Opus ranker that saw a truncated list and kept returning
  the same 3-4 lessons. Lesson extraction (writes sentences) stays on Opus.

Both raise jev.JevError on failure; callers fall back to the Opus path.
"""

import jev

SENTIMENT_CRITERIA = {
    "explicit_correction": "Brian directly corrects, contradicts, rejects or says the assistant was wrong, "
                           "or tells it to stop / not do something / redo it differently",
    "explicit_praise": "Brian explicitly praises the output (perfect, great, exactly right, nailed it)",
    "implicit_negative": "Brian is mildly dissatisfied without saying it is wrong: a terse disengaged reply "
                         "(ok, k, noted), a bare '?', re-asking the same thing, or impatience",
    "implicit_positive": "Brian builds on the output or proceeds to use it: a follow-up that treats it as valid, "
                         "'send it', 'do X next', approval to continue",
    "no_signal": "Brian changes topic entirely, or the new message is unrelated to the prior turn",
}


def sentiment(task: str, response: str, user_reply: str) -> tuple[str, float]:
    state = {"assistant_prior_task": task[:1500], "assistant_prior_response": response[:1500],
             "brian_new_message": user_reply[:2000]}
    a = jev.choice(state, "How does Brian's new message react to the assistant's prior response?",
                   SENTIMENT_CRITERIA, caller="sll-sentiment")
    return a["choice"], float(a.get("confidence", 0.0))


def rank(task: str, lesson_texts: list[str], top_k: int, min_p: float = 0.5, batch: int = 80) -> list[int]:
    """Indices of up to top_k lessons with P(relevant) >= min_p, best first."""
    scores = []
    for s in range(0, len(lesson_texts), batch):
        qs = {f"l{i}": {"type": "noul",
                        "instructions": "Would an AI assistant about to do this task make a mistake it could avoid, "
                                        "or do better work, by following this lesson? Lesson: " + lesson_texts[i][:300],
                        "criteria": {"true": "The lesson concretely applies to this kind of task, tool, counterparty, "
                                             "document or data", "false": "Only shares words or is about different work"}}
              for i in range(s, min(s + batch, len(lesson_texts)))}
        ans = jev.ask({"task": task[:3000]}, qs, caller="sll-ranker", timeout=30)
        scores += [(float(ans[k]["noul"]), int(k[1:])) for k in qs]
    scores.sort(reverse=True)
    return [i for p, i in scores if p >= min_p][:top_k]
