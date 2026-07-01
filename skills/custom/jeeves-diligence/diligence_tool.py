#!/usr/bin/env python3
"""Due diligence tool — gather data, build trackers, verify claims.

Usage:
    python diligence_tool.py gather-portfolio --date YYYY-MM-DD
    python diligence_tool.py verify-claim "claim text"
    python diligence_tool.py tracker-check <drive_folder_id>
    python diligence_tool.py ddq-scaffold --input questions.txt --output ddq_draft.md

Requires env vars:
  - REDSHIFT_HOST, REDSHIFT_PORT, REDSHIFT_DB, REDSHIFT_USER, REDSHIFT_PASSWORD
  - GOOGLE_CLIENT_ID, GOOGLE_CLIENT_SECRET, GOOGLE_REFRESH_TOKEN
"""

import argparse
import datetime as dt
import json
import os
import sys

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))

sys.path.insert(0, os.path.join(SCRIPT_DIR, '..', '_shared'))
from env_loader import load_env
load_env()


# ---------------------------------------------------------------------------
# Redshift helpers (reuse borrowing-base connection logic)
# ---------------------------------------------------------------------------

def _connect_redshift():
    """Get a Redshift connection with timeout."""
    try:
        import psycopg2
    except ImportError:
        import subprocess
        subprocess.check_call([sys.executable, '-m', 'pip', 'install', '-q', 'psycopg2-binary'])
        import psycopg2

    missing = [k for k in ('REDSHIFT_HOST', 'REDSHIFT_PORT', 'REDSHIFT_DB', 'REDSHIFT_USER', 'REDSHIFT_PASSWORD')
               if not os.environ.get(k)]
    if missing:
        print(f"ERROR: Missing env vars: {', '.join(missing)}", file=sys.stderr)
        sys.exit(1)

    return psycopg2.connect(
        host=os.environ['REDSHIFT_HOST'],
        port=int(os.environ['REDSHIFT_PORT']),
        dbname=os.environ['REDSHIFT_DB'],
        user=os.environ['REDSHIFT_USER'],
        password=os.environ['REDSHIFT_PASSWORD'],
        sslmode='require',
        sslrootcert='disable',
        connect_timeout=30,
        options='-c statement_timeout=120000',
    )


def _query(sql, params=None):
    """Execute a read-only query and return (columns, rows)."""
    conn = _connect_redshift()
    try:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            cols = [d[0] for d in cur.description]
            rows = cur.fetchall()
        return cols, rows
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# gather-portfolio: pull all key metrics for a DD data package
# ---------------------------------------------------------------------------

