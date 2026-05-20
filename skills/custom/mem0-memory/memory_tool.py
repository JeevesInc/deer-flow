"""
Jeeves Agent Memory Tool -- Mem0 + Qdrant embedded + Haiku LLM + local sentence-transformers

Usage:
    from memory_tool import add_memory, search_memory, get_all_memories

    add_memory("NB facility closed at 100MM SOFR+7.5%")
    hits = search_memory("NB facility terms")
    all_mem = get_all_memories()
"""

import os
import warnings
warnings.filterwarnings("ignore")

os.environ["TRANSFORMERS_OFFLINE"] = "1"
os.environ["HF_HUB_OFFLINE"] = "1"
os.environ["MEM0_TELEMETRY"] = "false"

from mem0 import Memory

USER_ID = "brian.mauck@tryjeeves.com"
QDRANT_PATH = os.path.join(
    os.environ.get("WORKSPACE_PATH", "C:/Jeeves/redshift-bot/deer-flow/backend/.deer-flow/threads/1d2803e0-70cb-404a-91e0-03b2e2ad76df/user-data/workspace"),
    "mem0_qdrant"
)

CONFIG = {
    "llm": {
        "provider": "anthropic",
        "config": {
            "model": "claude-haiku-4-5",
            "temperature": 0.1,
            "max_tokens": 2000,
        }
    },
    "embedder": {
        "provider": "huggingface",
        "config": {
            "model": "sentence-transformers/all-MiniLM-L6-v2"
        }
    },
    "vector_store": {
        "provider": "qdrant",
        "config": {
            "collection_name": "jeeves_agent_memory",
            "embedding_model_dims": 384,
            "path": QDRANT_PATH
        }
    }
}

_memory_instance = None

def _get_memory():
    global _memory_instance
    if _memory_instance is None:
        _memory_instance = Memory.from_config(CONFIG)
    return _memory_instance


def add_memory(text, user_id=USER_ID):
    """Extract facts from text and store. Returns list of added/updated memories."""
    m = _get_memory()
    result = m.add(text, user_id=user_id)
    return result.get("results", [])


def search_memory(query, user_id=USER_ID, limit=8):
    """Search relevant memories. Returns list of {memory, score, id} dicts."""
    m = _get_memory()
    result = m.search(query, filters={"user_id": user_id}, limit=limit)
    return result.get("results", [])


def get_all_memories(user_id=USER_ID):
    """Return all stored memories for this user."""
    m = _get_memory()
    result = m.get_all(filters={"user_id": user_id})
    return result.get("results", [])


def delete_memory(memory_id):
    """Delete a specific memory by ID."""
    _get_memory().delete(memory_id)


def update_memory(memory_id, new_text):
    """Manually update a specific memory."""
    _get_memory().update(memory_id, new_text)


if __name__ == "__main__":
    import sys

    cmd = sys.argv[1] if len(sys.argv) > 1 else "all"

    if cmd == "search" and len(sys.argv) > 2:
        query = " ".join(sys.argv[2:])
        hits = search_memory(query)
        for h in hits:
            print("[%.3f] %s" % (h.get("score", 0), h["memory"]))

    elif cmd == "add" and len(sys.argv) > 2:
        text = " ".join(sys.argv[2:])
        added = add_memory(text)
        for item in added:
            print("[%s] %s" % (item["event"], item["memory"]))

    elif cmd == "all":
        mems = get_all_memories()
        print("%d memories stored:\n" % len(mems))
        for item in mems:
            print("  [%s] %s" % (item["id"][:8], item["memory"]))

    elif cmd == "delete" and len(sys.argv) > 2:
        delete_memory(sys.argv[2])
        print("Deleted %s" % sys.argv[2])
