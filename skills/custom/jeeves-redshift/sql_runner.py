#!/usr/bin/env python3
"""Jeeves Redshift SQL runner — validates, executes, and formats query results.

Usage:
    python sql_runner.py "SELECT ..."
    python sql_runner.py --file query.sql
    python sql_runner.py "SELECT ..." --output results.xlsx
    python sql_runner.py "SELECT ..." --output results.csv
    python sql_runner.py "SELECT ..." --limit 500

Features:
    - Pre-execution validation (table names, common mistakes, date guards)
    - Automatic connection handling
    - Smart output: inline for ≤20 rows, Excel/CSV for larger results
    - Warnings printed to stderr so the agent can self-correct
"""

import argparse
import datetime as dt
import os
import re
import sys
import threading
import time

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))

# ---------------------------------------------------------------------------
# Rate limiting (per-process, thread-safe)
# ---------------------------------------------------------------------------

_last_query_time: float = 0.0
_rate_lock = threading.Lock()
QUERY_MIN_INTERVAL = 5.0  # seconds between queries

# ---------------------------------------------------------------------------
# Known schema
# ---------------------------------------------------------------------------

ALLOWED_TABLES = {
    'capital_markets_dm.loc_tape',
    'capital_markets_dm.gwc_tape',
    'master_transactions_dm.transactions_ssot',
    'master_customer_dm.companies_dm',
    'analytics_sandbox.loc_vintage_data',
    'analytics_sandbox.jeeves_unified_risk_scoring_final',
    'dms_mysql_jeeves_raw.companies',
    'dms_mysql_jeeves_raw.company_settings',
    'dms_mysql_jeeves_raw.instalments',
    'dms_mysql_jeeves_raw.cities',
    'dms_mysql_jeeves_raw.company_addresses',
    'dms_mysql_jeeves_raw.company_statements',
    'dms_mysql_jeeves_raw.collateral_records',
    'dms_mysql_underwriting_raw.taktile_data',
    'master_dimensions.currency_lookup_dim',
    # Added 2026-08-04 (Dream #80): dms_mysql_jeeves_raw.states (id -> statename) is REAL and
    # confirmed; it was missing from this list while its sibling .cities was present, which is
    # what pushed the agent to guess the nonexistent .state_lookup / .country_lookup names and
    # broke the canonical AIG invoice query twice (2026-07-21, 2026-08-03).
    'dms_mysql_jeeves_raw.states',
    'analytics_sandbox.jurs_test',
    'capital_markets_dm.rms_transactions',
    'capital_markets_dm.intraday_collections',
    'dms_mysql_jeeves_raw.satws_invoices',
    'dms_mysql_jeeves_raw.naics_industries',
    'analytics_sandbox.company_to_industry',
    'analytics_sandbox.industry_backfill',
}
# Added 2026-08-20 (Dream #93): the three industry-waterfall tables behind the CANONICAL,
# committed KB query cfo-org-kb/sql/mastercard_spend_by_org_id.sql (commit a3f0324). Rejected at
# runtime on 2026-08-19 during Alex's Mastercard follow-up, costing an --allow-tables retry --
# the same lagging-whitelist pattern as Dreams #60 and #63 (third recurrence). A table that a
# committed cfo-org-kb query depends on belongs in this list; when you add a query that needs a
# new table, add it here in the same change.
# Added 2026-07-18 (Dream #63): jurs_test/rms_transactions/intraday_collections/satws_invoices
# are all documented in this skill's own SKILL.md (Additional Tables section + worked example
# queries) but were missing from the whitelist -- same lagging-whitelist pattern as Dream #60.
# rms_transactions specifically was needed for the 2026-07-17 Kroll currency rebuild and had to
# be re-approved via --allow-tables. NOTE: dms_mysql_jeeves_raw has a much longer "Full Table
# List (209 tables)" reference further down in SKILL.md -- those are NOT whitelisted here by
# design (scope decision, not an oversight); flagged to Brian separately if broader access is
# ever needed.
# Added 2026-07-15 (Dream #60): cities/company_addresses/company_statements/collateral_records
# are all documented in this skill's own SKILL.md schema reference but were rejected at runtime
# as "not in the allowed list" across 3+ threads (2026-07-13/14, incl. AIG TEX file validation) --
# whitelist was lagging the documented reference, not a real access restriction.
# Added 2026-07-18 (Dream #63): master_dimensions.currency_lookup_dim resolves numeric ->
# alpha currency codes (join on cur.numeric_code = t.local_currency from rms_transactions).
# Confirmed correct table for "customer currency" asks. Do NOT guess
# dms_mysql_jeeves_raw.country_lookup / currency_lookup / state_lookup -- those do not
# exist and burned an --allow-tables retry loop on 2026-07-17 (Kroll payment tracing file).