PORTFOLIO_QUERIES = {
    "portfolio_summary": """
        SELECT
            COUNT(DISTINCT company_id) AS active_accounts,
            SUM(balance_usd) AS total_balance,
            SUM(CASE WHEN days_past_due >= 30 THEN balance_usd ELSE 0 END)
                / NULLIF(SUM(balance_usd), 0) AS dpd_30plus_rate,
            SUM(CASE WHEN days_past_due >= 90 THEN balance_usd ELSE 0 END)
                / NULLIF(SUM(balance_usd), 0) AS dpd_90plus_rate,
            SUM(credit_limit_usd) AS total_credit_limit
        FROM capital_markets_dm.loc_tape
        WHERE dt = %s AND charge_off_flag = false AND is_in_repayment = false
    """,
    "country_breakdown": """
        SELECT
            country_code,
            COUNT(DISTINCT company_id) AS accounts,
            SUM(balance_usd) AS balance,
            SUM(CASE WHEN days_past_due >= 90 THEN balance_usd ELSE 0 END)
                / NULLIF(SUM(balance_usd), 0) AS dpd_90plus_rate
        FROM capital_markets_dm.loc_tape
        WHERE dt = %s AND charge_off_flag = false AND is_in_repayment = false
        GROUP BY country_code ORDER BY balance DESC
    """,
    "charge_off_summary": """
        SELECT
            DATE_TRUNC('month', charge_off_dt)::date AS month,
            COUNT(DISTINCT company_id) AS accounts,
            SUM(balance_usd) AS charged_off_balance
        FROM capital_markets_dm.loc_tape
        WHERE charge_off_dt IS NOT NULL AND dt = charge_off_dt
            AND charge_off_dt BETWEEN %s AND %s
        GROUP BY 1 ORDER BY 1
    """,
    "gwc_summary": """
        SELECT
            SUM(balance_usd) AS gwc_balance,
            COUNT(DISTINCT company_id) AS gwc_accounts
        FROM capital_markets_dm.gwc_tape
        WHERE dt = %s AND charge_off_flag = false
    """,
    "dq_buckets": """
        SELECT
            dq_bucket,
            COUNT(DISTINCT company_id) AS accounts,
            SUM(balance_usd) AS balance
        FROM capital_markets_dm.loc_tape
        WHERE dt = %s AND charge_off_flag = false AND is_in_repayment = false
        GROUP BY dq_bucket ORDER BY dq_bucket
    """,
    "top_exposures": """
        SELECT
            lt.company_id,
            c.name,
            lt.country_code,
            lt.balance_usd,
            lt.credit_limit_usd,
            lt.days_past_due
        FROM capital_markets_dm.loc_tape lt
        JOIN master_customer_dm.companies_dm c ON c.company_id = lt.company_id
        WHERE lt.dt = %s AND lt.charge_off_flag = false AND lt.is_in_repayment = false
            AND c.is_test = false
        ORDER BY lt.balance_usd DESC LIMIT 20
    """,
}

COUNTRY_NAMES = {484: 'Mexico', 170: 'Colombia', 840: 'USA', 124: 'Canada', 76: 'Brazil'}


def cmd_gather_portfolio(args):
    """Pull all key DD metrics from Redshift and output as structured JSON."""
    yesterday = dt.date.today() - dt.timedelta(days=1)
    try:
        date = dt.datetime.strptime(args.date, '%Y-%m-%d').date() if args.date else yesterday
    except ValueError:
        print(f"ERROR: Invalid date '{args.date}'. Expected YYYY-MM-DD.", file=sys.stderr)
        sys.exit(1)

    if date >= dt.date.today():
        print(f"ERROR: Date {date} is today or future. Data available through {yesterday}.", file=sys.stderr)
        sys.exit(1)

    six_months_ago = date - dt.timedelta(days=180)

    print(f"Gathering portfolio data for {date}...\n")
    results = {"as_of_date": date.isoformat(), "queries_run": []}

    for name, sql in PORTFOLIO_QUERIES.items():
        print(f"  Running {name}...")
        try:
            # Determine params based on query
            if name == "charge_off_summary":
                cols, rows = _query(sql, (six_months_ago, date))
            else:
                cols, rows = _query(sql, (date,))

            results[name] = [dict(zip(cols, row)) for row in rows]
            results["queries_run"].append(name)
            print(f"    {len(rows)} rows")
        except Exception as e:
            results[name] = {"error": str(e)}
            print(f"    ERROR: {e}", file=sys.stderr)

    # Map country codes
    if "country_breakdown" in results and isinstance(results["country_breakdown"], list):
        for row in results["country_breakdown"]:
            cc = row.get("country_code")
            if cc:
                row["country_name"] = COUNTRY_NAMES.get(int(cc), str(cc))

    # Output
    output_dir = os.environ.get('OUTPUTS_PATH', '/mnt/user-data/outputs')
    os.makedirs(output_dir, exist_ok=True)
    output_path = os.path.join(output_dir, f"dd_portfolio_{date.isoformat()}.json")
    with open(output_path, 'w', encoding='utf-8') as f:
        json.dump(results, f, indent=2, default=str)

    print(f"\nSaved to: {output_path}")
    print(f"Queries completed: {len(results['queries_run'])}/{len(PORTFOLIO_QUERIES)}")

    # Print human-readable summary
    summary = results.get("portfolio_summary", [{}])
    if summary and isinstance(summary, list) and summary[0]:
        s = summary[0]
        print(f"\n--- Portfolio Summary (as of {date}) ---")
        print(f"  Active accounts:   {s.get('active_accounts', 'N/A')}")
        bal = s.get('total_balance')
        print(f"  Total balance:     ${float(bal):,.0f}" if bal else "  Total balance:     N/A")
        dpd90 = s.get('dpd_90plus_rate')
        print(f"  90+ DPD rate:      {float(dpd90)*100:.2f}%" if dpd90 else "  90+ DPD rate:      N/A")


