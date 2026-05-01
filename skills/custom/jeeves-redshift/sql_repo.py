#!/usr/bin/env python3
"""SQL query repository — save, search, and reuse successful queries.

Usage:
    python sql_repo.py list                          # List all saved queries
    python sql_repo.py search "balance by country"   # Search by keyword
    python sql_repo.py get "total_portfolio_balance"  # Get a specific query
    python sql_repo.py save "name" "SELECT ..." [--tags tag1,tag2] [--description "..."]
    python sql_repo.py delete "name"                 # Remove a saved query
    python sql_repo.py run "name" [--output ...]     # Run a saved query directly

Queries are stored in .deer-flow/sql_repo/ as individual JSON files.
"""

import json
import os
import re
import sys
import tempfile
from datetime import datetime
from pathlib import Path


def _repo_dir():
    """Return the SQL repo directory, creating it if needed."""
    base = os.environ.get('SQL_REPO_PATH', '')
    if not base:
        # Default: .deer-flow/sql_repo/ relative to the backend dir
        backend = Path(__file__).resolve().parent.parent.parent.parent / 'backend' / '.deer-flow' / 'sql_repo'
        base = str(backend)
    os.makedirs(base, exist_ok=True)
    return base


def _slug(name):
    """Convert a query name to a safe filename."""
    return re.sub(r'[^a-zA-Z0-9_-]', '_', name.lower().strip()).strip('_')


def _query_path(name):
    return os.path.join(_repo_dir(), f'{_slug(name)}.json')


def _atomic_write(path, data):
    dir_name = os.path.dirname(path)
    fd, tmp = tempfile.mkstemp(suffix='.tmp', dir=dir_name)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            json.dump(data, f, indent=2, default=str)
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


# ---------------------------------------------------------------------------
# Public API (importable from sql_runner.py)
# ---------------------------------------------------------------------------

def save_query(name, sql, tags=None, description=None):
    """Save a query to the repo."""
    path = _query_path(name)
    existing = None
    if os.path.exists(path):
        with open(path, 'r', encoding='utf-8') as f:
            existing = json.load(f)

    data = {
        'name': name,
        'slug': _slug(name),
        'sql': sql.strip(),
        'tags': tags or (existing or {}).get('tags', []),
        'description': description or (existing or {}).get('description', ''),
        'created_at': (existing or {}).get('created_at', datetime.now().isoformat()),
        'updated_at': datetime.now().isoformat(),
        'use_count': (existing or {}).get('use_count', 0),
        'last_used': (existing or {}).get('last_used'),
    }
    _atomic_write(path, data)
    return data


def get_query(name):
    """Get a saved query by name. Returns None if not found."""
    path = _query_path(name)
    if not os.path.exists(path):
        return None
    with open(path, 'r', encoding='utf-8') as f:
        return json.load(f)


def record_use(name):
    """Increment use_count and update last_used for a query."""
    data = get_query(name)
    if data:
        data['use_count'] = data.get('use_count', 0) + 1
        data['last_used'] = datetime.now().isoformat()
        _atomic_write(_query_path(name), data)


def list_queries():
    """List all saved queries. Returns list of dicts."""
    repo = _repo_dir()
    queries = []
    for fname in sorted(os.listdir(repo)):
        if not fname.endswith('.json'):
            continue
        path = os.path.join(repo, fname)
        try:
            with open(path, 'r', encoding='utf-8') as f:
                data = json.load(f)
            queries.append(data)
        except Exception as e:
            print(f"Warning: skipping malformed query file {fname}: {e}", file=sys.stderr)
    return queries


def search_queries(keyword):
    """Search queries by keyword across name, description, tags, and SQL."""
    keyword_lower = keyword.lower()
    results = []
    for q in list_queries():
        searchable = ' '.join([
            q.get('name', ''),
            q.get('description', ''),
            ' '.join(q.get('tags', [])),
            q.get('sql', ''),
        ]).lower()
        if keyword_lower in searchable:
            results.append(q)
    return results


