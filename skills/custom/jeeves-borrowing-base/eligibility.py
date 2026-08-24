"""Eligibility calculators for US (Bridge) and MX (SOFOM) borrowing bases.

Adapted from eligibility_calculator.py and eligibility_calculator_sofom.py
on the Capital Markets Google Drive.
"""

import pandas as pd
import numpy as np


# ---------------------------------------------------------------------------
# Shared constants
# ---------------------------------------------------------------------------
ELIGIBLE_COUNTRIES = [484, 170, 840, 124, 76]  # MX, CO, US, CA, BR
ELIGIBLE_CURRENCIES = [484, 840, 124, 170, 978, 986]  # MXN, USD, CAD, COP, EUR, BRL
ELIGIBLE_UW_SCORES = ['A', 'B', 'C', 'D']


def _add_common_elig_fields(df):
    """Add the eligibility flag columns common to both US and SOFOM.

    Column order MUST match the template (alphabetical: h-z, aa-qq).
    The template formula tabs reference tape columns by letter position.
    """
    df['elig_juris'] = df['country_code'].isin(ELIGIBLE_COUNTRIES).astype(int)
    df['elig_a'] = df['country_code'].isin(ELIGIBLE_COUNTRIES).astype(int)

    # Placeholder eligibility flags (company_id not null)
    for s in list('bcdefg'):
        df[f'elig_{s}'] = df['company_id'].notna().astype(int)

    # Real eligibility checks — inserted in alphabetical order to match template
    df['elig_f'] = df['currency'].isin(ELIGIBLE_CURRENCIES).astype(int)
    df['elig_g'] = df['company_id'].notna().astype(int)  # placeholder
    df['elig_h'] = (~df['is_in_repayment']).astype(int)
    df['elig_i'] = 1
    df['elig_j'] = (df['days_past_due'] <= 30).astype(int)
    df['elig_k'] = (~df['charge_off_flag']).astype(int)
    df['elig_l'] = (df['max_dpd'] <= 45).astype(int)

    # Remaining single-letter placeholders in order
    for s in list('mnopqrstuvwxyz'):
        df[f'elig_{s}'] = df['company_id'].notna().astype(int)

    # Double-letter flags in alphabetical order
    for s in ['aa', 'bb', 'cc', 'dd']:
        df[f'elig_{s}'] = df['company_id'].notna().astype(int)
    df['elig_ee'] = (
        (df['credit_limit_usd'] - df['total_collateral_amount_usd'].fillna(0)) <= 7_000_000
    ).astype(int)

    # elig_ff is set by the caller (US vs SOFOM have different balance columns)

    for s in ['gg', 'hh', 'ii', 'jj', 'kk']:
        df[f'elig_{s}'] = df['company_id'].notna().astype(int)
    df['elig_ll'] = df['uw_score'].isin(ELIGIBLE_UW_SCORES).astype(int)
    df['elig_mm'] = 1
    for s in ['nn', 'oo', 'pp', 'qq']:
        df[f'elig_{s}'] = df['company_id'].notna().astype(int)

    return df


# ---------------------------------------------------------------------------
# US (Bridge) eligibility
# ---------------------------------------------------------------------------

def calculate_eligibility_fields(df):
    """US eligibility: uses balance_usd for the over-limit check."""
    result = df.copy()
    result = _add_common_elig_fields(result)
    result['elig_ff'] = ((result['card_balance_usd'] + result['jp_principal_balance_usd']) <= (1.05 * result['credit_limit_usd'])).astype(int)

    elig_cols = [c for c in result.columns if c.startswith('elig_')]
    result['elig'] = result[elig_cols].min(axis=1)
    result['eligible_balance_usd'] = result['balance_usd'] * result['elig']
    return result