# ---------------------------------------------------------------------------
# verify-claim: check a factual claim against Redshift data
# ---------------------------------------------------------------------------

def cmd_verify_claim(args):
    """Check if a claim can be verified against available data.

    This does NOT verify the claim automatically — it runs relevant queries
    and presents the raw data so the user can compare.
    """
    claim = args.claim
    print(f"Claim to verify: \"{claim}\"\n")
    print("=" * 60)
    print("IMPORTANT: This tool provides raw data for manual verification.")
    print("It does NOT determine truth — you must compare the data yourself.")
    print("=" * 60)

    yesterday = dt.date.today() - dt.timedelta(days=1)
    claim_lower = claim.lower()

    queries_to_run = []

    # Detect what kind of claim this is and queue relevant queries
    if any(kw in claim_lower for kw in ('balance', 'portfolio', 'aum', 'outstanding')):
        queries_to_run.append(("Current portfolio balance", PORTFOLIO_QUERIES["portfolio_summary"], (yesterday,)))

    if any(kw in claim_lower for kw in ('dpd', 'delinquen', 'past due', 'dq')):
        queries_to_run.append(("DQ bucket distribution", PORTFOLIO_QUERIES["dq_buckets"], (yesterday,)))

    if any(kw in claim_lower for kw in ('charge off', 'charge-off', 'co rate', 'write off', 'write-off')):
        six_months_ago = yesterday - dt.timedelta(days=180)
        queries_to_run.append(("Charge-off trend (6 months)", PORTFOLIO_QUERIES["charge_off_summary"], (six_months_ago, yesterday)))

    if any(kw in claim_lower for kw in ('country', 'mexico', 'brazil', 'colombia', 'canada', 'geographic')):
        queries_to_run.append(("Country breakdown", PORTFOLIO_QUERIES["country_breakdown"], (yesterday,)))

    if any(kw in claim_lower for kw in ('gwc', 'jeeves pay', 'repayment')):
        queries_to_run.append(("GWC summary", PORTFOLIO_QUERIES["gwc_summary"], (yesterday,)))

    if any(kw in claim_lower for kw in ('account', 'borrower', 'customer', 'active')):
        queries_to_run.append(("Portfolio summary", PORTFOLIO_QUERIES["portfolio_summary"], (yesterday,)))

    if not queries_to_run:
        queries_to_run.append(("Portfolio summary (general)", PORTFOLIO_QUERIES["portfolio_summary"], (yesterday,)))
        queries_to_run.append(("Country breakdown (general)", PORTFOLIO_QUERIES["country_breakdown"], (yesterday,)))

    print(f"\nRunning {len(queries_to_run)} verification query(ies)...\n")

    for label, sql, params in queries_to_run:
        print(f"--- {label} ---")
        try:
            cols, rows = _query(sql, params)
            if not rows:
                print("  (no data)\n")
                continue
            # Print as simple table
            widths = [max(len(str(c)), max((len(str(r[i] if r[i] is not None else 'NULL')) for r in rows), default=0)) for i, c in enumerate(cols)]
            widths = [min(w, 30) for w in widths]
            header = ' | '.join(c.ljust(widths[i]) for i, c in enumerate(cols))
            print(f"  {header}")
            print(f"  {'-+-'.join('-' * w for w in widths)}")
            for row in rows[:20]:
                vals = [str(v if v is not None else 'NULL')[:30].ljust(widths[i]) for i, v in enumerate(row)]
                print(f"  {' | '.join(vals)}")
            if len(rows) > 20:
                print(f"  ... ({len(rows)} total rows)")
            print()
        except Exception as e:
            print(f"  ERROR: {e}\n", file=sys.stderr)

    print("Compare the data above against the claim. If no query covers the claim,")
    print("the claim cannot be verified from Redshift data — flag as [Needs Confirmation].")


