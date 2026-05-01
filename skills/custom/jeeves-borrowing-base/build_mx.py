#!/usr/bin/env python3
"""Build the MX (SOFOM) borrowing base workbook.

Usage:
    python build_mx.py [--start-date YYYY-MM-DD] [--end-date YYYY-MM-DD]

Defaults to a 3-day range ending yesterday.
Produces a single-tab Excel workbook: tape_combined

Output saved to $OUTPUTS_PATH/Borrowing Base - SOFOM - {YYYYMMDD}.xlsx
"""

import argparse
import datetime as dt
import os
from pathlib import Path
import sys

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))


from redshift_util import ensure_deps, connect


def main():
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    if hasattr(sys.stderr, 'reconfigure'):
        sys.stderr.reconfigure(encoding='utf-8', errors='replace')

    parser = argparse.ArgumentParser(description='Build MX SOFOM borrowing base')
    parser.add_argument('--start-date', type=str, default=None,
                        help='Start date YYYY-MM-DD (default: 3 days before yesterday)')
    parser.add_argument('--end-date', type=str, default=None,
                        help='End date YYYY-MM-DD (default: yesterday)')
    args = parser.parse_args()

    ensure_deps('xlsxwriter')
    import pandas as pd
    sys.path.insert(0, SCRIPT_DIR)
    from eligibility import calculate_eligibility_fields_sofom

    # Compute dates
    yesterday = dt.date.today() - dt.timedelta(days=1)
    try:
        end_date = dt.datetime.strptime(args.end_date, '%Y-%m-%d').date() if args.end_date else yesterday
    except ValueError:
        print(f"ERROR: Invalid end-date format '{args.end_date}'. Expected YYYY-MM-DD.", file=sys.stderr)
        sys.exit(1)

    # Guard: never run for today or future — data is not available yet
    if end_date >= dt.date.today():
        print(f"ERROR: end-date {end_date} is today or in the future. "
              f"Redshift data is only available through yesterday ({yesterday}). "
              f"Use --end-date {yesterday} or earlier.", file=sys.stderr)
        sys.exit(1)

    try:
        start_date = dt.datetime.strptime(args.start_date, '%Y-%m-%d').date() if args.start_date else end_date - dt.timedelta(days=2)
    except ValueError:
        print(f"ERROR: Invalid start-date format '{args.start_date}'. Expected YYYY-MM-DD.", file=sys.stderr)
        sys.exit(1)

    if start_date > end_date:
        print(f"ERROR: start-date {start_date} is after end-date {end_date}.", file=sys.stderr)
        sys.exit(1)

    # Generate date list
    dates = []
    d = start_date
    while d <= end_date:
        dates.append(d)
        d += dt.timedelta(days=1)

    print(f"MX SOFOM Borrowing Base: {start_date} to {end_date} ({len(dates)} days)")

    # Load SQL
    tape_sql = Path(SCRIPT_DIR, 'sql', 'data_tape_sofom.sql').read_text()

    con = connect()
    try:
        dfs = []

        for i, date in enumerate(dates):
            date_str = date.isoformat()
            print(f"  [{i+1}/{len(dates)}] Querying SOFOM tape for {date_str}...")
            df = pd.read_sql_query(tape_sql.format(date_str), con)
            print(f"    {len(df)} rows")
            if len(df) > 0:
                df = calculate_eligibility_fields_sofom(df)
            dfs.append(df)
    finally:
        con.close()

    print("\n  Combining results...")
    df_combined = pd.concat(dfs, ignore_index=True)
    print(f"  Total rows: {len(df_combined)}")

    # Save workbook
    output_dir = os.environ.get('OUTPUTS_PATH', '/mnt/user-data/outputs')
    os.makedirs(output_dir, exist_ok=True)
    date_str = end_date.strftime('%Y%m%d')
    filename = f"Borrowing Base - SOFOM - {date_str}.xlsx"
    output_path = os.path.join(output_dir, filename)

    print(f"  Writing {filename}...")
    with pd.ExcelWriter(output_path, engine='xlsxwriter') as writer:
        df_combined.to_excel(writer, sheet_name='tape', index=False)

    print(f"\nDone! Output: {output_path}")
    if len(df_combined) > 0:
        latest = df_combined[df_combined['dt'] == df_combined['dt'].max()]
        print(f"Latest date SOFOM balance: ${latest['sofom_balance_usd'].sum():,.2f}")
        eligible = latest[latest['elig'] == 1]
        print(f"Eligible SOFOM balance:    ${eligible['sofom_balance_usd'].sum():,.2f}")


if __name__ == '__main__':
    main()
