#!/usr/bin/env python3
"""Triage backed-up proposal patterns into KEEP (restore) and DROP (stay purged).

Context
-------
The 2026-08-26 purge removed all 247 patterns because a large share were derived
from the `ignored` labeling bug — the synthesizer treated "Brian never replied in
Slack" as "the classifier was wrong", which was false 445 times out of 470.

But that purge over-corrected. Many of the same patterns encoded genuinely
correct rules learned from real rejections and from the nature of the mail
itself, and dropping those cost precision immediately: within an hour the
classifier proposed drafting an acknowledgment for a Monex proof-of-payment
comprobante — a case a deleted pattern named by sender.

The distinction that matters is the pattern's EVIDENCE, not its conclusion:

  DROP  Its only support is that Brian did not reply. That signal is now known
        to be unreliable, so the pattern is unfounded even when it happens to
        be right. Tell: "goes unanswered", "are ignored", "never gets a reply".

  KEEP  It states a checkable property of the message — it is a receipt or
        confirmation, it is addressed to someone else, it is outbound counsel
        work, the ask is already answered in-thread, the work belongs to another
        team. These stand on their own regardless of whether Brian replied.

A keyword pass is not good enough here: "requests ... do not get an emailed
model or response from Brian" is silence-inferred but matches no obvious
keyword, and restoring a poisoned pattern re-breaks recall. So an LLM judges
each one against the question above.

Usage:
    python triage_proposal_patterns.py <backup.json>              # triage only
    python triage_proposal_patterns.py <backup.json> --restore    # + write KEEPs to mem0
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

_HERE = Path(__file__).resolve()
_BACKEND = _HERE.parents[1]
_DEER = _BACKEND / ".deer-flow"

for _p in (_BACKEND / "packages" / "harness", _BACKEND.parent / "skills" / "custom" / "_shared"):
    if _p.exists() and str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

try:
    from env_loader import load_env

    load_env()
except Exception:
    pass

import os  # noqa: E402

NAMESPACE = "proposal-patterns"
MODEL = "claude-opus-5"
BATCH = 10


def _text_from_response(resp) -> str:
    return "".join(
        getattr(b, "text", "") or "" for b in resp.content if getattr(b, "type", None) == "text"
    ).strip()


def _parse(text: str):
    s = text.strip()
    if s.startswith("```"):
        s = re.sub(r"^```[a-zA-Z]*\n?", "", s)
        s = re.sub(r"\n?```$", "", s).strip()
    try:
        return json.loads(s)
    except ValueError:
        m = re.search(r"\{.*\}", s, re.DOTALL)
        return json.loads(m.group(0)) if m else None


def triage_batch(client, batch: list[str], offset: int) -> list[dict]:
    listing = "\n".join(f"{i + offset}. {t}" for i, t in enumerate(batch))
    prompt = f"""You are auditing learned rules used by an email classifier for Brian Mauck,
who runs Capital Markets at Jeeves.

Each rule was synthesized from labeled feedback. Some were derived from a BROKEN
signal: the system wrongly recorded "Brian never replied" for hundreds of emails
he actually answered in Gmail. Any rule whose only support is Brian's silence is
UNFOUNDED and must be dropped, even if it sounds plausible.

For each rule below decide:

  "DROP" — its evidence is that Brian did not reply / did not respond / ignored
           it / it went unanswered / it never got an email back. The rule infers
           "not actionable" FROM SILENCE.

  "KEEP" — it states a checkable property of the message itself, independent of
           whether Brian replied. For example: the message is a receipt,
           confirmation, comprobante or proof-of-payment; it is addressed to
           someone else with Brian merely copied; it is Jeeves' own counsel
           sending work outbound; the ask was already answered in-thread; the
           work is owned by another named team or person; it is an automated or
           calendar notification; the proposed action would be a no-op courtesy
           reply.

When a rule mixes both, judge it on its PRIMARY justification. If the sentence
would collapse without the "he didn't reply" clause, DROP it.

Rules:
{listing}

Respond with ONLY valid JSON:
{{"verdicts": [{{"n": <number>, "verdict": "KEEP" | "DROP", "why": "<8 words max>"}}]}}"""

    resp = client.messages.create(
        model=MODEL, max_tokens=3000, messages=[{"role": "user", "content": prompt}]
    )
    data = _parse(_text_from_response(resp)) or {}
    return data.get("verdicts", []) or []


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("backup")
    ap.add_argument("--restore", action="store_true", help="Write KEEP patterns back to mem0.")
    args = ap.parse_args()

    pats = json.loads(Path(args.backup).read_text(encoding="utf-8"))
    texts = [(p.get("memory") or "").strip() for p in pats]
    texts = [t for t in texts if t]
    print(f"triaging {len(texts)} pattern(s)...")

    from anthropic import Anthropic

    client = Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])

    verdicts: dict[int, dict] = {}
    for i in range(0, len(texts), BATCH):
        batch = texts[i : i + BATCH]
        for v in triage_batch(client, batch, i):
            try:
                verdicts[int(v["n"])] = v
            except (KeyError, TypeError, ValueError):
                continue
        print(f"  ...{min(i + BATCH, len(texts))}/{len(texts)}")

    keep, drop, unjudged = [], [], []
    for i, t in enumerate(texts):
        v = verdicts.get(i)
        if v is None:
            unjudged.append(t)          # unjudged -> treat as DROP; silence is safer
        elif v.get("verdict") == "KEEP":
            keep.append((t, v.get("why", "")))
        else:
            drop.append((t, v.get("why", "")))

    print(f"\nKEEP {len(keep)}  DROP {len(drop)}  UNJUDGED {len(unjudged)} (dropped by default)\n")
    out = _DEER / "proposal_patterns_triage.json"
    out.write_text(
        json.dumps(
            {"keep": [t for t, _ in keep], "drop": [t for t, _ in drop], "unjudged": unjudged},
            indent=2,
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    print(f"triage written -> {out.name}")

    print("\n--- DROP (stay purged) ---")
    for t, why in drop[:12]:
        print(f"  x [{why}] {t[:110]}")
    print("\n--- KEEP (restore) ---")
    for t, why in keep[:12]:
        print(f"  * [{why}] {t[:110]}")

    if args.restore:
        from deerflow.agents.memory.mem0_store import get_mem0

        m = get_mem0()
        for t, _ in keep:
            # infer=False: store the curated sentence verbatim, same as
            # proposal_learner — the classifier retrieves the exact wording.
            m.add(t, user_id=NAMESPACE, infer=False)
        total = len(m.vector_store.list(filters={"user_id": NAMESPACE}, top_k=10000)[0])
        print(f"\nRestored {len(keep)} pattern(s). Namespace now holds {total}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