# ---------------------------------------------------------------------------
# ddq-scaffold: create a DDQ response template from a list of questions
# ---------------------------------------------------------------------------

def cmd_ddq_scaffold(args):
    """Read a list of DDQ questions and create a scaffolded response document."""
    if not os.path.isfile(args.input):
        print(f"ERROR: File not found: {args.input}", file=sys.stderr)
        sys.exit(1)

    with open(args.input, 'r', encoding='utf-8') as f:
        raw = f.read()

    # Parse questions — each non-blank line is a question (or numbered like "1. ...")
    lines = [l.strip() for l in raw.strip().split('\n') if l.strip()]
    questions = []
    for line in lines:
        # Strip leading numbering like "1.", "1)", "Q1:", etc.
        import re
        cleaned = re.sub(r'^(?:Q?\d+[\.\)\:]?\s*)', '', line).strip()
        if cleaned:
            questions.append(cleaned)

    if not questions:
        print("ERROR: No questions found in input file.", file=sys.stderr)
        sys.exit(1)

    output_dir = os.environ.get('OUTPUTS_PATH', '/mnt/user-data/outputs')
    os.makedirs(output_dir, exist_ok=True)
    output_path = os.path.join(output_dir, args.output) if args.output else os.path.join(output_dir, 'ddq_scaffold.md')

    with open(output_path, 'w', encoding='utf-8') as f:
        f.write("# DDQ Response Document\n\n")
        f.write(f"_Generated: {dt.date.today().isoformat()}_\n\n")
        f.write("---\n\n")
        f.write("> **IMPORTANT**: Every response must cite a specific source (Redshift query, Drive document,\n")
        f.write("> or management confirmation). Do not fill in answers without a verified source.\n")
        f.write("> Mark unverified items as **[Management to confirm]**.\n\n")
        f.write("---\n\n")

        for i, q in enumerate(questions, 1):
            f.write(f"## Q{i}. {q}\n\n")
            f.write("**Source**: [Specify: Redshift query / Drive document ID / Management confirmation]\n\n")
            f.write("**Response**:\n\n")
            f.write("[TO BE COMPLETED — do not write without a verified source]\n\n")
            f.write("---\n\n")

    print(f"Scaffolded {len(questions)} questions → {output_path}")
    print(f"\nNext steps:")
    print(f"  1. For each question, identify the data source (Redshift, Drive, or management)")
    print(f"  2. Pull the data and write the response")
    print(f"  3. Flag anything without a source as [Management to confirm]")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    if hasattr(sys.stderr, 'reconfigure'):
        sys.stderr.reconfigure(encoding='utf-8', errors='replace')

    parser = argparse.ArgumentParser(description='Jeeves Due Diligence Tool')
    subparsers = parser.add_subparsers(dest='command')

    # gather-portfolio
    gp = subparsers.add_parser('gather-portfolio', help='Pull all key DD portfolio metrics')
    gp.add_argument('--date', type=str, default=None, help='Date YYYY-MM-DD (default: yesterday)')

    # verify-claim
    vc = subparsers.add_parser('verify-claim', help='Check a claim against Redshift data')
    vc.add_argument('claim', type=str, help='The factual claim to verify')

    # ddq-scaffold
    ds = subparsers.add_parser('ddq-scaffold', help='Create DDQ response template from questions')
    ds.add_argument('--input', '-i', type=str, required=True, help='Text file with DDQ questions')
    ds.add_argument('--output', '-o', type=str, default='ddq_scaffold.md', help='Output filename')

    args = parser.parse_args()

    if args.command == 'gather-portfolio':
        cmd_gather_portfolio(args)
    elif args.command == 'verify-claim':
        cmd_verify_claim(args)
    elif args.command == 'ddq-scaffold':
        cmd_ddq_scaffold(args)
    else:
        parser.print_help()
        sys.exit(1)


if __name__ == '__main__':
    main()
