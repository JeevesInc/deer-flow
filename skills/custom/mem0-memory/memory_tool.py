"""Jeeves agent memory tool — thin CLI/import surface over the LIVE mem0 store.

This delegates to the harness module (`deerflow.agents.memory.mem0_store`),
which talks to the shared **Qdrant server** (deerflow-qdrant container,
localhost:6333) — the same store the agent, gateway, and webhook use.

History: this tool used to open its OWN thread-scoped embedded Qdrant
(`jeeves_agent_memory` under a threads/<uuid>/workspace dir) — an orphan
store nothing else read (Skills-H3). Anything written there was invisible
to the agent. Do not resurrect embedded-path configs here.

Usage:
    uv run python memory_tool.py search "NB facility terms"
    uv run python memory_tool.py add "NB facility closed at 100MM SOFR+7.5%"
    uv run python memory_tool.py recent [k]
    uv run python memory_tool.py count
    uv run python memory_tool.py delete <memory_id>

    from memory_tool import add_memory, search_memory
"""

import os
import sys
import warnings
from pathlib import Path

warnings.filterwarnings("ignore")

os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")
os.environ.setdefault("HF_HUB_OFFLINE", "1")
os.environ.setdefault("MEM0_TELEMETRY", "false")

# Make the harness + backend .env available when run as a skill script.
_REPO = Path(__file__).resolve().parents[3]  # .../deer-flow
sys.path.insert(0, str(_REPO / "backend" / "packages" / "harness"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "_shared"))
try:
    from env_loader import load_env

    load_env()
except Exception:
    pass

from deerflow.agents.memory.mem0_store import (  # noqa: E402
    MEM0_USER_ID,
    add_memories,
    count_memories,
    get_mem0,
    get_recent_memories,
    search_memories,
)


def add_memory(text, user_id=MEM0_USER_ID):
    """Extract facts from text and store in the live shared store."""
    result = add_memories([{"role": "user", "content": text}], user_id=user_id)
    return result.get("results", []) if isinstance(result, dict) else result


def search_memory(query, user_id=MEM0_USER_ID, limit=8):
    """Search relevant memories. Returns list of {memory, score, id} dicts."""
    return search_memories(query, top_k=limit, user_id=user_id)


def recent_memories(k=30, user_id=MEM0_USER_ID):
    """Most recent k memory texts (direct Qdrant scroll — mem0's get_all()
    is blind to the 2026-07-01 bulk-migrated facts; the scroll is not)."""
    return get_recent_memories(k, user_id=user_id)


def memory_count(user_id=MEM0_USER_ID):
    """True point count for the namespace (direct Qdrant, not mem0 get_all)."""
    return count_memories(user_id=user_id)


def delete_memory(memory_id):
    """Delete a specific memory by ID."""
    get_mem0().delete(memory_id)


def update_memory(memory_id, new_text):
    """Manually update a specific memory."""
    get_mem0().update(memory_id, new_text)


if __name__ == "__main__":
    # Owner-gated: mem0 holds Brian's private facts. Non-owner runs are
    # blocked here (see skills/custom/_shared/actor_guard.py).
    import os as _og_os, sys as _og_sys
    _og_sys.path.insert(0, _og_os.path.join(_og_os.path.dirname(
        _og_os.path.dirname(_og_os.path.abspath(__file__))), '_shared'))
    from actor_guard import require_owner as _require_owner
    _require_owner("the agent's long-term memory (mem0)")
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    cmd = sys.argv[1] if len(sys.argv) > 1 else "count"

    if cmd == "search" and len(sys.argv) > 2:
        for h in search_memory(" ".join(sys.argv[2:])):
            print("[%.3f] %s" % (h.get("score", 0) or 0, h.get("memory", "")))
    elif cmd == "add" and len(sys.argv) > 2:
        for item in add_memory(" ".join(sys.argv[2:])):
            print("[%s] %s" % (item.get("event", "?"), item.get("memory", "")))
    elif cmd == "recent":
        k = int(sys.argv[2]) if len(sys.argv) > 2 else 30
        for text in recent_memories(k):
            print("- %s" % text)
    elif cmd == "count":
        print("%d memories in namespace %s" % (memory_count(), MEM0_USER_ID))
    elif cmd == "delete" and len(sys.argv) > 2:
        delete_memory(sys.argv[2])
        print("Deleted %s" % sys.argv[2])
    else:
        print(__doc__)
