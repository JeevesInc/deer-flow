# mem0-memory skill

> ⚠️ STATUS: The live Mem0 store is managed by DeerFlow internally. Do NOT write to it from external scripts while the server is running (Qdrant embedded mode, single-writer).

## What actually exists

The live Mem0 system is fully operational:
- **962+ facts** stored at `.deer-flow/mem0_data/collection/deerflow_memories/`
- **Injection**: `Mem0InjectionMiddleware` fires on every LLM call, injects top-10 relevant memories as a SystemMessage extension
- **Write**: `MemoryMiddleware` queues post-session fact extraction automatically
- **LLM**: Claude Haiku (fast, cheap — patched from Sonnet on 2026-05-14)
- **Embeddings**: sentence-transformers/all-MiniLM-L6-v2 (local, no API cost)
- **Vector store**: Qdrant embedded at `.deer-flow/mem0_data/`

## Inspecting the store (read-only, while server is running)

```python
import sqlite3, pickle, os

backend = os.path.join(os.path.dirname(os.environ.get('SKILLS_PATH','')), 'backend')
db = os.path.join(backend, '.deer-flow', 'mem0_data', 'collection', 'deerflow_memories', 'storage.sqlite')
conn = sqlite3.connect(db)
cur = conn.cursor()
cur.execute('SELECT COUNT(*) FROM points')
print('Total facts:', cur.fetchone()[0])

cur.execute('SELECT id, point FROM points ORDER BY rowid DESC LIMIT 20')
for row_id, point_bytes in cur.fetchall():
    point = pickle.loads(point_bytes)
    text = (point.payload or {}).get('data', '')
    print('-', text[:120])
conn.close()
```

## API access (Gateway process — limited due to Qdrant multi-process lock)

```
GET http://localhost:8001/api/memory/status   # profile sections only (works)
GET http://localhost:8001/api/memory/mem0     # returns error (Gateway can't read locked store)
```

The Gateway API showing `mem0_count: -1` is expected — it's a multi-process limitation, not a data loss.
The LangGraph process holds the Qdrant lock and injection works fine within that process.

## Key architecture

- Two processes: LangGraph (port 2024, holds Qdrant lock) + Gateway (port 8001, API only)
- `Mem0InjectionMiddleware` runs inside LangGraph → injection works ✅
- Gateway API can't access Qdrant directly → shows -1 → cosmetic, not functional

## What the recalled_memories block is

The `<recalled_memories>` block at the top of each session context IS Mem0 working.
The injection runs at every LLM call, searching the 962-fact store for the 10 most relevant to the user's message.

## LLM config location

`packages/harness/deerflow/agents/memory/mem0_store.py`
Model: `claude-haiku-4-5` (fact extraction and dedup — patched 2026-05-14)

## Restart required after patching

After changing `mem0_store.py`, restart the LangGraph server for the new model to take effect.
The Qdrant store persists across restarts (facts are not lost).
