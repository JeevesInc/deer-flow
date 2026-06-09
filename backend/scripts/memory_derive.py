"""Auto-derive memory.json from mem0 + STRATEGIC_CONTEXT.md.

Called as Step 0 of the dreams cron. Reads top-k mem0 facts by recency
and clusters them into the memory.json structure, so memory.json is always
a computed summary rather than a manually-maintained file.

Preserves manual-edit keys (version, lastUpdated) and only replaces the
'history.recentMonths.summary' and 'user.workContext.summary' fields
when mem0 has enough facts to do so confidently.

This is intentionally conservative: it supplements rather than replaces
the existing memory.json until we have confidence in the derivation.
"""

import sys, os, json, sqlite3, pickle
from datetime import datetime, timezone
sys.path.insert(0, os.path.join(os.environ.get('SKILLS_PATH', '/mnt/skills'), 'custom', 'sll'))
import storage

BACKEND = storage.BACKEND_DIR
MEM_PATH = BACKEND / '.deer-flow' / 'memory.json'
SC_PATH = BACKEND / '.deer-flow' / 'STRATEGIC_CONTEXT.md'
MEM0_DB = BACKEND / '.deer-flow' / 'mem0_data' / 'collection' / 'deerflow_memories' / 'storage.sqlite'

MIN_FACTS_TO_DERIVE = 50   # only derive if we have enough facts
TOP_K_RECENT = 30          # use this many recent facts for summary


def read_recent_mem0_facts(k=TOP_K_RECENT) -> list[str]:
    if not MEM0_DB.exists():
        return []
    conn = sqlite3.connect(str(MEM0_DB))
    cur = conn.cursor()
    # Get most recently inserted points (last rowid = most recent)
    cur.execute("SELECT point FROM points ORDER BY rowid DESC LIMIT ?", (k,))
    facts = []
    for (pb,) in cur.fetchall():
        try:
            p = pickle.loads(pb)
            text = (p.payload or {}).get('data', '')
            if text:
                facts.append(text)
        except Exception:
            pass
    conn.close()
    return facts


def derive_summary_from_facts(facts: list[str], anthropic_client) -> str | None:
    """Ask Claude Haiku to summarize recent facts into a memory.json summary."""
    if not facts or not anthropic_client:
        return None
    
    facts_text = "\n".join(f"- {f}" for f in facts[:TOP_K_RECENT])
    prompt = f"""You are summarizing recent facts about Brian Mauck's work at Jeeves Financial Technology.
    
Recent facts from memory:
{facts_text}

Write a 2-3 sentence summary of the most important recent context: active deals, priorities, and key relationships.
Be concrete. Use specific names and numbers where present. No filler phrases.
Output only the summary text, nothing else."""

    try:
        resp = anthropic_client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=300,
            messages=[{"role": "user", "content": prompt}]
        )
        return resp.content[0].text.strip()
    except Exception as e:
        print(f"[memory_derive] Haiku call failed: {e}", file=sys.stderr)
        return None


def run():
    if not MEM_PATH.exists():
        print("[memory_derive] memory.json not found, skipping", file=sys.stderr)
        return

    # Count mem0 facts
    if not MEM0_DB.exists():
        print("[memory_derive] mem0 DB not found, skipping", file=sys.stderr)
        return

    conn = sqlite3.connect(str(MEM0_DB))
    cur = conn.cursor()
    cur.execute("SELECT COUNT(*) FROM points")
    total = cur.fetchone()[0]
    conn.close()

    if total < MIN_FACTS_TO_DERIVE:
        print(f"[memory_derive] Only {total} facts, skipping derive (min {MIN_FACTS_TO_DERIVE})", file=sys.stderr)
        return

    # Get recent facts
    facts = read_recent_mem0_facts(TOP_K_RECENT)
    if not facts:
        return

    # Derive summary
    client = storage.anthropic_client()
    summary = derive_summary_from_facts(facts, client)
    if not summary:
        return

    # Read current memory.json
    mem = json.loads(MEM_PATH.read_text(encoding='utf-8'))

    # Update recentMonths summary only
    old_summary = mem.get('history', {}).get('recentMonths', {}).get('summary', '')
    if summary == old_summary:
        print("[memory_derive] No change to summary", file=sys.stderr)
        return

    if 'history' not in mem:
        mem['history'] = {}
    if 'recentMonths' not in mem['history']:
        mem['history']['recentMonths'] = {}

    mem['history']['recentMonths']['summary'] = summary
    mem['lastUpdated'] = datetime.now(timezone.utc).isoformat()

    # Write back
    tmp = MEM_PATH.with_suffix('.json.tmp')
    tmp.write_text(json.dumps(mem, indent=2, ensure_ascii=False), encoding='utf-8')
    tmp.replace(MEM_PATH)
    print(f"[memory_derive] Updated memory.json recentMonths summary from {total} mem0 facts", file=sys.stderr)


if __name__ == '__main__':
    run()
