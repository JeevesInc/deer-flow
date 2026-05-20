#!/usr/bin/env python3
"""Migrate high-value facts from memory.json to mem0 and slim down the file.

Usage:
    cd deer-flow/backend
    .venv/Scripts/python.exe scripts/migrate_to_mem0.py

This script:
1. Reads all facts from memory.json
2. Filters to high-confidence facts (>= 0.7)
3. Adds each to mem0 as a user memory
4. Backs up the original memory.json
5. Writes a slimmed memory.json with only profile sections (no facts)
"""

import json
import os
import shutil
import sys
from datetime import datetime
from pathlib import Path

# Add harness to path
BACKEND_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND_DIR / "packages" / "harness"))

# Load .env
env_path = BACKEND_DIR / ".env"
if env_path.exists():
    for line in env_path.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, _, val = line.partition("=")
            os.environ.setdefault(key.strip(), val.strip())


def main():
    memory_file = BACKEND_DIR / ".deer-flow" / "memory.json"
    if not memory_file.exists():
        print(f"ERROR: {memory_file} not found")
        return 1

    # Load current memory
    with open(memory_file, encoding="utf-8") as f:
        memory_data = json.load(f)

    facts = memory_data.get("facts", [])
    print(f"Found {len(facts)} facts in memory.json")

    if not facts:
        print("No facts to migrate.")
        return 0

    # Filter to high-confidence facts
    high_conf = [f for f in facts if f.get("confidence", 0) >= 0.7]
    print(f"Migrating {len(high_conf)} high-confidence facts to mem0")

    # Initialize mem0
    from deerflow.agents.memory.mem0_store import get_mem0, MEM0_USER_ID

    m = get_mem0()

    migrated = 0
    failed = 0
    for fact in high_conf:
        content = fact.get("content", "").strip()
        if not content:
            continue
        try:
            m.add(
                content,
                user_id=MEM0_USER_ID,
                metadata={
                    "category": fact.get("category", "context"),
                    "confidence": fact.get("confidence", 0.7),
                    "source": fact.get("source", "migration"),
                    "migrated_from": "memory.json",
                    "original_id": fact.get("id", ""),
                },
            )
            migrated += 1
            if migrated % 50 == 0:
                print(f"  ...migrated {migrated}/{len(high_conf)}")
        except Exception as e:
            print(f"  FAILED: {content[:60]}... — {e}")
            failed += 1

    print(f"\nMigration complete: {migrated} succeeded, {failed} failed")

    # Backup original
    backup_path = memory_file.with_suffix(f".backup.{datetime.utcnow().strftime('%Y%m%dT%H%M%S')}.json")
    shutil.copy2(memory_file, backup_path)
    print(f"Backup saved to {backup_path}")

    # Write slimmed memory.json (profile only, no facts)
    slim_memory = {
        "version": "2.0",
        "lastUpdated": datetime.utcnow().isoformat() + "Z",
        "user": memory_data.get("user", {}),
        "history": memory_data.get("history", {}),
    }

    with open(memory_file, "w", encoding="utf-8") as f:
        json.dump(slim_memory, f, indent=2, ensure_ascii=False)
    print(f"Slimmed memory.json written (no facts, profile only)")

    # Verify mem0
    all_memories = m.get_all(filters={"user_id": MEM0_USER_ID})
    count = len(all_memories.get("results", [])) if isinstance(all_memories, dict) else len(all_memories)
    print(f"mem0 now contains {count} memories total")

    return 0


if __name__ == "__main__":
    sys.exit(main())
