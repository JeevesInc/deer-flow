"""
One-time seed: reads STRATEGIC_CONTEXT.md and existing memory block,
extracts facts into Mem0 so we start with institutional context populated.
"""
import os, sys, warnings
warnings.filterwarnings("ignore")

os.environ["TRANSFORMERS_OFFLINE"] = "1"
os.environ["HF_HUB_OFFLINE"] = "1"
os.environ["MEM0_TELEMETRY"] = "false"

SKILLS = os.environ.get("SKILLS_PATH", "C:/Jeeves/redshift-bot/deer-flow/skills")
sys.path.insert(0, os.path.join(SKILLS, "custom", "mem0-memory"))
from memory_tool import add_memory, get_all_memories

CONTEXT_PATH = os.path.join(
    os.environ.get("WORKSPACE_PATH", "C:/Jeeves/redshift-bot/deer-flow/backend/.deer-flow/threads/1d2803e0-70cb-404a-91e0-03b2e2ad76df/user-data/workspace"),
    "..", ".deer-flow", "STRATEGIC_CONTEXT.md"
)

def main():
    # Read strategic context
    ctx_path = os.path.abspath(CONTEXT_PATH)
    if not os.path.exists(ctx_path):
        print("STRATEGIC_CONTEXT.md not found at", ctx_path)
        return

    with open(ctx_path) as f:
        content = f.read()

    print("Seeding from STRATEGIC_CONTEXT.md (%d chars)..." % len(content))

    # Split into chunks to avoid token limits
    chunks = [content[i:i+2000] for i in range(0, len(content), 2000)]
    total_added = 0
    for i, chunk in enumerate(chunks):
        results = add_memory(chunk)
        added = len([r for r in results if r.get("event") == "ADD"])
        total_added += added
        print("  Chunk %d/%d -> %d facts extracted" % (i+1, len(chunks), added))

    print("\nDone. Total facts added: %d" % total_added)
    print("Total memories in store: %d" % len(get_all_memories()))

if __name__ == "__main__":
    main()
