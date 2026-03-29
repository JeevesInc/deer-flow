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
    """Add the eligibility flag columns common to both US and SOFOM."""
    df['elig_juris'] = df['country_code'].isin(ELIGIBLE_COUNTRIES).astype(int)
    df['elig_a'] = df['country_code'].isin(ELIGIBLE_COUNTRIES).astype(int)

    # Placeholder eligibility flags (company_id not null)
    for s in list('bcdefg') + list('mnopqrstuvwxyz') + ['aa','bb','cc','dd','gg','hh','ii','jj','kk','nn','oo','pp','qq']:
        df[f'elig_{s}'] = df['company_id'].notna().astype(int)

    df['elig_f'] = df['currency'].isin(ELIGIBLE_CURRENCIES).astype(int)
    df['elig_h'] = (~df['is_in_repayment']).astype(int)
    df['elig_i'] = 1
    df['elig_j'] = (df['days_past_due'] <= 30).astype(int)
    df['elig_k'] = (~df['charge_off_flag']).astype(int)
    df['elig_l'] = (df['max_dpd'] <= 45).astype(int)
    df['elig_ee'] = (df['credit_limit_usd'] <= 5_000_000).astype(int)
    df['elig_ll'] = df['uw_score'].isin(ELIGIBLE_UW_SCORES).astype(int)
    df['elig_mm'] = 1
    return df


# ---------------------------------------------------------------------------
# US (Bridge) eligibility
# ---------------------------------------------------------------------------

def calculate_eligibility_fields(df):
    """US eligibility: uses balance_usd for the over-limit check."""
    result = df.copy()
    result = _add_common_elig_fields(result)
    result['elig_ff'] = (result['balance_usd'] <= (1.05 * result['credit_limit_usd'])).astype(int)

    elig_cols = [c for c in result.columns if c.startswith('elig_')]
    result['elig'] = result[elig_cols].min(axis=1)
    result['eligible_balance_usd'] = result['balance_usd'] * result['elig']
    return result


def calculate_eligibility_fields_sofom(df):
    """SOFOM eligibility: uses sofom_balance_usd for the over-limit check."""
    result = df.copy()
    result = _add_common_elig_fields(result)
    result['elig_ff'] = (result['sofom_balance_usd'] <= (1.05 * result['credit_limit_usd'])).astype(int)

    elig_cols = [c for c in result.columns if c.startswith('elig_')]
    result['elig'] = result[elig_cols].min(axis=1)
    result['eligible_balance_usd'] = result['sofom_balance_usd'] * result['elig']
    if 'sofom_balance' in result.columns:
        result['eligible_balance'] = result['sofom_balance'] * result['elig']
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
