#!/usr/bin/env python3
"""
CLI for the wiki layer — grep-based search and index management.

Usage:
  python wiki_search.py "Neuberger Berman covenant"
  python wiki_search.py "trust agreement" --category counterparties
  python wiki_search.py --rebuild-index
  python wiki_search.py --show counterparties/neuberger_berman
"""

import argparse
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))
import wiki  # noqa: E402


def main():
    # Owner-gated capability (2026-08-28). Non-owner Slack users may ask
    # data questions and work with files, but never touch Brian's private
    # surfaces. See skills/custom/_shared/actor_guard.py.
    import os as _og_os, sys as _og_sys
    _og_sys.path.insert(0, _og_os.path.join(_og_os.path.dirname(
        _og_os.path.dirname(_og_os.path.abspath(__file__))), '_shared'))
    from actor_guard import require_owner as _require_owner
    _require_owner("Brian's private knowledge wiki")

    parser = argparse.ArgumentParser(description="Search / manage the wiki layer")
    parser.add_argument("query", nargs="?", help="Search term (grep, case-insensitive)")
    parser.add_argument("--category", choices=sorted(wiki.VALID_CATEGORIES))
    parser.add_argument("--rebuild-index", action="store_true")
    parser.add_argument("--show", help="Print a specific page, e.g. counterparties/neuberger_berman")
    args = parser.parse_args()

    if args.rebuild_index:
        path = wiki.rebuild_index()
        print(f"Index rebuilt: {path}")
        return

    if args.show:
        parts = args.show.split("/")
        if len(parts) != 2:
            print("Use format: <category>/<slug>")
            sys.exit(1)
        category, slug = parts
        content = wiki.read_page(category, slug.replace(".md", ""))
        if not content:
            print(f"No page found at {args.show}")
            sys.exit(1)
        print(content)
        return

    if not args.query:
        parser.print_help()
        sys.exit(1)

    results = wiki.search(args.query, category=args.category)
    if not results:
        print(f"No matches for '{args.query}'")
        return
    for r in results:
        print(f"{r['file']}:{r['line_no']}: {r['line']}")
    print(f"\n{len(results)} matches")


if __name__ == "__main__":
    main()