DEPRECATED_COLUMNS = {
    'v0_charge_off_amount_usd': 'Use balance_usd WHERE dt = charge_off_dt instead',
    'v0_charge_off_cumulative_amount_usd': 'Use balance_usd WHERE dt = charge_off_dt instead',
}

# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

def _warn(msg: str) -> None:
    print(f"⚠ WARNING: {msg}", file=sys.stderr)


def _error(msg: str) -> None:
    print(f"✗ ERROR: {msg}", file=sys.stderr)


def _strip_comments(sql: str, strip_strings: bool = False) -> str:
    """Remove -- and /* */ comments (and optionally 'string literals').

    Guards must not be fooled by keywords/semicolons hidden in comments, nor
    fire on dates/semicolons inside string literals.
    """
    out = []
    i, n = 0, len(sql)
    while i < n:
        two = sql[i:i + 2]
        if two == '--':
            j = sql.find('\n', i)
            i = n if j == -1 else j
        elif two == '/*':
            j = sql.find('*/', i + 2)
            i = n if j == -1 else j + 2
            out.append(' ')
        elif sql[i] == "'":
            j = i + 1
            while j < n:
                if sql[j] == "'":
                    if sql[j + 1:j + 2] == "'":  # escaped ''
                        j += 2
                        continue
                    break
                j += 1
            if strip_strings:
                out.append("''")
            else:
                out.append(sql[i:j + 1])
            i = j + 1
        else:
            out.append(sql[i])
            i += 1
    return ''.join(out)


# Schemas the agent may always introspect (catalog/metadata, read-only views).
_SYSTEM_SCHEMAS = {'information_schema', 'pg_catalog', 'pg_internal'}

# Today-valued SQL functions. Bare use means "query today's data", which does
# not exist yet (Redshift lags a day); yesterday-relative arithmetic is fine.
_TODAY_FUNCS = re.compile(r'(?i)\b(CURRENT_DATE|CURRENT_TIMESTAMP|GETDATE\s*\(\s*\)|SYSDATE|NOW\s*\(\s*\))')


def _bare_today_func_uses(cleaned: str) -> list[str]:
    """Occurrences of CURRENT_DATE/GETDATE()/... NOT part of a minus-offset."""
    bad = []
    for m in _TODAY_FUNCS.finditer(cleaned):
        if re.match(r'\s*-', cleaned[m.end():]):
            continue  # CURRENT_DATE - 1 etc.
        before = cleaned[max(0, m.start() - 80):m.start()]
        if re.search(r"(?i)DATE_?ADD\s*\(\s*'?\w+'?\s*,\s*-\s*\d+\s*,\s*$", before):
            continue  # DATEADD(day, -1, CURRENT_DATE)
        bad.append(m.group(1).strip())
    return bad


