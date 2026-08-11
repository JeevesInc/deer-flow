"""
Repo connector — ingests git repositories into the wiki (repos/ category).

Mirrors OpenWiki's `git-repo` connector: reads recent commits and current
file listing for each tracked repo, then synthesizes a living summary page
under wiki/repos/<repo>.md (what the repo is, recent changes, structure).

Tracked repos are configured on disk at KNOWLEDGE_DIR/_repo_registry.json,
seeded with cfo-org-kb (the canonical SQL/analytics query source of truth
that Brian commits every new Redshift query to).
"""

import json
import logging
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))
import wiki  # noqa: E402
from wiki import DEER_FLOW_DIR  # noqa: E402

KNOWLEDGE_DIR = DEER_FLOW_DIR / "knowledge"
REPO_REGISTRY_FILE = KNOWLEDGE_DIR / "_repo_registry.json"
REPO_STATE_FILE = KNOWLEDGE_DIR / "_repo_crawl_state.json"

log = logging.getLogger("knowledge-crawler.repos")

# path is resolved relative to the skills root (SCRIPT_DIR.parent == custom/)
_SEED_REPOS = {
    "cfo-org-kb": {
        "path": "cfo-org-kb",
        "description": "Canonical SQL query / analytics script source of truth for CFO org",
    },
}


def load_repo_registry() -> dict:
    registry = dict(_SEED_REPOS)
    if REPO_REGISTRY_FILE.exists():
        try:
            disk = json.loads(REPO_REGISTRY_FILE.read_text(encoding="utf-8"))
            registry.update(disk)
        except Exception as e:
            log.warning(f"Failed to load repo registry: {e}")
    return registry


def save_repo_registry(registry: dict):
    KNOWLEDGE_DIR.mkdir(parents=True, exist_ok=True)
    REPO_REGISTRY_FILE.write_text(json.dumps(registry, indent=2), encoding="utf-8")


def _load_state() -> dict:
    if REPO_STATE_FILE.exists():
        return json.loads(REPO_STATE_FILE.read_text(encoding="utf-8"))
    return {}


def _save_state(state: dict):
    KNOWLEDGE_DIR.mkdir(parents=True, exist_ok=True)
    REPO_STATE_FILE.write_text(json.dumps(state, indent=2, default=str), encoding="utf-8")


def _run_git(repo_dir: Path, args: list, timeout: int = 30) -> str:
    try:
        proc = subprocess.run(
            ["git"] + args, cwd=str(repo_dir), capture_output=True, text=True, timeout=timeout,
        )
        return proc.stdout.strip()
    except subprocess.TimeoutExpired:
        log.warning(f"git {' '.join(args)} timed out in {repo_dir} after {timeout}s (likely a stalled credential/network prompt) — skipping")
        return ""
    except Exception as e:
        log.warning(f"git {' '.join(args)} failed in {repo_dir}: {e}")
        return ""


def crawl_repo(name: str, info: dict, since_iso: str = None) -> dict:
    """Pull latest and gather recent commits + top-level structure for one repo."""
    repo_dir = SCRIPT_DIR.parent / info["path"]
    if not repo_dir.exists():
        log.warning(f"Repo path not found: {repo_dir}")
        return {}

    # NOTE: deliberately does NOT run `git pull` — on this host it can hang on
    # a credential/GUI auth prompt that a subprocess timeout can't reliably
    # kill. Pulling is already a manual step in the cfo-org-kb workflow
    # (SOUL.md: "cd .../cfo-org-kb && git pull" before kb_search.py); this
    # connector just reads whatever is currently checked out locally.
    since_arg = [f"--since={since_iso}"] if since_iso else ["-20"]
    log_out = _run_git(repo_dir, ["log", "--date=short", "--pretty=%ad | %an | %s"] + since_arg)

    # Top-level file/dir listing for structure context
    listing = _run_git(repo_dir, ["ls-tree", "-r", "--name-only", "HEAD"])
    files = [f for f in listing.splitlines() if f]
    top_level = sorted({f.split("/")[0] for f in files})

    return {
        "recent_commits": log_out,
        "num_files": len(files),
        "top_level": top_level,
    }


def run_repo_crawl(force: bool = False) -> int:
    registry = load_repo_registry()
    state = _load_state()
    total_updated = 0

    for name, info in registry.items():
        last = state.get(name, {}).get("last_crawled")
        if not force and last:
            last_dt = datetime.fromisoformat(last)
            if datetime.now(timezone.utc) - last_dt < __import__("datetime").timedelta(hours=24):
                continue

        data = crawl_repo(name, info, since_iso=last)
        if not data or not data.get("recent_commits"):
            state[name] = {"last_crawled": datetime.now(timezone.utc).isoformat()}
            continue

        facts = (
            f"Description: {info.get('description', '')}\n"
            f"Files tracked: {data['num_files']}\n"
            f"Top-level structure: {', '.join(data['top_level'][:20])}\n\n"
            f"Recent commits:\n{data['recent_commits']}"
        )
        wiki.update_page("repos", name, facts, source_label="git-repo-connector", title=f"Repo: {name}")
        total_updated += 1
        state[name] = {"last_crawled": datetime.now(timezone.utc).isoformat()}

    _save_state(state)
    if total_updated:
        wiki.rebuild_index()
    return total_updated


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Git repo -> wiki connector")
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--add", help="Register a new repo: name:relative_path:description")
    args = parser.parse_args()

    if args.add:
        parts = args.add.split(":", 2)
        if len(parts) < 2:
            print("Usage: --add name:relative_path[:description]")
            sys.exit(1)
        name, path = parts[0], parts[1]
        desc = parts[2] if len(parts) > 2 else ""
        registry = load_repo_registry()
        registry[name] = {"path": path, "description": desc}
        save_repo_registry(registry)
        print(f"Registered repo: {name} -> {path}")
        sys.exit(0)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    updated = run_repo_crawl(force=args.force)
    print(f"Repo crawl complete: {updated} repos updated")
