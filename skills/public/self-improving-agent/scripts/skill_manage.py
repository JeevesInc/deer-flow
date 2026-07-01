#!/usr/bin/env python3
"""Skill management tool for DeerFlow self-improving agent.

Provides create, patch, read, list, and delete operations on skill files,
modeled after Hermes Agent's skill_manage tool.

Usage:
    python skill_manage.py list
    python skill_manage.py read <skill-name>
    python skill_manage.py create <skill-name> --description "..." --content "..."
    python skill_manage.py patch <skill-name> --old "old text" --new "new text"
    python skill_manage.py append <skill-name> --section "## Section" --content "new content"
    python skill_manage.py log <skill-name> --episode '{"situation": "...", "lesson": "..."}'
    python skill_manage.py patterns [--skill <skill-name>]
    python skill_manage.py search <query>
"""

import argparse
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path


def get_skills_root() -> Path:
    """Get the skills root directory."""
    # Try environment variable first (DeerFlow sandbox sets this)
    skills_path = os.environ.get("SKILLS_PATH")
    if skills_path:
        return Path(skills_path)
    # Fallback: relative to this script
    return Path(__file__).resolve().parent.parent.parent.parent


def get_memory_root() -> Path:
    """Get the self-improving-agent memory directory."""
    return get_skills_root() / "public" / "self-improving-agent" / "memory"


def cmd_list(args):
    """List all available skills with their descriptions."""
    root = get_skills_root()
    skills = []
    for category in ["public", "custom"]:
        cat_path = root / category
        if not cat_path.exists():
            continue
        for skill_dir in sorted(cat_path.iterdir()):
            skill_file = skill_dir / "SKILL.md"
            if not skill_file.exists():
                continue
            desc = _extract_description(skill_file)
            skills.append({
                "name": skill_dir.name,
                "category": category,
                "description": desc[:120] if desc else "(no description)",
            })
    for s in skills:
        print(f"  [{s['category']}] {s['name']}: {s['description']}")
    print(f"\n{len(skills)} skills total")


def cmd_read(args):
    """Read a skill's SKILL.md content."""
    skill_file = _find_skill(args.skill_name)
    if not skill_file:
        print(f"ERROR: Skill '{args.skill_name}' not found", file=sys.stderr)
        sys.exit(1)
    print(skill_file.read_text(encoding="utf-8"))


def cmd_create(args):
    """Create a new skill."""
    root = get_skills_root()
    category = args.category or "custom"
    skill_dir = root / category / args.skill_name
    skill_file = skill_dir / "SKILL.md"

    if skill_file.exists():
        print(f"ERROR: Skill '{args.skill_name}' already exists at {skill_file}", file=sys.stderr)
        sys.exit(1)

    skill_dir.mkdir(parents=True, exist_ok=True)

    content = f"""---
name: {args.skill_name}
description: >
  {args.description}
allowed-tools:
  - bash
  - write_file
  - read_file
---

# {args.skill_name.replace('-', ' ').title()}

<!-- Created: {datetime.now(timezone.utc).strftime('%Y-%m-%d')} | source: self-improving-agent -->

{args.content}
"""
    skill_file.write_text(content, encoding="utf-8")
    print(f"Created skill: {skill_file}")

    # Log the creation as an episode
    _log_episode(
        skill="self-improving-agent",
        situation=f"Created new skill: {args.skill_name}",
        solution=f"Skill created at {category}/{args.skill_name}",
        lesson=f"New capability added: {args.description[:100]}",
    )


def cmd_patch(args):
    """Patch a skill file using old_string/new_string replacement."""
    skill_file = _find_skill(args.skill_name)
    if not skill_file:
        print(f"ERROR: Skill '{args.skill_name}' not found", file=sys.stderr)
        sys.exit(1)

    content = skill_file.read_text(encoding="utf-8")
    if args.old not in content:
        print(f"ERROR: old_string not found in {args.skill_name}/SKILL.md", file=sys.stderr)
        print(f"  Looking for: {args.old[:80]}...", file=sys.stderr)
        sys.exit(1)

    new_content = content.replace(args.old, args.new, 1)
    skill_file.write_text(new_content, encoding="utf-8")
    print(f"Patched {args.skill_name}/SKILL.md")
    print(f"  Replaced: {args.old[:60]}...")
    print(f"  With:     {args.new[:60]}...")

    # Log the patch as an episode
    _log_episode(
        skill=args.skill_name,
        situation=f"Patched skill guidance in {args.skill_name}",
        solution=f"Replaced: {args.old[:80]}... -> {args.new[:80]}...",
        lesson="Skill guidance updated based on experience",
    )


