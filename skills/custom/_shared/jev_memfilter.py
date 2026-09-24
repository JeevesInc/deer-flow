"""Jev relevance filter for mem0 injection (2026-09-23).

The vector gate (mem0_min_injection_score) passes anything semantically near
the query — e.g. a Jev question pulled in CBIZ-audit and $15M-pagare facts.
This asks Jev, for every candidate in ONE fan-out request, whether the memory
would actually help answer the message, and keeps P >= min_p.

filter_memories() never raises: on any failure it returns the input unchanged.
"""

MIN_P = 0.5


def filter_memories(query: str, memories: list[dict], min_p: float = MIN_P,
                    kind: str = "fact", caller: str = "mem0-filter") -> list[dict]:
    if not memories or not query:
        return memories
    try:
        import jev
        texts = [(m.get("memory", "") or m.get("text", "") or "").strip() for m in memories]
        noun = "lesson" if kind == "lesson" else "remembered fact"
        qs = {f"m{i}": {"type": "noul",
                        "instructions": f"Would this {noun} help an assistant respond correctly to the user's "
                                        f"message? {noun.capitalize()}: {t[:400]}",
                        "criteria": {"true": "It is about the same counterparty, system, workflow, document or "
                                             "decision the message is about, or it changes how the reply should be done",
                                     "false": "It is about a different topic and merely shares words or a company name"}}
              for i, t in enumerate(texts) if t}
        if not qs:
            return memories
        ans = jev.ask({"user_message": query[-4000:]}, qs, caller=caller, timeout=8, retries=1)
        scored = sorted(((float(ans[k]["noul"]), int(k[1:])) for k in qs), reverse=True)
        return [dict(memories[i], jev_p=p) for p, i in scored if p >= min_p]
    except Exception:
        return memories