def calculate_eligibility_fields_sofom(df):
    """SOFOM eligibility: same over-limit check as US, but eligible balance uses sofom_balance_usd."""
    result = df.copy()
    result = _add_common_elig_fields(result)
    result['elig_ff'] = ((result['card_balance_usd'] + result['jp_principal_balance_usd']) <= (1.05 * result['credit_limit_usd'])).astype(int)

    # ---- First Amendment to the MX SPV Loan Agreement (July 14, 2026), Schedule B (dd) ----
    # "Named Account Debtors shall have a credit limit equal to the lesser of (i) $7,000,000
    # and (ii) 10% of the aggregate Receivable Balance of Eligible Receivables."
    # Named Account Debtor = an Account Debtor with an AIG-approved coverage amount, which
    # arrives on the tape as insured_limit_usd (dms_mysql_jeeves_raw.insurance_guarantee_records).
    # The 10% cap is measured against the eligible pool BEFORE (dd) is applied, per day.
    # NOTE: as drafted the criterion addresses Named Account Debtors only, so non-Named
    # debtors carry no credit-limit eligibility test. Confirm with Agent before changing.
    _pre_cols = [c for c in result.columns if c.startswith('elig_')]
    _pre_elig = result[_pre_cols].min(axis=1)
    if 'insured_limit_usd' in result.columns:
        _insured = result['insured_limit_usd'].fillna(0)
    else:
        _insured = pd.Series(0.0, index=result.index)
    _pool = (result['sofom_balance_usd'].fillna(0) * _pre_elig).groupby(result['dt']).transform('sum')
    _cap = np.minimum(7_000_000.0, 0.10 * _pool)
    result['elig_dd'] = np.where(_insured > 0, (result['credit_limit_usd'].fillna(0) <= _cap), True).astype(int)

    elig_cols = [c for c in result.columns if c.startswith('elig_')]
    result['elig'] = result[elig_cols].min(axis=1)
    result['eligible_balance_usd'] = result['sofom_balance_usd'] * result['elig']
    if 'sofom_balance' in result.columns:
        result['eligible_balance'] = result['sofom_balance'] * result['elig']

    # insured_limit_usd MUST be the LAST column (tape!EI). Every concentration formula in
    # the SOFOM master workbook addresses the tape by column letter, so inserting the new
    # field anywhere before the elig_* block would shift EF/CM/CH and silently break the
    # whole Exhibit A/B block. Keep it pinned to the end.
    if 'insured_limit_usd' in result.columns:
        result = result[[c for c in result.columns if c != 'insured_limit_usd'] + ['insured_limit_usd']]
    return result


# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------

def calculate_eligibility_summary(df, balance_col='balance_usd'):
    """Per-criterion summary stats plus an OVERALL_ELIGIBLE row."""
    elig_cols = [c for c in df.columns if c.startswith('elig_')]
    rows = []
    for col in elig_cols:
        total = len(df)
        elig_count = int(df[col].sum())
        total_bal = df[balance_col].sum()
        elig_bal = df.loc[df[col] == 1, balance_col].sum()
        rows.append({
            'eligibility_field': col,
            'eligible_count': elig_count,
            'total_count': total,
            'eligible_count_pct': round(elig_count / total * 100, 2) if total else 0,
            'eligible_balance_usd': round(elig_bal, 2),
            'total_balance_usd': round(total_bal, 2),
            'eligible_balance_pct': round(elig_bal / total_bal * 100, 2) if total_bal else 0,
        })

    summary = pd.DataFrame(rows)

    overall = df[df['elig'] == 1]
    overall_row = {
        'eligibility_field': 'OVERALL_ELIGIBLE',
        'eligible_count': len(overall),
        'total_count': len(df),
        'eligible_count_pct': round(len(overall) / len(df) * 100, 2) if len(df) else 0,
        'eligible_balance_usd': round(overall[balance_col].sum(), 2),
        'total_balance_usd': round(df[balance_col].sum(), 2),
        'eligible_balance_pct': round(overall[balance_col].sum() / df[balance_col].sum() * 100, 2) if df[balance_col].sum() else 0,
    }
    return pd.concat([pd.DataFrame([overall_row]), summary], ignore_index=True)