def cmd_append(args):
    """Append content to a specific section of a skill file."""
    skill_file = _find_skill(args.skill_name)
    if not skill_file:
        print(f"ERROR: Skill '{args.skill_name}' not found", file=sys.stderr)
        sys.exit(1)

    content = skill_file.read_text(encoding="utf-8")

    # Find the section header
    section_pattern = re.escape(args.section)
    match = re.search(f"^({section_pattern}.*)$", content, re.MULTILINE)
    if not match:
        # Section doesn't exist, append at end
        marker = f"\n\n<!-- Evolution: {datetime.now(timezone.utc).strftime('%Y-%m-%d')} | source: self-improving-agent -->\n"
        content += f"\n{marker}{args.section}\n\n{args.content}\n"
    else:
        # Find end of section (next heading of same or higher level)
        section_level = len(args.section) - len(args.section.lstrip("#"))
        rest = content[match.end():]
        next_heading = re.search(rf"^#{{{1},{section_level}}}\s", rest, re.MULTILINE)
        if next_heading:
            insert_pos = match.end() + next_heading.start()
        else:
            insert_pos = len(content)

        marker = f"\n<!-- Evolution: {datetime.now(timezone.utc).strftime('%Y-%m-%d')} | source: self-improving-agent -->\n"
        content = content[:insert_pos] + f"\n{marker}{args.content}\n" + content[insert_pos:]

    skill_file.write_text(content, encoding="utf-8")
    print(f"Appended to {args.skill_name}/SKILL.md section '{args.section}'")


def cmd_log(args):
    """Log an episode to episodic memory."""
    try:
        episode_data = json.loads(args.episode)
    except json.JSONDecodeError as e:
        print(f"ERROR: Invalid JSON: {e}", file=sys.stderr)
        sys.exit(1)

    _log_episode(
        skill=args.skill_name,
        situation=episode_data.get("situation", ""),
        root_cause=episode_data.get("root_cause", ""),
        solution=episode_data.get("solution", ""),
        lesson=episode_data.get("lesson", ""),
        rating=episode_data.get("rating"),
        comments=episode_data.get("comments"),
    )
    print(f"Logged episode for skill: {args.skill_name}")


def cmd_patterns(args):
    """Show learned patterns, optionally filtered by skill."""
    mem_root = get_memory_root()
    patterns_file = mem_root / "semantic-patterns.json"

    if not patterns_file.exists():
        print("No patterns learned yet.")
        return

    with open(patterns_file, encoding="utf-8") as f:
        data = json.load(f)

    patterns = data.get("patterns", {})
    if not patterns:
        print("No patterns learned yet.")
        return

    for pid, p in sorted(patterns.items(), key=lambda x: x[1].get("confidence", 0), reverse=True):
        if args.skill and args.skill not in p.get("target_skills", []):
            continue
        conf = p.get("confidence", 0)
        apps = p.get("applications", 0)
        print(f"  [{conf:.2f} | {apps} uses] {p.get('name', pid)}")
        print(f"    Pattern: {p.get('pattern', 'N/A')}")
        print(f"    Skills: {', '.join(p.get('target_skills', []))}")
        print()


def cmd_search(args):
    """Search episodic memory for past experiences."""
    mem_root = get_memory_root()
    ep_dir = mem_root / "episodic"
    if not ep_dir.exists():
        print("No episodes recorded yet.")
        return

    query = args.query.lower()
    matches = []

    for ep_file in sorted(ep_dir.glob("*.json")):
        with open(ep_file, encoding="utf-8") as f:
            ep = json.load(f)
        # Search across all text fields
        searchable = " ".join(str(v) for v in ep.values() if isinstance(v, str)).lower()
        if query in searchable:
            matches.append(ep)

    if not matches:
        print(f"No episodes matching '{args.query}'")
        return

    for ep in matches[-10:]:  # Show last 10 matches
        print(f"  [{ep.get('timestamp', '?')[:10]}] {ep.get('skill', '?')}")
        print(f"    Situation: {ep.get('situation', 'N/A')}")
        print(f"    Lesson: {ep.get('lesson', 'N/A')}")
        print()
    print(f"{len(matches)} matching episodes found")


# --- Helpers ---

def _find_skill(name: str) -> Path | None:
    """Find a skill by name across public and custom directories."""
    root = get_skills_root()
    for category in ["custom", "public"]:
        skill_file = root / category / name / "SKILL.md"
        if skill_file.exists():
            return skill_file
    return None


def _extract_description(skill_file: Path) -> str:
    """Extract the description from SKILL.md frontmatter."""
    content = skill_file.read_text(encoding="utf-8")
    match = re.search(r"^---\s*\n(.*?)\n---", content, re.DOTALL)
    if not match:
        return ""
    fm = match.group(1)
    desc_match = re.search(r"description:\s*>?\s*\n?\s*(.*?)(?:\n\w|\nallowed|\n---)", fm, re.DOTALL)
    if desc_match:
        return " ".join(desc_match.group(1).split())
    desc_match = re.search(r"description:\s*(.+)", fm)
    if desc_match:
        return desc_match.group(1).strip()
    return ""


