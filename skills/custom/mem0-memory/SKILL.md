---
name: mem0-memory
description: Use this skill to inspect, search, add to, or clean up the agent's long-term semantic memory (mem0). Use when the user asks "what do you remember about X", wants a memory added/corrected/deleted, or asks about the memory system's health or counts.
allowed-tools:
  - bash
  - read_file
---

# mem0-memory — Long-Term Semantic Memory

> **Accuracy is mandatory.** Recalled memories reflect what was true when written — verify numbers/claims before repeating them.

## Current architecture (since 2026-07-01)

- **Vector store:** Qdrant **server** (`deerflow-qdrant` container from `monitoring/docker-compose.yml`, `localhost:6333`). Shared by the LangGraph agent, the gateway (`/api/memory`), and the webhook receiver — the old embedded single-writer lock is gone.
- **Collection:** `deerflow_memories`, namespaced by `user_id`:
  - `deerflow_user` — conversational facts (the agent's main memory)
  - `strategic-context` — STRATEGIC_CONTEXT.md sections (auto-injected per turn)
  - `proposal-patterns` — webhook classifier learnings
  - `lessons` — SLL lessons (synced from `.deer-flow/sll/lessons.jsonl` by `backend/scripts/sll_sync.py`; injected per turn as `<learned_lessons>`)
- **Embedder:** `sentence-transformers/all-MiniLM-L6-v2` (local, 384-dim). **LLM** for fact extraction: Sonnet 5.
- **Injection:** `Mem0InjectionMiddleware` searches per turn with the user's message and injects results **score-gated** by `memory.mem0_min_injection_score` (default 0.22).
- Config lives in `backend/packages/harness/deerflow/agents/memory/mem0_store.py` (env: `MEM0_QDRANT_HOST`/`MEM0_QDRANT_PORT`). Changing it needs a LangGraph restart.

## Commands

```bash
uv run python /mnt/skills/custom/mem0-memory/memory_tool.py search "NB facility terms"
uv run python /mnt/skills/custom/mem0-memory/memory_tool.py add "NB facility closed at 100MM SOFR+7.5%"
uv run python /mnt/skills/custom/mem0-memory/memory_tool.py recent 30
uv run python /mnt/skills/custom/mem0-memory/memory_tool.py count
uv run python /mnt/skills/custom/mem0-memory/memory_tool.py delete <memory_id>
```

These hit the SAME live store the agent uses — additions are immediately searchable by the running agent.

## Known gotchas

- **`mem0.get_all()` is blind to migrated facts.** The 2026-07-01 bulk migration bypassed mem0's internal index, so `get_all()` sees only natively-written facts (~20) while Qdrant holds 4,400+. `search()` reaches everything. For counts or recency scans use `memory_tool.py count` / `recent` (they scroll Qdrant directly).
- **mem0 API quirks:** `search()` takes `top_k=` (NOT `limit`, silently ignored); `delete_all` wants `user_id=` (NOT `filters=`).
- **If Qdrant is down** (`curl localhost:6333/readyz`), the agent runs with no memory and logs `mem0 injection failed`. Restart via `docker compose -f monitoring/docker-compose.yml up -d qdrant`.

## Health check

```bash
curl -s localhost:6333/readyz                      # Qdrant server
curl -s localhost:8001/api/memory/status           # gateway view (profile + true mem0 count)
uv run python /mnt/skills/custom/mem0-memory/memory_tool.py count
```
