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


from redshift_util import ensure_deps, connect


def main():
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    if hasattr(sys.stderr, 'reconfigure'):
        sys.stderr.reconfigure(encoding='utf-8', errors='replace')

    parser = argparse.ArgumentParser(description='Build US borrowing base')
    parser.add_argument('--date', type=str, default=None,
                        help='Target date YYYY-MM-DD (default: yesterday)')
    args = parser.parse_args()

    ensure_deps('xlsxwriter')
    import pandas as pd
    sys.path.insert(0, SCRIPT_DIR)
    from eligibility import calculate_eligibility_fields, calculate_eligibility_summary

    # Compute dates
    yesterday = dt.date.today() - dt.timedelta(days=1)
    if args.date:
        date_end = dt.datetime.strptime(args.date, '%Y-%m-%d').date()
    else:
        date_end = yesterday

    # Guard: never run for today or future — data is not available yet
    if date_end >= dt.date.today():
        print(f"ERROR: date {date_end} is today or in the future. "
              f"Redshift data is only available through yesterday ({yesterday}). "
              f"Use --date {yesterday} or earlier.", file=sys.stderr)
        sys.exit(1)

    # BOP = last day of prior month
    date_beg = date_end.replace(day=1) - dt.timedelta(days=1)

    print(f"US Borrowing Base: BOP={date_beg}, EOP={date_end}")

    # Load SQL
    tape_sql = open(os.path.join(SCRIPT_DIR, 'sql', 'data_tape.sql')).read()
    rf_sql = open(os.path.join(SCRIPT_DIR, 'sql', 'loc_acct_rollforward.sql')).read()

    con = connect()

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

    # Enforce exact column order to match template formula references.
    # Reference order derived from the March 30 Bridge BB tape_end tab.
    TAPE_COL_ORDER = [
        'dt','company_id','loan_id','country_code','product','currency','delinquent_dt',
        'days_past_due','dq_bucket','dq_bucket_daily','dq_bucket_monthly','charge_off_exclusion',
        'charge_off_dt','charge_off_flag','disbursement_amount','payment_amount','cashback_amount',
        'late_payment_penalty_amount','jeeves_pay_disbursement_amount','jeeves_pay_fee_amount',
        'loan_allocation_amount','fx_adjustment_amount','adjustment_amount','debit_amount',
        'credit_amount','balance','jvs_remaining','sofom_balance','sofom_balance_usd',
        'transfer_flag','card_balance','jp_balance','jp_principal_balance','jp_interest_balance',
        'overpay_balance','invoiced','spot_rate','disbursement_amount_usd','payment_amount_usd',
        'cashback_amount_usd','late_payment_penalty_amount_usd','jeeves_pay_disbursement_amount_usd',
        'jeeves_pay_fee_amount_usd','loan_allocation_amount_usd','fx_adjustment_amount_usd',
        'adjustment_amount_usd','debit_amount_usd','credit_amount_usd','balance_usd',
        'card_balance_usd','jp_balance_usd','jp_principal_balance_usd','jp_interest_balance_usd',
        'invoiced_usd','forex_adjustment','is_in_repayment','repayment_dt',
        'v0_charge_off_amount_usd','v0_charge_off_cumulative_amount_usd','status',
        'card_disbursement','card_payment','card_cashback','card_late_payment_penalty',
        'card_loan_allocation','card_fx_adjustment','card_adjustment','jp_disbursement','jp_fee',
        'jp_payment','jp_cashback','jp_late_payment_penalty','jp_loan_allocation','jp_fx_adjustment',
        'jp_adjustment','prior_currency','prior_balance','prior_spot_rate',
        'currency_switch_adjustment_usd','onboarding_date','max_dpd','uw_score','name','ein',
        'credit_limit_usd','elig','state_name','city_name','naics_industry_id','is_startup',
        'elig_juris','elig_a','elig_b','elig_c','elig_d','elig_e','elig_f','elig_g','elig_h',
        'elig_i','elig_j','elig_k','elig_l','elig_m','elig_n','elig_o','elig_p','elig_q','elig_r',
        'elig_s','elig_t','elig_u','elig_v','elig_w','elig_x','elig_y','elig_z','elig_aa','elig_bb',
        'elig_cc','elig_dd','elig_ee','elig_ff','elig_gg','elig_hh','elig_ii','elig_jj','elig_kk',
        'elig_ll','elig_mm','elig_nn','elig_oo','elig_pp','elig_qq',
    ]

    def reorder_tape(df):
        # Add any missing columns as blank, drop unexpected extras, enforce order
        for col in TAPE_COL_ORDER:
            if col not in df.columns:
                df[col] = None
        # Keep only columns in TAPE_COL_ORDER (drops assignment_dt, eligible_balance_usd, etc.)
        return df[TAPE_COL_ORDER]

    df_beg = reorder_tape(df_beg)
    df_end = reorder_tape(df_end)

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
