#!/usr/bin/env python3
"""Build the US (Bridge) borrowing base workbook.

Usage:
    python build_us.py [--date YYYY-MM-DD]

Defaults to yesterday. Produces a 4-tab Excel workbook:
  tape_start, tape_end, rollforward, eligibility_summary

Output saved to $OUTPUTS_PATH/Borrowing Base - US - {YYYYMMDD}.xlsx
"""

import argparse
import datetime as dt
import os
import sys

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))


def _ensure_deps():
    try:
        import psycopg2, pandas, openpyxl, xlsxwriter  # noqa: F401
    except ImportError:
        import subprocess
        subprocess.check_call([sys.executable, '-m', 'pip', 'install', '-q',
                               'psycopg2-binary', 'pandas', 'openpyxl', 'xlsxwriter'])


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


def main():
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    if hasattr(sys.stderr, 'reconfigure'):
        sys.stderr.reconfigure(encoding='utf-8', errors='replace')

    parser = argparse.ArgumentParser(description='Build US borrowing base')
    parser.add_argument('--date', type=str, default=None,
                        help='Target date YYYY-MM-DD (default: yesterday)')
    args = parser.parse_args()

    _ensure_deps()
    import pandas as pd
    sys.path.insert(0, SCRIPT_DIR)
    from eligibility import calculate_eligibility_fields, calculate_eligibility_summary

    # Compute dates
    if args.date:
        date_end = dt.datetime.strptime(args.date, '%Y-%m-%d').date()
    else:
        date_end = dt.date.today() - dt.timedelta(days=1)

    # BOP = last day of prior month
    date_beg = date_end.replace(day=1) - dt.timedelta(days=1)

    print(f"US Borrowing Base: BOP={date_beg}, EOP={date_end}")

    # Load SQL
    tape_sql = open(os.path.join(SCRIPT_DIR, 'sql', 'data_tape.sql')).read()
    rf_sql = open(os.path.join(SCRIPT_DIR, 'sql', 'loc_acct_rollforward.sql')).read()

    con = _connect()

    print(f"  Querying tape for {date_beg} (BOP)...")
    df_beg = pd.read_sql_query(tape_sql.format(date_beg.isoformat()), con)
    print(f"    {len(df_beg)} rows")

    print(f"  Querying tape for {date_end} (EOP)...")
    df_end = pd.read_sql_query(tape_sql.format(date_end.isoformat()), con)
    print(f"    {len(df_end)} rows")

    print(f"  Querying rollforward {date_beg} -> {date_end}...")
    rollforward = pd.read_sql_query(
        rf_sql.format(date_beg.isoformat(), date_end.isoformat()), con)
    print(f"    {len(rollforward)} rows")

    con.close()

    print("  Calculating eligibility (BOP)...")
    df_beg = calculate_eligibility_fields(df_beg)

    print("  Calculating eligibility (EOP)...")
    df_end = calculate_eligibility_fields(df_end)

    print("  Generating eligibility summary...")
    elig_summary = calculate_eligibility_summary(df_end, balance_col='balance_usd')

    # Save workbook
    output_dir = os.environ.get('OUTPUTS_PATH', '/mnt/user-data/outputs')
    os.makedirs(output_dir, exist_ok=True)
    date_str = date_end.strftime('%Y%m%d')
    filename = f"Borrowing Base - US - {date_str}.xlsx"
    output_path = os.path.join(output_dir, filename)

    print(f"  Writing {filename}...")
    with pd.ExcelWriter(output_path, engine='xlsxwriter') as writer:
        df_beg.to_excel(writer, sheet_name='tape_start', index=False)
        df_end.to_excel(writer, sheet_name='tape_end', index=False)
        rollforward.to_excel(writer, sheet_name='rollforward', index=False)
        elig_summary.to_excel(writer, sheet_name='eligibility_summary', index=False)

    print(f"\nDone! Output: {output_path}")
    print(f"Total EOP balance: ${df_end['balance_usd'].sum():,.2f}")
    eligible = df_end[df_end['elig'] == 1]
    print(f"Eligible balance:  ${eligible['balance_usd'].sum():,.2f}")
    print(f"Eligible accounts: {len(eligible)} of {len(df_end)}")


if __name__ == '__main__':
    main()