def validate_sql(sql: str, allow_today: bool = False, extra_tables=None) -> list[str]:
    """Validate SQL and return list of warnings. Returns errors as strings prefixed with 'ERROR:'.

    allow_today: downgrade the today/future-date and bare-CURRENT_DATE errors
        to warnings (--allow-today).
    extra_tables: iterable of extra allowed 'schema.table' names, or the
        string '*' to skip whitelist enforcement (--allow-tables).
    """
    warnings = []
    sql_lower = sql.lower()
    # Comments removed for all structural checks; strings additionally removed
    # where a quoted literal could hide a semicolon or a fake table name.
    no_comments = _strip_comments(sql)
    cleaned = _strip_comments(sql, strip_strings=True)

    # Block non-SELECT statements
    # Strip leading WITH (CTE) to find the actual statement type.
    # NOTE: previously used a non-greedy regex `WITH\s+.*?\)` which stops at the
    # FIRST closing paren -- this breaks on any CTE body containing nested parens
    # (e.g. SUM(CASE WHEN ... THEN x ELSE 0 END), ROUND(...), etc.), which is
    # extremely common in real queries (e.g. a "cash_paid" aggregate). It would
    # truncate mid-expression and misidentify a fragment like "AS" or ",0)" as
    # the leading keyword, incorrectly rejecting valid SELECT/CTE queries.
    # Fixed by walking the string and tracking paren depth to find the true end
    # of each top-level CTE definition, then repeating for chained CTEs
    # (WITH a AS (...), b AS (...) SELECT ...).
    def _strip_leading_ctes(text: str) -> str:
        s = text.lstrip()
        if not re.match(r'(?i)^WITH\s', s):
            return s
        s = re.sub(r'(?i)^WITH\s+', '', s, count=1)
        while True:
            m = re.match(r'(?i)^\s*[a-zA-Z0-9_]+\s*(\([^()]*\)\s*)?AS\s*\(', s)
            if not m:
                break
            depth = 1
            i = m.end()
            while i < len(s) and depth > 0:
                if s[i] == '(':
                    depth += 1
                elif s[i] == ')':
                    depth -= 1
                i += 1
            s = s[i:].lstrip()
            if s.startswith(','):
                s = s[1:].lstrip()
                continue
            break
        return s

    stripped = _strip_leading_ctes(cleaned)
    first_keyword = stripped.strip().split()[0].upper() if stripped.strip() else ''
    if first_keyword and first_keyword not in ('SELECT', 'WITH', 'EXPLAIN'):
        return [f"ERROR: Only SELECT queries are allowed. Got: {first_keyword}"]

    # Multi-statement injection: the first-keyword check above only sees the
    # FIRST statement, so 'SELECT 1; DELETE ...' used to pass validation and
    # rely solely on DB grants. A trailing ';' is fine; content after one is not.
    body = cleaned.rstrip().rstrip(';')
    if ';' in body:
        return ["ERROR: Multiple SQL statements are not allowed — content found after a ';'. "
                "Run one statement at a time."]

    # Table whitelist (enforced; was advisory-only). Qualified schema.table
    # references only — unqualified FROM targets are CTE names.
    if extra_tables != '*':
        allowed = {t.lower() for t in ALLOWED_TABLES}
        if extra_tables:
            allowed |= {t.strip().lower() for t in extra_tables if t.strip()}
        refs = {r.lower() for r in re.findall(
            r'(?i)\b(?:FROM|JOIN)\s+([a-zA-Z_][\w$]*\.[a-zA-Z_][\w$]*)', cleaned)}
        unknown = sorted(r for r in refs
                         if r not in allowed and r.split('.')[0] not in _SYSTEM_SCHEMAS)
        if unknown:
            return ["ERROR: Table(s) not in the allowed list: " + ', '.join(unknown)
                    + ". Allowed: " + ', '.join(sorted(ALLOWED_TABLES))
                    + ". If a table is genuinely needed, re-run with "
                    + "--allow-tables <schema.table>[,...] (or --allow-tables '*')."]

    # Check for deprecated columns
    for col, fix in DEPRECATED_COLUMNS.items():
        if col in sql_lower:
            warnings.append(f"Deprecated column '{col}' used. {fix}")

    # Check DATE_TRUNC on dt column
    if re.search(r"DATE_TRUNC\s*\([^)]*['\"]month['\"][^)]*\bdt\b", sql, re.IGNORECASE):
        warnings.append("DATE_TRUNC on dt column detected. Use 'dt BETWEEN ... AND ...' instead for loc_tape/gwc_tape.")

    # Today/future dates and today-valued functions are ERRORS (were a warning
    # matching only the literal string): data lags a day, so today-dated tape /
    # borrowing-base queries return nulls that cascade into wrong eligibility.
    today = dt.date.today()
    yesterday = (today - dt.timedelta(days=1)).isoformat()
    bad_dates = []
    for lit in re.findall(r"'(\d{4}-\d{2}-\d{2})", no_comments):
        try:
            if dt.date.fromisoformat(lit) >= today:
                bad_dates.append(lit)
        except ValueError:
            pass
    bad_funcs = _bare_today_func_uses(cleaned)
    if bad_dates or bad_funcs:
        what = ', '.join(sorted(set(bad_dates)) + sorted(set(bad_funcs)))
        msg = (f"Today/future date reference ({what}). Redshift data is only available "
               f"through yesterday ({yesterday}) — today-dated rows don't exist and "
               f"null-cascade into eligibility fields. Use '{yesterday}' or "
               f"CURRENT_DATE - 1"
               + ("." if allow_today else ", or re-run with --allow-today if intentional."))
        if allow_today:
            warnings.append(msg)
        else:
            return ["ERROR: " + msg]

    # Check loc_tape queries for missing filters
    if 'loc_tape' in sql_lower:
        # Only warn if this looks like a portfolio query (not a charge-off lookup)
        if 'charge_off_dt' not in sql_lower and 'charge_off_flag' not in sql_lower:
            warnings.append("loc_tape query missing 'charge_off_flag = false' filter. Active portfolio queries should always include this.")
        if 'is_in_repayment' not in sql_lower:
            warnings.append("loc_tape query missing 'is_in_repayment = false' filter. Active portfolio queries should always include this.")

    # Check transactions_ssot for missing test filter
    if 'transactions_ssot' in sql_lower and 'is_company_test' not in sql_lower:
        warnings.append("transactions_ssot query missing 'is_company_test = false' filter. Results may include test data.")

    return warnings