def _log_episode(skill: str, situation: str, solution: str = "", lesson: str = "",
                 root_cause: str = "", rating=None, comments=None):
    """Write an episode to episodic memory."""
    mem_root = get_memory_root()
    ep_dir = mem_root / "episodic"
    ep_dir.mkdir(parents=True, exist_ok=True)

    now = datetime.now(timezone.utc)
    date_str = now.strftime("%Y-%m-%d")

    # Find next episode number for today
    existing = list(ep_dir.glob(f"{date_str}-*.json"))
    ep_num = len(existing) + 1

    episode = {
        "id": f"ep-{date_str}-{ep_num:03d}",
        "timestamp": now.isoformat(),
        "skill": skill,
        "situation": situation,
        "root_cause": root_cause,
        "solution": solution,
        "lesson": lesson,
        "user_feedback": {
            "rating": rating,
            "comments": comments,
        },
    }

    filename = f"{date_str}-{skill}-{ep_num:03d}.json"
    filepath = ep_dir / filename
    with open(filepath, "w", encoding="utf-8") as f:
        json.dump(episode, f, indent=2)

    # Also update semantic patterns if lesson is substantial
    if lesson and len(lesson) > 20:
        _maybe_update_pattern(episode)


def _maybe_update_pattern(episode: dict):
    """Check if an episode should create or reinforce a semantic pattern."""
    mem_root = get_memory_root()
    patterns_file = mem_root / "semantic-patterns.json"

    if patterns_file.exists():
        with open(patterns_file, encoding="utf-8") as f:
            data = json.load(f)
    else:
        data = {"patterns": {}}

    # Check if a similar pattern already exists
    lesson_lower = episode.get("lesson", "").lower()
    for pid, p in data["patterns"].items():
        if _text_similarity(p.get("pattern", "").lower(), lesson_lower) > 0.5:
            # Reinforce existing pattern
            p["applications"] = p.get("applications", 0) + 1
            p["confidence"] = min(1.0, p.get("confidence", 0.7) + 0.05)
            p["last_applied"] = datetime.now(timezone.utc).isoformat()
            with open(patterns_file, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2)
            return

    # New pattern — start at 0.7 confidence
    now = datetime.now(timezone.utc)
    pat_id = f"pat-{now.strftime('%Y-%m-%d')}-{len(data['patterns']) + 1:03d}"
    data["patterns"][pat_id] = {
        "id": pat_id,
        "name": episode.get("lesson", "")[:60],
        "source": episode.get("id", "manual"),
        "confidence": 0.7,
        "applications": 1,
        "created": now.strftime("%Y-%m-%d"),
        "last_applied": now.isoformat(),
        "category": "general",
        "pattern": episode.get("lesson", ""),
        "problem": episode.get("situation", ""),
        "solution": episode.get("solution", ""),
        "target_skills": [episode.get("skill", "unknown")],
    }

    with open(patterns_file, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)


def _text_similarity(a: str, b: str) -> float:
    """Simple Jaccard similarity on word tokens."""
    if not a or not b:
        return 0.0
    words_a = set(re.findall(r"\w+", a.lower()))
    words_b = set(re.findall(r"\w+", b.lower()))
    if not words_a or not words_b:
        return 0.0
    intersection = words_a & words_b
    union = words_a | words_b
    return len(intersection) / len(union)


def main():
    parser = argparse.ArgumentParser(description="DeerFlow Skill Management Tool")
    subparsers = parser.add_subparsers(dest="command", help="Command to execute")

    # list
    subparsers.add_parser("list", help="List all skills")

    # read
    p_read = subparsers.add_parser("read", help="Read a skill's SKILL.md")
    p_read.add_argument("skill_name")

    # create
    p_create = subparsers.add_parser("create", help="Create a new skill")
    p_create.add_argument("skill_name")
    p_create.add_argument("--description", required=True)
    p_create.add_argument("--content", required=True)
    p_create.add_argument("--category", default="custom", choices=["public", "custom"])

    # patch
    p_patch = subparsers.add_parser("patch", help="Patch a skill file")
    p_patch.add_argument("skill_name")
    p_patch.add_argument("--old", required=True, help="Text to find")
    p_patch.add_argument("--new", required=True, help="Text to replace with")

    # append
    p_append = subparsers.add_parser("append", help="Append to a skill section")
    p_append.add_argument("skill_name")
    p_append.add_argument("--section", required=True, help="Section header (e.g. '## Rules')")
    p_append.add_argument("--content", required=True, help="Content to append")

    # log
    p_log = subparsers.add_parser("log", help="Log an episode")
    p_log.add_argument("skill_name")
    p_log.add_argument("--episode", required=True, help="JSON episode data")

    # patterns
    p_patterns = subparsers.add_parser("patterns", help="Show learned patterns")
    p_patterns.add_argument("--skill", help="Filter by skill name")

    # search
    p_search = subparsers.add_parser("search", help="Search episodic memory")
    p_search.add_argument("query")

    args = parser.parse_args()
    if not args.command:
        parser.print_help()
        sys.exit(1)

    commands = {
        "list": cmd_list,
        "read": cmd_read,
        "create": cmd_create,
        "patch": cmd_patch,
        "append": cmd_append,
        "log": cmd_log,
        "patterns": cmd_patterns,
        "search": cmd_search,
    }
    commands[args.command](args)


if __name__ == "__main__":
    main()
