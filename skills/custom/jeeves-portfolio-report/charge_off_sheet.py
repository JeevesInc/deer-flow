"""'Charge-Offs & 90+ DPD' tab for the monthly Portfolio Report (added 2026-11-02, Brian).

Four sections, each sorted by amount (largest first), each with a SUM total and a
check row that must read 0.00 against the Summary cell it decomposes:
  1. LOC charge-offs in period      -> rollforward.charge_off_usd       vs Summary!L17
  2. RPP charge-offs in period      -> mods CY, EK in period, EL TRUE   vs Summary!L60
  3. LOC 90+ DPD at EOP             -> loc, Country!F8:F9 filters       vs Summary!L14+L15
  4. RPP 90-120 DPD at EOP          -> mods CY+DU+DV, EE=4, ER/EL FALSE vs Summary!L58
Names come from dms_mysql_jeeves_raw.companies (charged-off companies are NOT on the
loc tape, so the loc 'name' column cannot be used for sections 1, 2 and 4).
"""
import datetime as dt

import pandas as pd
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side

SHEET_NAME = 'Charge-Offs & 90+ DPD'
CTRY = {484: 'Mexico', 170: 'Colombia', 76: 'Brazil'}
MONEY = '#,##0.00;(#,##0.00);-'


def _d(x):
    return None if x is None or pd.isna(x) else pd.Timestamp(x).date()


def _i(x):
    return None if x is None or pd.isna(x) else int(x)


def _f(x):
    return None if x is None or pd.isna(x) else float(x)


def _truthy(s):
    return s.astype(str).str.lower().isin(['true', '1', 't'])


