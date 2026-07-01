"""
Post-session hook: call this at the end of a conversation to extract and store facts.
Pass conversation text as stdin or as argument.

Usage:
    echo "Brian confirmed NB executed April 2026 at 100MM" | uv run python post_session.py
    uv run python post_session.py "NB closed. CIM amendment still open."
"""
import os, sys, warnings
warnings.filterwarnings("ignore")

os.environ["TRANSFORMERS_OFFLINE"] = "1"
os.environ["HF_HUB_OFFLINE"] = "1"
os.environ["MEM0_TELEMETRY"] = "false"

SKILLS = os.environ.get("SKILLS_PATH", "C:/Jeeves/redshift-bot/deer-flow/skills")
sys.path.insert(0, os.path.join(SKILLS, "custom", "mem0-memory"))
from memory_tool import add_memory

def main():
    if len(sys.argv) > 1:
        text = " ".join(sys.argv[1:])
    elif not sys.stdin.isatty():
        text = sys.stdin.read().strip()
    else:
        print("Usage: echo 'text' | python post_session.py")
        sys.exit(1)

    if not text:
        print("No text provided.")
        sys.exit(1)

    results = add_memory(text)
    for r in results:
        print("[%s] %s" % (r.get("event", "?"), r.get("memory", "")))
    print("\n%d memories updated." % len(results))

if __name__ == "__main__":
    main()