# ---------------------------------------------------------------------------
# Connection
# ---------------------------------------------------------------------------

def _ensure_deps():
    try:
        import psycopg2  # noqa: F401
    except ImportError:
        import subprocess
        subprocess.check_call([sys.executable, '-m', 'pip', 'install', '-q', 'psycopg2-binary'])


_pool = None
_pool_lock = threading.Lock()
STATEMENT_TIMEOUT_MS = 60_000  # 60 seconds


def _get_pool():
    """Get or create the connection pool (singleton, thread-safe)."""
    global _pool
    if _pool is not None:
        return _pool
    with _pool_lock:
        if _pool is not None:
            return _pool
        from psycopg2 import pool as pg_pool
        _pool = pg_pool.SimpleConnectionPool(
            minconn=1,
            maxconn=5,
            host=os.environ['REDSHIFT_HOST'],
            port=int(os.environ['REDSHIFT_PORT']),
            dbname=os.environ['REDSHIFT_DB'],
            user=os.environ['REDSHIFT_USER'],
            password=os.environ['REDSHIFT_PASSWORD'],
            sslmode='require',
            sslrootcert='disable',
            options=f'-c statement_timeout={STATEMENT_TIMEOUT_MS}',
        )
        return _pool


def _connect():
    """Get a healthy connection from the pool, replacing stale ones."""
    global _pool
    pool = _get_pool()
    conn = pool.getconn()
    try:
        # Quick health check — catches stale SSL / dropped connections
        conn.cursor().execute('SELECT 1')
        return conn
    except Exception:
        # Connection is dead — discard it and force a fresh pool
        try:
            pool.putconn(conn, close=True)
        except Exception:
            pass
        with _pool_lock:
            try:
                _pool.closeall()
            except Exception:
                pass
            _pool = None
        # Retry with a brand-new pool
        return _get_pool().getconn()


def _release(conn):
    """Return a connection to the pool."""
    try:
        if _pool is not None:
            _pool.putconn(conn)
    except Exception:
        pass


def _rate_limit():
    """Enforce minimum interval between queries."""
    global _last_query_time
    with _rate_lock:
        now = time.monotonic()
        elapsed = now - _last_query_time
        if elapsed < QUERY_MIN_INTERVAL and _last_query_time > 0:
            wait = QUERY_MIN_INTERVAL - elapsed
            _warn(f"Rate limiting: waiting {wait:.1f}s before next query")
            time.sleep(wait)
        _last_query_time = time.monotonic()


