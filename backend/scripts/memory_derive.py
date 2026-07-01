"""Auto-derive memory.json from the live mem0 (Qdrant server) long-term store.

Called as Step 0 of the dreams cron (in an isolated subprocess). Reads the
most-recent mem0 facts and clusters them into a short recentMonths summary, so
memory.json stays a computed summary rather than a hand-maintained file.

Preserves manual-edit keys (version, lastUpdated) and only replaces
`history.recentMonths.summary` when mem0 has enough facts to do so confidently.
This is intentionally conservative: it supplements rather than replaces
memory.json.

History (2026-07-01): this script used to read an *embedded* Qdrant SQLite at
`.deer-flow/mem0_data/collection/.../storage.sqlite`. That store was frozen as
a rollback backup when mem0 migrated to a Qdrant *server*, so the derive was
summarizing stale facts. It also imported the SLL skill's `storage` module via
`$SKILLS_PATH`, which is not set outside the sandbox — so on this box the import
actually failed and the derive never ran. Both problems are fixed by reading the
live store through the harness `mem0_store` helpers and resolving paths via
`deerflow.config.paths`, dropping the SLL dependency entirely.
"""

import json
import os
import sys
from datetime import UTC, datetime
from pathlib import Path

MIN_FACTS_TO_DERIVE = 50   # only derive if we have enough facts
TOP_K_RECENT = 30          # use this many recent facts for the summary
DERIVE_MODEL = os.environ.get("MEMORY_DERIVE_MODEL", "claude-haiku-4-5-20251001")


def _paths():
    """Return (memory.json, STRATEGIC_CONTEXT.md) paths.

    Resolved `__file__`-relative (this file is backend/scripts/memory_derive.py,
    so backend == parents[1]) rather than via get_paths(), whose base_dir is
    config/env-dependent and falls back to ~/.deer-flow outside the gateway
    process — which would point the derive at the wrong (or a nonexistent)
    memory.json.
    """
    dd = Path(__file__).resolve().parents[1] / ".deer-flow"
    return dd / "memory.json", dd / "STRATEGIC_CONTEXT.md"


def read_recent_mem0_facts(k: int = TOP_K_RECENT) -> list[str]:
    """Return up to k fact texts, most-recent first (live Qdrant, source of truth)."""
    from deerflow.agents.memory.mem0_store import get_recent_memories

    return get_recent_memories(k)


def count_mem0_facts() -> int:
    from deerflow.agents.memory.mem0_store import count_memories

    return count_memories()


def derive_summary_from_facts(facts: list[str]) -> str | None:
    """Ask Claude Haiku to summarize recent facts into a memory.json summary."""
    if not facts:
        return None
    try:
        from anthropic import Anthropic
    except Exception as e:
        print(f"[memory_derive] anthropic import failed: {e}", file=sys.stderr)
        return None
    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key:
        print("[memory_derive] ANTHROPIC_API_KEY not set, skipping", file=sys.stderr)
        return None

    facts_text = "\n".join(f"- {f}" for f in facts[:TOP_K_RECENT])
    prompt = f"""You are summarizing recent facts about Brian Mauck's work at Jeeves Financial Technology.

Recent facts from memory:
{facts_text}

Write a 2-3 sentence summary of the most important recent context: active deals, priorities, and key relationships.
Be concrete. Use specific names and numbers where present. No filler phrases.
Output only the summary text, nothing else."""

    try:
        resp = Anthropic(api_key=api_key).messages.create(
            model=DERIVE_MODEL,
            max_tokens=300,
            messages=[{"role": "user", "content": prompt}],
        )
        return resp.content[0].text.strip()
    except Exception as e:
        print(f"[memory_derive] Haiku call failed: {e}", file=sys.stderr)
        return None


def run():
    mem_path, _sc_path = _paths()
    if not mem_path.exists():
        print("[memory_derive] memory.json not found, skipping", file=sys.stderr)
        return

    try:
        total = count_mem0_facts()
    except Exception as e:  # Qdrant down / mem0 init failure — degrade quietly
        print(f"[memory_derive] mem0 count failed: {e}", file=sys.stderr)
        return
    if total < MIN_FACTS_TO_DERIVE:
        print(f"[memory_derive] Only {total} facts, skipping derive (min {MIN_FACTS_TO_DERIVE})", file=sys.stderr)
        return

    try:
        facts = read_recent_mem0_facts(TOP_K_RECENT)
    except Exception as e:
        print(f"[memory_derive] mem0 read failed: {e}", file=sys.stderr)
        return
    if not facts:
        return

    summary = derive_summary_from_facts(facts)
    if not summary:
        return

    mem = json.loads(mem_path.read_text(encoding="utf-8"))
    old_summary = mem.get("history", {}).get("recentMonths", {}).get("summary", "")
    if summary == old_summary:
        print("[memory_derive] No change to summary", file=sys.stderr)
        return

    mem.setdefault("history", {}).setdefault("recentMonths", {})
    mem["history"]["recentMonths"]["summary"] = summary
    mem["lastUpdated"] = datetime.now(UTC).isoformat()

    tmp = mem_path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(mem, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(mem_path)
    print(f"[memory_derive] Updated memory.json recentMonths summary from {total} mem0 facts", file=sys.stderr)


if __name__ == "__main__":
    run()
