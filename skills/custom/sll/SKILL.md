---
name: sll
description: Synthetic Limbic Layer — the bot's persistent reward engine. Scores every substantive turn, extracts lessons from failures and successes, and injects relevant lessons before complex tasks. Driven by SOUL.md integration spec; not user-invokable directly.
allowed-tools:
  - bash
---

# Synthetic Limbic Layer (SLL)

This skill is invoked automatically by SOUL.md integration on every turn — not by user request. See SOUL.md "Synthetic Limbic Layer" section for the full integration contract.

## Scripts

- `sll_score.py` — end-of-turn composite scoring + start-of-turn sentiment application
- `sll_inject.py` — retrieve and format relevant lessons for the current task
- `sll_dashboard.py` — view score history, top lessons, prune stale lessons

## Storage

All data lives under `.deer-flow/sll/`:
- `pending.json` — turn awaiting sentiment from next user reply
- `log.jsonl` — every scored turn (append-only)
- `lessons.jsonl` — every stored lesson (text, outcome, boost, retrieval stats)
- `retrievals.jsonl` — every inject call

Retrieval uses a Haiku ranking call over `lessons.jsonl` rather than mem0/Qdrant — the SLL scripts run as bash subprocesses fired by the agent, and the LangGraph worker already holds the Qdrant lock, so a subprocess cannot share that store.

## Behavior

- Default initial composite = 0.6 (neutral)
- Sentiment from next user reply overrides/modifies:
  - `explicit_correction` → 0.1 (failure → lesson stored 2.5x)
  - `explicit_praise` → 0.95 (success → lesson stored 2.0x)
  - `implicit_negative` → composite − 0.35
  - `implicit_positive` → composite + 0.15
- Lessons stored only when final composite < 0.4 or > 0.8
- Failures format: `[!] AVOID ...` Successes format: `[+] DO ...`

## Safety

All scripts MUST exit 0 even on failure — they run inside the agent's bash loop and a non-zero exit would block the turn. Errors go to stderr.
