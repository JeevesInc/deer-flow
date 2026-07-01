"""mem0-based long-term memory storage provider.

Replaces the custom fact extraction / JSON storage with mem0's semantic
vector search.  Profile sections (workContext, personalContext, topOfMind)
are still managed via the lightweight FileMemoryStorage so they're always
injected.  Facts are stored and retrieved through mem0.
"""

import logging
import os
import threading
from pathlib import Path
from typing import Any

# Windows SSL fix: inject system trust store so HuggingFace downloads work
try:
    import truststore
    truststore.inject_into_ssl()
except Exception:
    pass

logger = logging.getLogger(__name__)

_mem0_instance = None
_mem0_lock = threading.Lock()

# Default user_id for single-user deployment
MEM0_USER_ID = "deerflow_user"

# Qdrant collection backing all mem0 namespaces (kept in sync with get_mem0()).
MEM0_COLLECTION = "deerflow_memories"

# Dedicated namespace for the semantically-retrievable strategic context layer.
# Stored in the same Qdrant collection but isolated by user_id filter (same
# pattern as the "proposal-patterns" namespace).
STRATEGIC_CONTEXT_USER_ID = "strategic-context"


def _get_mem0_data_dir() -> str:
    """Get the directory for mem0's local data (Qdrant + SQLite)."""
    from deerflow.config.paths import get_paths

    return str(get_paths().base_dir / "mem0_data")


def get_mem0() -> Any:
    """Get or create the global mem0 Memory instance.

    Uses Anthropic for the LLM (fact extraction/dedup) and
    sentence-transformers for local embeddings.  Vector store is
    on-disk Qdrant (no server needed).
    """
    global _mem0_instance
    if _mem0_instance is not None:
        return _mem0_instance

    with _mem0_lock:
        if _mem0_instance is not None:
            return _mem0_instance

        from mem0 import Memory

        api_key = os.environ.get("ANTHROPIC_API_KEY", "")
        if not api_key:
            raise RuntimeError(
                "ANTHROPIC_API_KEY not set — required for mem0 LLM provider"
            )

        data_dir = _get_mem0_data_dir()
        Path(data_dir).mkdir(parents=True, exist_ok=True)

        # Vector store: connect to a Qdrant SERVER (not embedded/on-disk).
        # Embedded Qdrant takes an exclusive filesystem lock on mem0_data that
        # lets only ONE process attach — so the agent, gateway, and webhook
        # receiver would fight over it and whoever lost ran with NO memory
        # (see project_mem0_lock_contention, 2026-07-01). A server lets all
        # three connect concurrently. Host/port are env-overridable; default to
        # the local docker container (monitoring/docker-compose.yml).
        qdrant_host = os.environ.get("MEM0_QDRANT_HOST", "localhost")
        qdrant_port = int(os.environ.get("MEM0_QDRANT_PORT", "6333"))

        config = {
            "llm": {
                "provider": "anthropic",
                "config": {
                    "model": "claude-sonnet-5",
                    "api_key": api_key,
                    "max_tokens": 4096,
                },
            },
            "embedder": {
                "provider": "huggingface",
                "config": {
                    "model": "sentence-transformers/all-MiniLM-L6-v2",
                    "embedding_dims": 384,
                },
            },
            "vector_store": {
                "provider": "qdrant",
                "config": {
                    "collection_name": MEM0_COLLECTION,
                    "host": qdrant_host,
                    "port": qdrant_port,
                    "embedding_model_dims": 384,
                },
            },
            "version": "v1.1",
        }

        logger.info("Initializing mem0 with Qdrant server at %s:%s", qdrant_host, qdrant_port)
        _mem0_instance = Memory.from_config(config_dict=config)
        return _mem0_instance


def add_memories(
    messages: list[dict[str, str]],
    thread_id: str | None = None,
    *,
    user_id: str = MEM0_USER_ID,
    infer: bool = True,
    metadata: dict | None = None,
) -> dict:
    """Add conversation messages to mem0.

    Args:
        messages: List of {"role": "user"|"assistant", "content": "..."} dicts.
        thread_id: Optional thread ID for metadata.
        user_id: mem0 namespace to store under (default: the conversational user).
        infer: When True (default) mem0 runs LLM fact-extraction/dedup on the
            messages. When False the raw message content is stored verbatim as a
            memory — use this for pre-chunked content (e.g. strategic context
            sections) that should keep its coherence instead of being atomized.
        metadata: Extra metadata to attach to the stored memories. Merged with
            the thread_id when provided.

    Returns:
        mem0 add() response dict.
    """
    m = get_mem0()
    meta = dict(metadata) if metadata else {}
    if thread_id:
        meta.setdefault("thread_id", thread_id)
    result = m.add(
        messages,
        user_id=user_id,
        metadata=meta,
        infer=infer,
    )
    logger.info("mem0.add() stored memories (user=%s, infer=%s) from thread %s: %s", user_id, infer, thread_id, result)
    return result


