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

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))

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
    'dms_mysql_underwriting_raw.taktile_data',
}

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


def validate_sql(sql: str) -> list[str]:
    """Validate SQL and return list of warnings. Returns errors as strings prefixed with 'ERROR:'."""
    warnings = []
    sql_upper = sql.upper()
    sql_lower = sql.lower()

    # Block non-SELECT statements
    # Strip leading WITH (CTE) to find the actual statement type
    stripped = re.sub(r'(?i)^\s*WITH\s+.*?\)\s*', '', sql, flags=re.DOTALL)
    first_keyword = stripped.strip().split()[0].upper() if stripped.strip() else ''
    if first_keyword and first_keyword not in ('SELECT', 'WITH', 'EXPLAIN'):
        return [f"ERROR: Only SELECT queries are allowed. Got: {first_keyword}"]

    # Check for deprecated columns
    for col, fix in DEPRECATED_COLUMNS.items():
        if col in sql_lower:
            warnings.append(f"Deprecated column '{col}' used. {fix}")

    # Check DATE_TRUNC on dt column
    if re.search(r"DATE_TRUNC\s*\([^)]*['\"]month['\"][^)]*\bdt\b", sql, re.IGNORECASE):
        warnings.append("DATE_TRUNC on dt column detected. Use 'dt BETWEEN ... AND ...' instead for loc_tape/gwc_tape.")

    # Check for today/future dates
    today = dt.date.today().isoformat()
    if f"'{today}'" in sql:
        warnings.append(f"Today's date ({today}) used. Redshift data is only available through yesterday. Use {(dt.date.today() - dt.timedelta(days=1)).isoformat()} instead.")

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


def _connect():
    import psycopg2
    return psycopg2.connect(
        host=os.environ['REDSHIFT_HOST'],
        port=int(os.environ['REDSHIFT_PORT']),
        dbname=os.environ['REDSHIFT_DB'],
        user=os.environ['REDSHIFT_USER'],
        password=os.environ['REDSHIFT_PASSWORD'],
        sslmode='require',
        sslrootcert='disable',
    )


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
    args = parser.parse_args()

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
    issues = validate_sql(sql)
    errors = [i for i in issues if i.startswith('ERROR:')]
    warnings = [i for i in issues if not i.startswith('ERROR:')]

    for w in warnings:
        _warn(w)

    if errors:
        for e in errors:
            _error(e.replace('ERROR: ', ''))
        sys.exit(1)

    # Execute
    _ensure_deps()
    try:
        conn = _connect()
        cur = conn.cursor()
        cur.execute(sql)

        if cur.description is None:
            print("Query executed successfully (no result set).")
            conn.close()
            return

        columns = [desc[0] for desc in cur.description]
        rows = cur.fetchall()
        conn.close()

    except Exception as e:
        err_msg = str(e).strip()
        _error(f"Query failed: {err_msg}")

        # Provide actionable guidance for common errors
        if 'column' in err_msg.lower() and 'does not exist' in err_msg.lower():
            print("Hint: Check column name spelling. Load jeeves-redshift skill for the full schema.", file=sys.stderr)
        elif 'relation' in err_msg.lower() and 'does not exist' in err_msg.lower():
            print("Hint: Check table name. Allowed tables: " + ', '.join(sorted(ALLOWED_TABLES)), file=sys.stderr)
        elif 'permission denied' in err_msg.lower():
            print("Hint: The analytics_dev user may not have access to this table.", file=sys.stderr)
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


if __name__ == '__main__':
    main()
