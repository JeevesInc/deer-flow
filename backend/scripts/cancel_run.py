"""Cancel all in-flight runs on a given LangGraph thread.

Usage:
    .venv/Scripts/python.exe scripts/cancel_run.py <thread_id>

Used to recover threads whose state was corrupted by a mid-run process
restart (orphaned tool calls trigger an infinite dangling-tool loop).
"""

from __future__ import annotations

import asyncio
import sys

from langgraph_sdk import get_client


async def main(thread_id: str) -> int:
    client = get_client(url="http://localhost:2024")
    runs = await client.runs.list(thread_id, limit=20)
    print(f"thread {thread_id}: {len(runs)} runs")
    cancelled = 0
    for r in runs:
        status = r.get("status")
        print(f"  {r.get('run_id')}  status={status}")
        if status in ("pending", "running"):
            await client.runs.cancel(thread_id, r["run_id"])
            print("    -> cancelled")
            cancelled += 1
    print(f"done: cancelled {cancelled} run(s)")
    return 0 if cancelled else 1


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("usage: cancel_run.py <thread_id>", file=sys.stderr)
        sys.exit(2)
    sys.exit(asyncio.run(main(sys.argv[1])))