def search_memories(query: str, top_k: int = 10, *, user_id: str = MEM0_USER_ID) -> list[dict]:
    """Search mem0 for memories relevant to the query.

    Args:
        query: The search query (typically the user's latest message).
        top_k: Maximum number of memories to return.
        user_id: mem0 namespace to search within.

    Returns:
        List of memory dicts with 'memory', 'score', 'metadata', etc.
    """
    m = get_mem0()
    # mem0 search filters by user_id and caps with top_k (NOT `limit`, which is
    # silently swallowed by **kwargs and ignored).
    results = m.search(query=query, filters={"user_id": user_id}, top_k=top_k)
    if isinstance(results, dict):
        return results.get("results", [])
    return results if isinstance(results, list) else []


def get_all_memories(*, user_id: str = MEM0_USER_ID) -> list[dict]:
    """Get all stored memories for the given namespace."""
    m = get_mem0()
    results = m.get_all(filters={"user_id": user_id})
    if isinstance(results, dict):
        return results.get("results", [])
    return results if isinstance(results, list) else []


def _qdrant_client():
    """Direct Qdrant client to the same server mem0 uses.

    Needed because mem0's own `get_all()`/count read from mem0's internal index,
    which the 2026-07-01 bulk migration bypassed — so `get_all()` sees only the
    ~20 facts written natively since the migration, not the ~4,400 migrated ones
    (they ARE in Qdrant and ARE reachable via `search()`, just invisible to
    get_all). Anything that needs the true count or a recency-ordered scan must
    go straight to Qdrant, which is the source of truth.
    """
    from qdrant_client import QdrantClient

    host = os.environ.get("MEM0_QDRANT_HOST", "localhost")
    port = int(os.environ.get("MEM0_QDRANT_PORT", "6333"))
    return QdrantClient(host=host, port=port)


def _user_filter(user_id: str):
    from qdrant_client import models as qm

    return qm.Filter(must=[qm.FieldCondition(key="user_id", match=qm.MatchValue(value=user_id))])


def count_memories(*, user_id: str = MEM0_USER_ID) -> int:
    """True count of stored facts for a namespace (scrolls Qdrant, not mem0's index)."""
    client = _qdrant_client()
    return client.count(collection_name=MEM0_COLLECTION, count_filter=_user_filter(user_id), exact=True).count


def get_recent_memories(k: int = 30, *, user_id: str = MEM0_USER_ID) -> list[str]:
    """Return up to k fact texts, most-recent first (by payload created_at).

    Reads directly from Qdrant so it sees the full store (see `_qdrant_client`).
    Facts are stored under the `data` payload key.
    """
    client = _qdrant_client()
    flt = _user_filter(user_id)
    points: list = []
    offset = None
    while True:
        batch, offset = client.scroll(
            collection_name=MEM0_COLLECTION,
            scroll_filter=flt,
            with_payload=True,
            with_vectors=False,
            limit=1000,
            offset=offset,
        )
        points.extend(batch)
        if offset is None:
            break
    points.sort(key=lambda p: (p.payload or {}).get("created_at", ""), reverse=True)
    out: list[str] = []
    for p in points[:k]:
        text = ((p.payload or {}).get("data") or "").strip()
        if text:
            out.append(text)
    return out


def delete_all_memories(*, user_id: str = MEM0_USER_ID) -> None:
    """Delete all memories for the given namespace. Use with caution."""
    m = get_mem0()
    # mem0's delete_all takes user_id directly (not a filters= dict).
    m.delete_all(user_id=user_id)
    logger.warning("All mem0 memories deleted for user %s", user_id)


def reset_mem0_instance() -> None:
    """Reset the global mem0 instance (for testing)."""
    global _mem0_instance
    with _mem0_lock:
        _mem0_instance = None