# ---------------------------------------------------------------------------
# Output formatting
# ---------------------------------------------------------------------------

def _format_inline(columns: list[str], rows: list[tuple], limit: int) -> str:
    """Format results as a readable table for inline display."""
    if not rows:
        return "(0 rows returned)"

    truncated = len(rows) > limit
    display_rows = rows[:limit]

    # Calculate column widths
    widths = [len(c) for c in columns]
    for row in display_rows:
        for i, val in enumerate(row):
            widths[i] = max(widths[i], len(str(val) if val is not None else 'NULL'))

    # Cap widths at 40 chars
    widths = [min(w, 40) for w in widths]

    lines = []
    # Header
    header = ' | '.join(c.ljust(widths[i]) for i, c in enumerate(columns))
    lines.append(header)
    lines.append('-+-'.join('-' * w for w in widths))

    # Rows
    for row in display_rows:
        vals = []
        for i, val in enumerate(row):
            s = str(val) if val is not None else 'NULL'
            if len(s) > 40:
                s = s[:37] + '...'
            vals.append(s.ljust(widths[i]))
        lines.append(' | '.join(vals))

    result = '\n'.join(lines)
    if truncated:
        result += f"\n\n... showing {limit} of {len(rows)} rows. Use --output to save all rows."

    return result


def _save_excel(columns: list[str], rows: list[tuple], path: str) -> None:
    """Save results to an Excel file."""
    try:
        import openpyxl
    except ImportError:
        import subprocess
        subprocess.check_call([sys.executable, '-m', 'pip', 'install', '-q', 'openpyxl'])
        import openpyxl

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = 'Query Results'
    ws.append(columns)
    for row in rows:
        ws.append(list(row))

    os.makedirs(os.path.dirname(path) or '.', exist_ok=True)
    wb.save(path)