def delete_query(name):
    """Delete a saved query."""
    path = _query_path(name)
    if os.path.exists(path):
        os.unlink(path)
        return True
    return False


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')

    if len(sys.argv) < 2:
        print("Usage: python sql_repo.py <command> [args]")
        print("Commands: list, search, get, save, delete, run")
        sys.exit(1)

    cmd = sys.argv[1]

    if cmd == 'list':
        queries = list_queries()
        if not queries:
            print("No saved queries in the repo.")
            print(f"  Repo dir: {_repo_dir()}")
            return
        print(f"SQL Repo: {len(queries)} saved query(ies)\n")
        # Sort by use_count desc, then updated_at desc
        queries.sort(key=lambda q: (q.get('use_count', 0), q.get('updated_at', '')), reverse=True)
        for q in queries:
            tags = ', '.join(q.get('tags', [])) or 'none'
            uses = q.get('use_count', 0)
            updated = q.get('updated_at', '?')[:10]
            desc = q.get('description', '')
            desc_str = f" — {desc}" if desc else ''
            print(f"  {q['name']}  (uses: {uses}, updated: {updated}, tags: {tags}){desc_str}")
        print(f"\nUse 'get <name>' to see the full SQL.")

    elif cmd == 'search':
        if len(sys.argv) < 3:
            print("Usage: python sql_repo.py search \"keyword\"", file=sys.stderr)
            sys.exit(1)
        keyword = sys.argv[2]
        results = search_queries(keyword)
        if not results:
            print(f"No queries matching: {keyword}")
            return
        print(f"Found {len(results)} query(ies) matching '{keyword}':\n")
        for q in results:
            print(f"  {q['name']}")
            print(f"    {q.get('description', '(no description)')}")
            # Show first 2 lines of SQL
            sql_lines = q.get('sql', '').strip().split('\n')
            for line in sql_lines[:2]:
                print(f"    {line}")
            if len(sql_lines) > 2:
                print(f"    ... ({len(sql_lines)} lines total)")
            print()

    elif cmd == 'get':
        if len(sys.argv) < 3:
            print("Usage: python sql_repo.py get \"name\"", file=sys.stderr)
            sys.exit(1)
        name = sys.argv[2]
        q = get_query(name)
        if not q:
            print(f"Query not found: {name}")
            # Suggest similar
            all_q = list_queries()
            suggestions = [qr['name'] for qr in all_q if name.lower() in qr.get('name', '').lower()]
            if suggestions:
                print(f"  Did you mean: {', '.join(suggestions)}")
            sys.exit(1)
        print(f"Name: {q['name']}")
        if q.get('description'):
            print(f"Description: {q['description']}")
        if q.get('tags'):
            print(f"Tags: {', '.join(q['tags'])}")
        print(f"Uses: {q.get('use_count', 0)} | Updated: {q.get('updated_at', '?')[:10]}")
        print(f"\nSQL:\n{q['sql']}")

    elif cmd == 'save':
        if len(sys.argv) < 4:
            print('Usage: python sql_repo.py save "name" "SELECT ..." [--tags t1,t2] [--description "..."]', file=sys.stderr)
            sys.exit(1)
        name = sys.argv[2]
        sql = sys.argv[3]
        tags = []
        description = ''
        i = 4
        while i < len(sys.argv):
            if sys.argv[i] == '--tags' and i + 1 < len(sys.argv):
                tags = [t.strip() for t in sys.argv[i + 1].split(',')]
                i += 2
            elif sys.argv[i] == '--description' and i + 1 < len(sys.argv):
                description = sys.argv[i + 1]
                i += 2
            else:
                i += 1
        data = save_query(name, sql, tags=tags, description=description)
        print(f"Saved: {data['name']} ({_query_path(name)})")

    elif cmd == 'delete':
        if len(sys.argv) < 3:
            print("Usage: python sql_repo.py delete \"name\"", file=sys.stderr)
            sys.exit(1)
        if delete_query(sys.argv[2]):
            print(f"Deleted: {sys.argv[2]}")
        else:
            print(f"Not found: {sys.argv[2]}")
            sys.exit(1)

    elif cmd == 'run':
        if len(sys.argv) < 3:
            print("Usage: python sql_repo.py run \"name\" [--output ...] [--limit ...]", file=sys.stderr)
            sys.exit(1)
        name = sys.argv[2]
        q = get_query(name)
        if not q:
            print(f"Query not found: {name}", file=sys.stderr)
            sys.exit(1)
        record_use(name)
        # Delegate to sql_runner with the saved SQL
        import subprocess
        runner = os.path.join(os.path.dirname(__file__), 'sql_runner.py')
        extra_args = sys.argv[3:]  # pass through --output, --limit, etc.
        result = subprocess.run(
            [sys.executable, runner, q['sql']] + extra_args,
            env={**os.environ},
        )
        sys.exit(result.returncode)

    else:
        print(f"Unknown command: {cmd}", file=sys.stderr)
        sys.exit(1)


if __name__ == '__main__':
    main()
