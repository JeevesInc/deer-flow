"""
Wiki layer — the OpenWiki-Brains-style memory store.
=====================================================
Unlike STRATEGIC_CONTEXT.md (auto-injected every turn, so bounded by context
size) and mem0 (semantic/vector recall, so probabilistic), the wiki is a
directory of evergreen Markdown pages on disk:

  - No size limit — one page per counterparty/deal/repo/topic.
  - Grep-able — exact-match full text search, not embedding similarity.
  - Evergreen — pages are MERGED on update (old facts kept unless
    superseded), not blindly overwritten and not append-only logs.
  - Not auto-injected — the agent pulls from it on demand via wiki_search.py,
    so it never competes for context budget the way STRATEGIC_CONTEXT.md does.

Directory layout (under DEER_FLOW_DIR / "wiki"):

  wiki/
    index.md                     <- auto-generated table of contents
    counterparties/<slug>.md     <- one evergreen page per counterparty/lender
    deals/<slug>.md              <- one evergreen page per active facility/deal
    repos/<slug>.md              <- synthesized summary of a tracked git repo
    topics/<slug>.md             <- cross-cutting topics (e.g. MX Servicer Report)

Each page has two parts:
  1. A merged "living" body (facts as currently understood — kept current).
  2. A "## Update Log" tail section (dated one-liners, newest last) so you can
     still see how understanding evolved, without that history bloating the
     living body.
"""

import json
import logging
import os
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
DEER_FLOW_DIR = Path(os.environ.get(
    "DEER_FLOW_DATA_DIR",
    str(SCRIPT_DIR.parent.parent.parent / "backend" / ".deer-flow"),
))
WIKI_DIR = DEER_FLOW_DIR / "wiki"

_shared = SCRIPT_DIR.parent / "_shared"
if str(_shared) not in sys.path and _shared.exists():
    sys.path.insert(0, str(_shared))

log = logging.getLogger("knowledge-crawler.wiki")

VALID_CATEGORIES = {"counterparties", "deals", "repos", "topics"}