def _save_csv(columns: list[str], rows: list[tuple], path: str) -> None:
    """Save results to a CSV file."""
    import csv
    os.makedirs(os.path.dirname(path) or '.', exist_ok=True)
    with open(path, 'w', newline='', encoding='utf-8') as f:
        writer = csv.writer(f)
        writer.writerow(columns)
        writer.writerows(rows)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    if hasattr(sys.stderr, 'reconfigure'):
        sys.stderr.reconfigure(encoding='utf-8', errors='replace')

    parser = argparse.ArgumentParser(description='Jeeves Redshift SQL runner')
    parser.add_argument('sql', nargs='?', default=None, help='SQL query to execute')
    parser.add_argument('--file', '-f', type=str, default=None, help='Read SQL from file')
    parser.add_argument('--output', '-o', type=str, default=None, help='Save results to .xlsx or .csv')
    parser.add_argument('--limit', type=int, default=100, help='Max rows for inline display (default: 100)')
    parser.add_argument('--generate', '-g', type=str, default=None,
                        help='Generate SQL from natural language question using DSPy, then execute')
    parser.add_argument('--save', type=str, default=None,
                        help='Save the query to the SQL repo after successful execution (provide a descriptive name)')
    parser.add_argument('--tags', type=str, default=None,
                        help='Comma-separated tags for --save (e.g. "portfolio,balance,loc")')
    parser.add_argument('--allow-today', action='store_true',
                        help='Downgrade the today/future-date guard from error to warning (data lags a day — only use when querying non-tape tables)')
    parser.add_argument('--allow-tables', type=str, default=None,
                        help="Comma-separated extra tables to allow beyond the whitelist, or '*' to skip the table check")
    parser.add_argument('--statement-timeout', type=int, default=None,
                        help='Override the default 60s Redshift statement_timeout, in seconds. Use for long waterfall/tape queries instead of copying this file into a sql_runner_long.py variant.')
    args = parser.parse_args()

    if args.statement_timeout:
        globals()['STATEMENT_TIMEOUT_MS'] = int(args.statement_timeout) * 1000
        print(f'[sql_runner] statement_timeout set to {args.statement_timeout}s', file=sys.stderr)

    # Generate SQL from natural language if --generate is used
    if args.generate:
        try:
            from dspy_sql import generate_sql
            result = generate_sql(args.generate)
            if not result["success"]:
                _error(f"SQL generation failed: {result['error']}")
                sys.exit(1)
            sql = result["sql"]
            print(f"Generated SQL ({result['reasoning'][:100]}...):\n{sql}\n", file=sys.stderr)
        except ImportError:
            _error("DSPy not installed. Run: uv pip install dspy")
            sys.exit(1)
    elif args.file:
        with open(args.file, 'r', encoding='utf-8') as f:
            sql = f.read().strip()
    elif args.sql:
        sql = args.sql.strip()
    else:
        print("ERROR: Provide SQL as an argument, use --file, or use --generate", file=sys.stderr)
        sys.exit(1)

    if not sql:
        print("ERROR: Empty SQL query", file=sys.stderr)
        sys.exit(1)

    # Validate
    if args.allow_tables and args.allow_tables.strip() == '*':
        extra_tables = '*'
    elif args.allow_tables:
        extra_tables = [t for t in args.allow_tables.split(',') if t.strip()]
    else:
        extra_tables = None
    issues = validate_sql(sql, allow_today=args.allow_today, extra_tables=extra_tables)
    errors = [i for i in issues if i.startswith('ERROR:')]
    warnings = [i for i in issues if not i.startswith('ERROR:')]

    for w in warnings:
        _warn(w)

    if errors:
        for e in errors:
            _error(e.replace('ERROR: ', ''))
        sys.exit(1)

    # Always echo the SQL being executed for visibility
    print("=" * 60)
    print("EXECUTING SQL:")
    print("-" * 60)
    print(sql)
    print("=" * 60)
    print()

    # Execute
    _ensure_deps()
    _rate_limit()
    conn = None
    cur = None
    try:
        conn = _connect()
        cur = conn.cursor()
        cur.execute(sql)

        if cur.description is None:
            print("Query executed successfully (no result set).")
            cur.close()
            _release(conn)
            return

        columns = [desc[0] for desc in cur.description]
        rows = cur.fetchall()
        cur.close()
        cur = None
        _release(conn)
        conn = None

    except Exception as e:
        if cur is not None:
            try:
                cur.close()
            except Exception:
                pass
        if conn is not None:
            try:
                conn.rollback()
            except Exception:
                pass
            _release(conn)
        err_msg = str(e).strip()
        _error(f"Query failed: {err_msg}")

        # Provide actionable guidance for common errors
        if 'column' in err_msg.lower() and 'does not exist' in err_msg.lower():
            print("Hint: Check column name spelling. Load jeeves-redshift skill for the full schema.", file=sys.stderr)
        elif 'relation' in err_msg.lower() and 'does not exist' in err_msg.lower():
            print("Hint: Check table name. Allowed tables: " + ', '.join(sorted(ALLOWED_TABLES)), file=sys.stderr)
        elif 'permission denied' in err_msg.lower():
            print("Hint: The bmauck user may not have access to this table.", file=sys.stderr)
        elif 'statement_timeout' in err_msg.lower() or 'canceling statement' in err_msg.lower():
            print(f"Hint: Query exceeded {STATEMENT_TIMEOUT_MS // 1000}s timeout. Simplify the query or add tighter filters.", file=sys.stderr)
        elif 'invalid input syntax' in err_msg.lower():
            print("Hint: Data type mismatch. Check for empty strings in integer columns — use a CTE with WHERE column > 0.", file=sys.stderr)

        sys.exit(1)

    # Output
    print(f"({len(rows)} rows, {len(columns)} columns)")

    if args.output:
        path = args.output
        if path.endswith('.csv'):
            _save_csv(columns, rows, path)
        else:
            _save_excel(columns, rows, path)
        print(f"Saved to: {path}")
    else:
        print()
        print(_format_inline(columns, rows, args.limit))

    # Save to SQL repo if requested
    if args.save:
        try:
            from sql_repo import save_query
            tags = [t.strip() for t in args.tags.split(',')] if args.tags else []
            save_query(args.save, sql, tags=tags)
            print(f"\nQuery saved to repo as: {args.save}")
        except Exception as e:
            print(f"\nWarning: Could not save to repo: {e}", file=sys.stderr)


if __name__ == '__main__':
    main()
