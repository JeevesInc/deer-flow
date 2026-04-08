#!/usr/bin/env python3
"""Search past conversations in DeerFlow's checkpoint database.

Provides full-text search across conversation history stored in checkpoints.db,
similar to Hermes Agent's FTS5 session search layer.

Usage:
    python session_search.py "redshift date lag"
    python session_search.py "borrowing base" --limit 5
    python session_search.py "error" --skill jeeves-redshift
"""

import argparse
import json
import os
import re
import sqlite3
import sys
from pathlib import Path


def get_checkpoints_db() -> Path:
    """Locate the checkpoints database."""
    # Standard location
    candidates = [
        Path(os.environ.get("DEER_FLOW_DATA", "")) / "checkpoints.db",
        Path(__file__).resolve().parent.parent.parent.parent.parent / "backend" / ".deer-flow" / "checkpoints.db",
    ]
    for p in candidates:
        if p.exists():
            return p
    print("ERROR: checkpoints.db not found", file=sys.stderr)
    sys.exit(1)


def search_conversations(query: str, limit: int = 10, skill_filter: str | None = None):
    """Search through conversation checkpoints for matching content."""
    db_path = get_checkpoints_db()
    conn = sqlite3.connect(str(db_path))

    try:
        # Get table info to understand schema
        cursor = conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        tables = [row[0] for row in cursor.fetchall()]

        if "checkpoints" not in tables:
            print("No checkpoints table found. Available tables:", tables)
            return

        # Get column info
        cursor = conn.execute("PRAGMA table_info(checkpoints)")
        columns = [row[1] for row in cursor.fetchall()]

        # Search through checkpoint data
        # The checkpoint blob contains serialized conversation state
        query_lower = query.lower()
        matches = []

        # Try to search in the checkpoint data
        if "checkpoint" in columns and "thread_id" in columns:
            cursor = conn.execute(
                "SELECT thread_id, checkpoint FROM checkpoints ORDER BY rowid DESC"
            )
            for thread_id, checkpoint_data in cursor:
                if checkpoint_data is None:
                    continue
                # Try to decode as text
                try:
                    if isinstance(checkpoint_data, bytes):
                        text = checkpoint_data.decode("utf-8", errors="ignore")
                    else:
                        text = str(checkpoint_data)

                    if query_lower in text.lower():
                        # Extract relevant snippets
                        snippets = _extract_snippets(text, query_lower)
                        if snippets:
                            matches.append({
                                "thread_id": thread_id,
                                "snippets": snippets[:3],
                            })
                            if len(matches) >= limit:
                                break
                except Exception:
                    continue

        if not matches:
            # Fallback: try writes table if it exists
            if "writes" in tables:
                cursor = conn.execute("PRAGMA table_info(writes)")
                write_cols = [row[1] for row in cursor.fetchall()]
                if "value" in write_cols:
                    cursor = conn.execute(
                        "SELECT thread_id, value FROM writes ORDER BY rowid DESC"
                    )
                    for thread_id, value in cursor:
                        if value and query_lower in str(value).lower():
                            snippets = _extract_snippets(str(value), query_lower)
                            if snippets:
                                matches.append({
                                    "thread_id": thread_id,
                                    "snippets": snippets[:3],
                                })
                                if len(matches) >= limit:
                                    break

        if not matches:
            print(f"No conversations found matching '{query}'")
            return

        print(f"Found {len(matches)} conversations matching '{query}':\n")
        for m in matches:
            print(f"  Thread: {m['thread_id']}")
            for s in m["snippets"]:
                # Clean up and truncate snippet
                clean = re.sub(r"\s+", " ", s).strip()
                if len(clean) > 200:
                    clean = clean[:200] + "..."
                print(f"    ...{clean}...")
            print()

    finally:
        conn.close()


def _extract_snippets(text: str, query: str, context_chars: int = 100) -> list[str]:
    """Extract text snippets around query matches."""
    snippets = []
    text_lower = text.lower()
    start = 0
    while True:
        pos = text_lower.find(query, start)
        if pos == -1:
            break
        snippet_start = max(0, pos - context_chars)
        snippet_end = min(len(text), pos + len(query) + context_chars)
        snippet = text[snippet_start:snippet_end]
        # Only keep snippets that look like natural language (not binary/encoded data)
        if _is_readable(snippet):
            snippets.append(snippet)
        start = pos + len(query)
        if len(snippets) >= 5:
            break
    return snippets


def _is_readable(text: str) -> bool:
    """Check if text is human-readable (not binary garbage)."""
    if not text:
        return False
    printable = sum(1 for c in text if c.isprintable() or c in "\n\r\t")
    return printable / len(text) > 0.8


def main():
    parser = argparse.ArgumentParser(description="Search past DeerFlow conversations")
    parser.add_argument("query", help="Search query")
    parser.add_argument("--limit", type=int, default=10, help="Max results (default: 10)")
    parser.add_argument("--skill", help="Filter by skill name mentioned in conversation")
    args = parser.parse_args()

    search_conversations(args.query, limit=args.limit, skill_filter=args.skill)


if __name__ == "__main__":
    main()