def _slugify(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")
    return slug or "untitled"


def _page_path(category: str, name: str) -> Path:
    if category not in VALID_CATEGORIES:
        raise ValueError(f"Unknown wiki category '{category}', expected one of {VALID_CATEGORIES}")
    return WIKI_DIR / category / f"{_slugify(name)}.md"


def read_page(category: str, name: str) -> str:
    path = _page_path(category, name)
    if path.exists():
        return path.read_text(encoding="utf-8")
    return ""


def _split_page(text: str) -> tuple:
    """Split a page into (living_body, update_log_tail)."""
    marker = "## Update Log"
    if marker in text:
        idx = text.index(marker)
        return text[:idx].rstrip() + "\n", text[idx:].rstrip() + "\n"
    return (text.rstrip() + "\n" if text.strip() else ""), f"{marker}\n"


def update_page(category: str, name: str, new_facts: str, source_label: str, title: str = None) -> Path:
    """
    Merge new_facts into the evergreen page for (category, name).

    - If Anthropic is reachable, uses Claude to merge intelligently (supersede
      stale facts, keep what's still true, avoid duplication).
    - Falls back to a plain append under Update Log if merge fails so nothing
      is ever lost.

    Always appends a dated one-liner to the Update Log tail regardless.
    """
    path = _page_path(category, name)
    path.parent.mkdir(parents=True, exist_ok=True)
    existing = read_page(category, name)
    living, log_tail = _split_page(existing)
    ts = datetime.now().strftime("%Y-%m-%d %H:%M")
    display_title = title or name

    try:
        merged_living = _merge_with_claude(display_title, living, new_facts)
    except Exception as e:
        log.warning(f"Wiki merge via Claude failed ({e}); falling back to raw append")
        merged_living = (living + f"\n\n### {ts} update ({source_label})\n{new_facts}\n") if living else (
            f"# {display_title}\n\n{new_facts}\n"
        )

    log_entry = f"- {ts} ({source_label}): updated\n"
    new_log_tail = log_tail.rstrip() + "\n" + log_entry

    path.write_text(merged_living.rstrip() + "\n\n" + new_log_tail, encoding="utf-8")
    log.info(f"Wiki page updated: {category}/{path.name}")
    return path


def _merge_with_claude(title: str, living: str, new_facts: str) -> str:
    import anthropic
    client = anthropic.Anthropic()

    prompt = f"""You maintain an evergreen wiki page for Brian Mauck's Capital Markets AI assistant.
Page title: {title}

CURRENT PAGE BODY (living facts as currently understood — empty if this is a new page):
{living[:6000] if living else "(new page — none yet)"}

NEW INFORMATION TO MERGE IN:
{new_facts[:4000]}

Rewrite the FULL page body (markdown, starting with a "# {title}" header) by:
- Keeping facts that are still true
- Updating/superseding facts that changed (don't keep contradictory old and new versions side by side)
- Adding genuinely new facts, organized under sensible subheadings (e.g. Status, Key People, Open Items, Terms, Recent Activity)
- Being concise and factual — no filler, no repeated information
- NOT including an "Update Log" section — that is handled separately

Output ONLY the new page body markdown, nothing else."""

    response = client.messages.create(
        model="claude-opus-5-5",
        max_tokens=2000,
        messages=[{"role": "user", "content": prompt}],
    )
    from llm import text_from_response
    return text_from_response(response).strip()


def rebuild_index() -> Path:
    """Regenerate wiki/index.md — a table of contents across all categories."""
    WIKI_DIR.mkdir(parents=True, exist_ok=True)
    lines = ["# Wiki Index", "", f"_Auto-generated {datetime.now().strftime('%Y-%m-%d %H:%M')}_", ""]
    total = 0
    for category in sorted(VALID_CATEGORIES):
        cat_dir = WIKI_DIR / category
        if not cat_dir.exists():
            continue
        pages = sorted(cat_dir.glob("*.md"))
        if not pages:
            continue
        lines.append(f"## {category.title()}")
        for p in pages:
            title = p.stem.replace("_", " ").title()
            try:
                text = p.read_text(encoding="utf-8")
                m = re.search(r"^#\s+(.+)$", text, re.MULTILINE)
                if m:
                    title = m.group(1).strip()
            except Exception:
                pass
            mtime = datetime.fromtimestamp(p.stat().st_mtime).strftime("%Y-%m-%d")
            lines.append(f"- [{title}]({category}/{p.name}) — updated {mtime}")
            total += 1
        lines.append("")
    index_path = WIKI_DIR / "index.md"
    index_path.write_text("\n".join(lines), encoding="utf-8")
    log.info(f"Wiki index rebuilt: {total} pages")
    return index_path


def search(pattern: str, category: str = None, max_results: int = 40) -> list:
    """Grep the wiki for an exact-match pattern. Returns [{file, line_no, line}]."""
    target_dir = WIKI_DIR / category if category and category in VALID_CATEGORIES else WIKI_DIR
    if not target_dir.exists():
        return []
    results = []
    try:
        proc = subprocess.run(
            ["grep", "-rniH", "--include=*.md", pattern, str(target_dir)],
            capture_output=True, text=True, timeout=15,
        )
        # Windows paths carry a drive-letter colon (C:\...), so a naive
        # split(":", 2) mis-parses "C:\...:4:text" as file="C". Use a greedy
        # regex instead — greedy .* eats up to the LAST ":<digits>:" match,
        # which is always the real line-number separator grep inserts.
        line_re = re.compile(r"^(.*):(\d+):(.*)$")
        for line in proc.stdout.splitlines()[:max_results]:
            m = line_re.match(line)
            if not m:
                continue
            fpath, line_no, text = m.group(1), m.group(2), m.group(3)
            try:
                rel = Path(fpath).relative_to(WIKI_DIR)
            except ValueError:
                rel = Path(fpath)
            results.append({
                "file": str(rel),
                "line_no": int(line_no),
                "line": text.strip(),
            })
    except FileNotFoundError:
        pat_re = re.compile(re.escape(pattern), re.IGNORECASE)
        for md in target_dir.rglob("*.md"):
            try:
                for i, line in enumerate(md.read_text(encoding="utf-8").splitlines(), 1):
                    if pat_re.search(line):
                        results.append({"file": str(md.relative_to(WIKI_DIR)), "line_no": i, "line": line.strip()})
                        if len(results) >= max_results:
                            return results
            except Exception:
                continue
    except Exception as e:
        log.warning(f"Wiki search failed: {e}")
    return results