def add_charge_off_sheet(wb, df_loc, df_rf, df_mods, bop_start, eop, con):
    """Build/replace the tab in `wb`. `con` is an open Redshift connection."""
    loc, rf, mods = df_loc.copy(), df_rf.copy(), df_mods.copy()

    co = rf[pd.to_numeric(rf.charge_off_usd, errors='coerce').fillna(0) != 0].copy()
    mods['charge_off_dt'] = pd.to_datetime(mods['charge_off_dt'])
    rco = mods[_truthy(mods.charge_off_flag)
               & (mods.charge_off_dt >= pd.Timestamp(bop_start))
               & (mods.charge_off_dt <= pd.Timestamp(eop))].copy()
    hd = loc[(pd.to_numeric(loc.balance_usd) > 0) & ~_truthy(loc.charge_off_flag)
             & ~_truthy(loc.is_in_repayment) & (pd.to_numeric(loc.days_past_due) >= 90)].copy()
    rhd = mods[(pd.to_numeric(mods.dq_bucket_daily, errors='coerce') == 4)
               & ~_truthy(mods.is_out_of_policy_repayment) & ~_truthy(mods.charge_off_flag)].copy()
    rhd['amt'] = (rhd.balance_usd.fillna(0) + rhd.uninvoiced_interest_usd.fillna(0)
                  + rhd.uninvoiced_vat_fee_tax_usd.fillna(0))

    ids = sorted({int(i) for i in pd.concat([co.company_id, rco.company_id, rhd.company_id])})
    idl = ','.join(map(str, ids)) or '0'
    cur = con.cursor()
    cur.execute(f"select id, name from dms_mysql_jeeves_raw.companies where id in ({idl})")
    nm = {int(a): b for a, b in cur.fetchall()}
    cur.execute(f"select company_id, charge_off_dt from capital_markets_dm.loc_tape "
                f"where dt = '{eop.isoformat()}' and company_id in ({idl})")
    codt = {int(a): b for a, b in cur.fetchall()}
    cur.close()

    co = co.sort_values('charge_off_usd', ascending=False)
    rco = rco.sort_values('balance_usd', ascending=False)
    hd = hd.sort_values('balance_usd', ascending=False)
    rhd = rhd.sort_values('amt', ascending=False)

    if SHEET_NAME in wb.sheetnames:
        del wb[SHEET_NAME]
    pos = wb.sheetnames.index('Country') + 1 if 'Country' in wb.sheetnames else len(wb.sheetnames)
    ws = wb.create_sheet(SHEET_NAME, index=pos)
    ws['A1'] = 'Charge-Offs and Highly Delinquent Accounts by Name'
    ws['A1'].font = Font(bold=True, size=14)
    ws['A2'] = (f'Period {bop_start:%d-%b-%Y} to {eop:%d-%b-%Y}; delinquency as of {eop:%d-%b-%Y}. '
                f'USD. Sorted by amount, largest first.')
    state = {'r': 4}
    hfill = PatternFill('solid', fgColor='1F3864')
    hfont = Font(bold=True, color='FFFFFF')
    thin = Border(top=Side(style='thin'))

    def section(title, hdrs, rows, check_ref, check_label):
        r = state['r']
        ws.cell(r, 1, title).font = Font(bold=True, size=12)
        r += 1
        for j, h in enumerate(hdrs, 1):
            c = ws.cell(r, j, h)
            c.font, c.fill = hfont, hfill
            c.alignment = Alignment(horizontal='center', wrap_text=True)
        r += 1
        first = r
        for row in rows:
            for j, v in enumerate(row, 1):
                c = ws.cell(r, j, v)
                if isinstance(v, float):
                    c.number_format = MONEY
                if isinstance(v, dt.date):
                    c.number_format = 'yyyy-mm-dd'
            r += 1
        if not rows:
            ws.cell(r, 1, 'None')
            r += 1
        ws.cell(r, 1, f'Total ({len(rows)} accounts)').font = Font(bold=True)
        c = ws.cell(r, 7, f'=SUM(G{first}:G{r-1})' if rows else 0)
        c.number_format, c.font = MONEY, Font(bold=True)
        for j in range(1, 8):
            ws.cell(r, j).border = thin
        tot = r
        r += 1
        ws.cell(r, 1, f'Check vs {check_label} (should be 0.00)')
        ws.cell(r, 7, f'=G{tot}-({check_ref})').number_format = MONEY
        state['r'] = r + 2

    def ctry(x):
        return CTRY.get(_i(x), _i(x))

    section('1. LOC charge-offs in period',
            ['Company ID', 'Name', 'Country', 'Charge-Off Date', 'BOP DPD', 'BOP Balance (USD)', 'Charge-Off (USD)'],
            [[int(x.company_id), nm.get(int(x.company_id)), ctry(x.country_code), _d(codt.get(int(x.company_id))),
              _i(x.bop_days_past_due), _f(x.bop_balance_usd), _f(x.charge_off_usd)] for x in co.itertuples()],
            'Summary!L17', 'Summary!L17')
    section('2. RPP (repayment plan) charge-offs in period',
            ['Company ID', 'Name', 'Country', 'Charge-Off Date', 'RPP Reference', '', 'Charge-Off (USD)'],
            [[int(x.company_id), nm.get(int(x.company_id)), ctry(x.country_code), _d(x.charge_off_dt),
              x.loan_reference_number, None, _f(x.balance_usd)] for x in rco.itertuples()],
            'Summary!L60', 'Summary!L60')
    section('3. LOC accounts 90+ DPD at period end (not charged off, not in RPP)',
            ['Company ID', 'Name', 'Country', 'Delinquent Since', 'DPD', 'Credit Limit (USD)', 'Balance (USD)'],
            [[int(x.company_id), x.name, ctry(x.country_code), _d(x.delinquent_dt), _i(x.days_past_due),
              _f(x.credit_limit_usd), _f(x.balance_usd)] for x in hd.itertuples()],
            'Summary!L14+Summary!L15', 'Summary!L14+L15')
    section('4. RPP accounts 90-120 DPD at period end',
            ['Company ID', 'Name', 'Country', 'Delinquent Since', 'DPD', 'RPP Reference', 'Balance incl. Uninvoiced (USD)'],
            [[int(x.company_id), nm.get(int(x.company_id)), ctry(x.country_code), _d(x.delinquent_dt),
              _i(x.days_past_due), x.loan_reference_number, _f(x.amt)] for x in rhd.itertuples()],
            'Summary!L58', 'Summary!L58')
    for col, w in zip('ABCDEFG', [12, 48, 11, 15, 9, 20, 22]):
        ws.column_dimensions[col].width = w
    ws.freeze_panes = 'A4'
    return {'loc_co': (len(co), float(co.charge_off_usd.sum())),
            'rpp_co': (len(rco), float(rco.balance_usd.sum())),
            'loc_90': (len(hd), float(hd.balance_usd.sum())),
            'rpp_90': (len(rhd), float(rhd.amt.sum()))}
