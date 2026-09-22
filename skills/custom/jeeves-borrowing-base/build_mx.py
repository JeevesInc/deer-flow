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


# Columns the borrowing base is actually computed from. A NULL in any of
# these on the tape date is a DATA GAP: it silently nulls a downstream *_usd
# column, flips elig_ff to 0, and publishes a wrong base. 2026-09-21: the
# 2026-09-20 run published Eligible $0 this way. These are REPORTED, never
# quietly filled.
CRITICAL_COLS = [
    'spot_rate', 'balance', 'sofom_balance', 'sofom_balance_usd', 'balance_usd',
    'card_balance_usd', 'jp_principal_balance_usd', 'credit_limit_usd', 'elig',
]


def check_tape_completeness(con, df, date_str):
    """Return (gaps, fx_carried) for one tape date.

    gaps        list of "column: n/total rows NULL" strings for CRITICAL_COLS
    fx_carried  count of rows whose spot_rate came from the fx_fallback CTE
                (i.e. the source table had no rate for this date at all)

    Both are surfaced to the caller and to Slack. The fallback keeps the base
    computable, but it is never invisible.
    """
    gaps = []
    n = len(df)
    for col in CRITICAL_COLS:
        if col not in df.columns:
            gaps.append(f"{col}: COLUMN MISSING from tape")
            continue
        nulls = int(df[col].isna().sum())
        if nulls:
            gaps.append(f"{col}: {nulls}/{n} rows NULL")

    fx_carried = 0
    try:
        cur = con.cursor()
        cur.execute(
            "select count(*), sum(case when spot_rate is null then 1 else 0 end) "
            "from capital_markets_dm.loc_tape where dt = %s", (date_str,)
        )
        row_ct, null_ct = cur.fetchone()
        cur.close()
        if row_ct and null_ct and int(null_ct) == int(row_ct):
            fx_carried = int(row_ct)
    except Exception as err:
        gaps.append(f"spot_rate source check failed: {err}")
    return gaps, fx_carried


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
    data_gaps = []
    try:
        dfs = []

        for i, date in enumerate(dates):
            date_str = date.isoformat()
            print(f"  [{i+1}/{len(dates)}] Querying SOFOM tape for {date_str}...")
            df = pd.read_sql_query(tape_sql.format(date_str), con)
            print(f"    {len(df)} rows")
            if len(df) > 0:
                df = calculate_eligibility_fields_sofom(df)
                gaps, fx_carried = check_tape_completeness(con, df, date_str)
                if fx_carried:
                    msg = (f"DATA-GAP: {date_str} capital_markets_dm.loc_tape has NO "
                           f"spot_rate for any of {fx_carried} rows -- USD columns "
                           f"rebuilt from each company's last prior non-null rate. "
                           f"FX is CARRIED FORWARD, not market, for this date.")
                    print(msg)
                    print(msg, file=sys.stderr)
                    data_gaps.append(msg)
                for g in gaps:
                    msg = f"DATA-GAP: {date_str} {g}"
                    print(msg)
                    print(msg, file=sys.stderr)
                    data_gaps.append(msg)
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
        sofom_total = latest['sofom_balance_usd'].sum()
        eligible = latest[latest['elig'] == 1]
        elig_total = eligible['sofom_balance_usd'].sum()
        print(f"Latest date SOFOM balance: ${sofom_total:,.2f}")
        print(f"Eligible SOFOM balance:    ${elig_total:,.2f}")

        # 2026-09-21 GUARD: on 2026-09-20 the 08:30 build published SOFOM $0.00 /
        # Eligible $0.00 because loc_tape.spot_rate was NULL for every row of the
        # weekend dt, nulling every *_usd column so elig_ff compared NaN and
        # returned 0 for all 788 rows. A pool that is 100% ineligible, or a latest
        # date with no FX, is a data fault -- never a portfolio fact. Fail the
        # build so the scheduler alerts instead of merging a $0 workbook.
        n_no_fx = int(latest['spot_rate'].isna().sum())
        if n_no_fx:
            print(f"ERROR: spot_rate is NULL for {n_no_fx}/{len(latest)} rows on "
                  f"{latest['dt'].max()} -- FX fallback did not resolve. "
                  f"Not publishing this tape.", file=sys.stderr)
            sys.exit(3)
        if data_gaps:
            print("\n" + "=" * 70)
            print(f"DATA GAPS DETECTED ({len(data_gaps)}) -- REPORT BEFORE USING THIS BASE:")
            for g in data_gaps:
                print("  * " + g)
            print("=" * 70)
        if abs(latest['sofom_balance'].sum()) > 0 and elig_total == 0:
            print(f"ERROR: eligible balance is 0 against a non-zero pool "
                  f"({len(latest)} rows) on {latest['dt'].max()} -- eligibility "
                  f"fault, not a portfolio fact. Not publishing this tape.",
                  file=sys.stderr)
            sys.exit(4)


if __name__ == '__main__':
    main()
