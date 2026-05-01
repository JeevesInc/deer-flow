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

        config = {
            "llm": {
                "provider": "anthropic",
                "config": {
                    "model": "claude-sonnet-4-20250514",
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
                    "collection_name": "deerflow_memories",
                    "path": data_dir,
                    "embedding_model_dims": 384,
                },
            },
            "version": "v1.1",
        }

        logger.info("Initializing mem0 with local Qdrant at %s", data_dir)
        _mem0_instance = Memory.from_config(config_dict=config)
        return _mem0_instance


def add_memories(messages: list[dict[str, str]], thread_id: str | None = None) -> dict:
    """Add conversation messages to mem0.

    Args:
        messages: List of {"role": "user"|"assistant", "content": "..."} dicts.
        thread_id: Optional thread ID for metadata.

    Returns:
        mem0 add() response dict.
    """
    m = get_mem0()
    metadata = {"thread_id": thread_id} if thread_id else {}
    result = m.add(
        messages,
        user_id=MEM0_USER_ID,
        metadata=metadata,
    )
    logger.info("mem0.add() stored memories from thread %s: %s", thread_id, result)
    return result


def search_memories(query: str, top_k: int = 10) -> list[dict]:
    """Search mem0 for memories relevant to the query.

    Args:
        query: The search query (typically the user's latest message).
        top_k: Maximum number of memories to return.

    Returns:
        List of memory dicts with 'memory', 'score', etc.
    """
    m = get_mem0()
    # mem0 v2 uses filters= instead of top-level user_id
    results = m.search(query=query, filters={"user_id": MEM0_USER_ID}, limit=top_k)
    if isinstance(results, dict):
        return results.get("results", [])
    return results if isinstance(results, list) else []


def get_all_memories() -> list[dict]:
    """Get all stored memories for the user."""
    m = get_mem0()
    results = m.get_all(filters={"user_id": MEM0_USER_ID})
    if isinstance(results, dict):
        return results.get("results", [])
    return results if isinstance(results, list) else []


def delete_all_memories() -> None:
    """Delete all memories for the user. Use with caution."""
    m = get_mem0()
    m.delete_all(filters={"user_id": MEM0_USER_ID})
    logger.warning("All mem0 memories deleted for user %s", MEM0_USER_ID)


def reset_mem0_instance() -> None:
    """Reset the global mem0 instance (for testing)."""
    global _mem0_instance
    with _mem0_lock:
        _mem0_instance = None
