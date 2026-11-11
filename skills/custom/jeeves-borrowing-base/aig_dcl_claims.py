"""AIG DCL claims report -- built from the weekly TEX file.

Implements Pablo Cortina's "AIG DCL Claims Report: Replication Guide"
(Google Doc 1-a9xPA4yge-iZ2_DzwEGWrYAEOXU7xYN4mgwJxSX7ok, shared 2026-11-09) and the
layout of his AIG_DCL_Claims_Report - 20260908 sheet (1zoqv-V0fKgoUE0_cxpTnAOyodH3I9HVCO3ibXPM4D7A).
Called by report_scheduler_cron.run_aig_tex_file right after the SFTP upload, on the exact
file AIG received.

Inputs
  tex      : DataFrame of the TEX file (pipe-delimited, all str) or a path to it
  fx       : DataFrame [currency, fx_date, rate] from cfo-org-kb/sql/aig_dcl_fx_daily.sql
             (rate = USD per 1 unit local; usd = local * rate)
  named    : aig_named_limits.csv next to this file (buyer, tax_id, limit_usd, status)
"""
import os, re
import numpy as np
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
NAMED_CSV = os.path.join(HERE, 'aig_named_limits.csv')
POLICY_START = pd.Timestamp('2026-01-01')
DCL_FLOOR, DCL_CAP, CUSTOMER_MIN = 25_000.0, 1_000_000.0, 25_000.0
SUSPEND_DAYS, CLAIM_DAYS = 60, 180


def _norm(v):
    if v is None or (isinstance(v, float) and np.isnan(v)):
        return None
    s = re.sub(r'[^A-Za-z0-9]', '', str(v)).upper()
    return s or None


def load_named_map(path=NAMED_CSV):
    nl = pd.read_csv(path, dtype={'tax_id': str})
    nl = nl[nl['status'].str.lower() == 'matched']
    return {_norm(t): float(v) for t, v in zip(nl['tax_id'], nl['limit_usd']) if _norm(t)}


def _fx_lookup(fx):
    fx = fx.copy()
    fx['fx_date'] = pd.to_datetime(fx['fx_date'])
    fx['rate'] = pd.to_numeric(fx['rate'])
    return {c: g.set_index('fx_date')['rate'].sort_index() for c, g in fx.groupby(fx['currency'].str.strip())}


def build(tex, fx, named_map=None, as_of=None):
    """Return (report_df, summary_dict, diagnostics_dict)."""
    if isinstance(tex, str):
        tex = pd.read_csv(tex, sep='|', dtype=str, keep_default_na=False)
    named_map = load_named_map() if named_map is None else named_map
    df = tex.copy()
    df = df[df['legalname'].astype(str).str.strip() != ''].copy()
    for c in ['invoicesentdatetime', 'duedate', 'settlementdatetime']:
        df[c] = pd.to_datetime(df[c].replace('', None), format='%m/%d/%Y', errors='coerce')
    for c in ['invoicedamount', 'unpaidamount', 'cashpaidamount', 'days_past_due']:
        df[c] = pd.to_numeric(df[c].replace('', None), errors='coerce').fillna(0.0)

    # -- FX: rate on the invoice's sent date, nearest date if missing --------------
    # Drop run-day (and later) rates: a partial-day average is unreliable, so same-day
    # invoices fall back to the nearest complete day.
    if as_of is not None:
        fx = fx[pd.to_datetime(fx['fx_date']) < pd.Timestamp(as_of).normalize()]
    fxs = _fx_lookup(fx)
    missing_ccy, nearest = set(), [0]
    def rate(ccy, d):
        ccy = str(ccy).strip()
        if ccy == 'USD':
            return 1.0
        s = fxs.get(ccy)
        if s is None or pd.isna(d):
            missing_ccy.add(ccy)
            return np.nan
        k = pd.Timestamp(d.date())
        if k in s.index:
            return float(s.loc[k])
        nearest[0] += 1
        return float(s.iloc[s.index.get_indexer([k], method='nearest')[0]])
    df['fx'] = [rate(c, d) for c, d in zip(df['currency'], df['invoicesentdatetime'])]
    if df['fx'].isna().any():
        raise RuntimeError('No FX rate for currencies: ' + ', '.join(sorted(missing_ccy)))
    df['usdinvoicedamount'] = df['invoicedamount'] * df['fx']
    df['unpaid_usd'] = df['unpaidamount'] * df['fx']

    # -- Customer key ---------------------------------------------------------------
    df['tax_id_norm'] = df['id_number'].apply(_norm)
    blank = df['tax_id_norm'].isna()
    df.loc[blank, 'tax_id_norm'] = 'NOID_' + df.loc[blank, 'id'].astype(str)
    df['_src'] = np.arange(len(df))

    # -- Clearing history + point-in-time DCL ---------------------------------------
    cleared = df[(df.unpaid_usd == 0) & df.settlementdatetime.notna()][
        ['tax_id_norm', 'settlementdatetime', 'usdinvoicedamount']].sort_values(['tax_id_norm', 'settlementdatetime'])
    cleared['running_max_cleared'] = cleared.groupby('tax_id_norm')['usdinvoicedamount'].cummax()
    left = df[df.invoicesentdatetime.notna()].sort_values(['invoicesentdatetime', 'tax_id_norm'], kind='stable').reset_index(drop=True)
    right = cleared.sort_values(['settlementdatetime', 'tax_id_norm'], kind='stable').reset_index(drop=True)[
        ['tax_id_norm', 'settlementdatetime', 'running_max_cleared']].rename(columns={'settlementdatetime': 'settle_ref'})
    m = pd.merge_asof(left, right, left_on='invoicesentdatetime', right_on='settle_ref',
                      by='tax_id_norm', direction='backward')
    m['limit'] = m['running_max_cleared'].fillna(0).clip(lower=DCL_FLOOR, upper=DCL_CAP)
    m['no_clearing_history'] = m['running_max_cleared'].isna()

    # -- Named limit override -------------------------------------------------------
    m['has_named_limit'] = m['tax_id_norm'].isin(named_map)
    m.loc[m.has_named_limit, 'limit'] = m.loc[m.has_named_limit, 'tax_id_norm'].map(named_map)

    # -- 60-DPD suspension (all rows, incl. pre-2026) -------------------------------
    first_unpaid = df[df.unpaid_usd > 0].groupby('tax_id_norm')['duedate'].min()
    cutoff = (first_unpaid + pd.Timedelta(days=SUSPEND_DAYS)).rename('cutoff')
    m = m.merge(cutoff, left_on='tax_id_norm', right_index=True, how='left')
    m['suspended'] = m.cutoff.notna() & (m.invoicesentdatetime > m.cutoff)
    m.loc[m.suspended, 'limit'] = 0.0

    # -- Candidates + depletion (source order breaks same-day ties) ------------------
    cand = m[(m.invoicesentdatetime >= POLICY_START) & (m.unpaid_usd > 0)] \
        .sort_values(['tax_id_norm', 'invoicesentdatetime', '_src'], kind='stable').reset_index(drop=True)
    used, ub, cl = {}, [], []
    for t, u_usd, lim in zip(cand.tax_id_norm, cand.unpaid_usd, cand.limit):
        u = used.get(t, 0.0)
        c = min(u_usd, max(lim - u, 0.0))
        used[t] = u + c
        ub.append(u); cl.append(c)
    cand['used_before'] = ub
    cand['claimable'] = cl

    agg_claim = cand.groupby('tax_id_norm')['claimable'].sum()
    agg_unpaid = cand.groupby('tax_id_norm')['unpaid_usd'].sum()
    keep = agg_claim[agg_claim >= CUSTOMER_MIN].index
    rep = cand[cand.tax_id_norm.isin(keep)].copy()
    rep['cust_unpaid'] = rep.tax_id_norm.map(agg_unpaid)
    rep['cust_claimable'] = rep.tax_id_norm.map(agg_claim)
    rep['claim_first_date'] = rep['duedate'] + pd.Timedelta(days=CLAIM_DAYS)
    rep['capped'] = rep.unpaid_usd > rep.claimable + 1e-9
    rep['depleted'] = rep.used_before > 0
    rep['past_due'] = rep.days_past_due > 0
    rep = rep.sort_values(['days_past_due', 'claim_first_date'], ascending=[False, True], kind='stable').reset_index(drop=True)

    pre_policy = int(((df.unpaid_usd > 0) & (df.invoicesentdatetime < POLICY_START)).sum())
    as_of = pd.Timestamp(as_of) if as_of is not None else df['invoicesentdatetime'].max()
    if 'is_in_repayment' in rep:
        rpp = rep['is_in_repayment'].astype(str).str.strip() == '1'
    else:
        rpp = pd.Series(False, index=rep.index)
    summary = {
        'as_of': as_of,
        'eligible': len(rep),
        'past_due': int(rep.past_due.sum()),
        'not_yet_due': int((~rep.past_due).sum()),
        'total_unpaid': float(rep.unpaid_usd.sum()),
        'total_claimable': float(rep.claimable.sum()),
        'capped': int(rep.capped.sum()),
        'no_history_non_named': int((rep.no_clearing_history & ~rep.has_named_limit).sum()),
        'suspended': int(rep.suspended.sum()),
        'pre_policy_excluded': pre_policy,
        'named_invoices': int(rep.has_named_limit.sum()),
        'named_customers': int(rep.loc[rep.has_named_limit, 'tax_id_norm'].nunique()),
        'depleted': int(rep.depleted.sum()),
        'customers': int(rep.tax_id_norm.nunique()),
        'rpp_invoices': int(rpp.sum()),
        'rpp_customers': int(rep.loc[rpp, 'tax_id_norm'].nunique()),
        'rpp_claimable': float(rep.loc[rpp, 'claimable'].sum()),
        'claimable_fileable_now': float(rep.loc[rep.claim_first_date <= as_of, 'claimable'].sum()),
        'invoices_fileable_now': int((rep.claim_first_date <= as_of).sum()),
    }
    diag = {'fx_nearest_date_fallbacks': nearest[0], 'rows_in': len(tex), 'candidates': len(cand)}
    return rep, summary, diag


COLS = [
    ('Customer', 'legalname'), ('Org ID (billing entity)', 'id'), ('Tax ID (DCL key)', 'tax_id_norm'),
    ('Tax ID (as reported)', 'id_number'), ('Country', 'country'), ('Original Currency', 'currency'),
    ('Invoice Sent Date', 'invoicesentdatetime'), ('Due Date', 'duedate'),
    ('Invoice Amount (USD)', 'usdinvoicedamount'), ('Unpaid Amount (USD)', 'unpaid_usd'),
    ('Customer Total Unpaid (USD)', 'cust_unpaid'), ('Customer Total Claimable (USD)', 'cust_claimable'),
    ('AIG Named Limit Customer?', 'has_named_limit'), ('Coverage Limit at Issuance (USD)', 'limit'),
    ('Coverage Used by Prior Claims (USD)', 'used_before'), ('Claimable Amount (USD)', 'claimable'),
    ('Capped by Limit?', 'capped'), ('Limit Depleted by Prior Claims?', 'depleted'),
    ('No Prior Clearing History', 'no_clearing_history'), ('60-DPD Coverage Suspended?', 'suspended'),
    ('Customer DCL Suspension Cutoff Date', 'cutoff'), ('Days Past Due (as of {asof})', 'days_past_due'),
    ('Claim First Eligible Date', 'claim_first_date'), ('In Repayment Plan (RPP)?', 'is_in_repayment'),
]

METHOD = [
    ('1. Source', "The TEX file uploaded to AIG/Aronova SFTP this run ({src}). Rows with a blank legalname dropped; dates parsed M/D/YYYY; amounts in local currency."),
    ('2. FX', "Local amounts converted at the average conversion_send_to_usd per currency per calendar day of the invoice sent date (master_transactions_dm.transactions_ssot, is_company_test = FALSE, rate > 0). Nearest available date used when a day is missing ({fx_fb} fallbacks this run). Assumption to confirm: same-day Jeeves average, not a market close."),
    ('3. Customer key', "id_number stripped of non-alphanumerics and upper-cased; blank -> NOID_<org id>. Limits, suspension and depletion all run on this key, so org IDs sharing a tax ID share one pool."),
    ('4. DCL', "Running maximum of fully-cleared invoice USD amounts (unpaid = 0 with a settlement date), point-in-time: only clearings on or before the invoice sent date count (same day included). Floor $25,000, cap $1,000,000. Pre-2026 history counts."),
    ('5. Named limits', "For matched tax IDs the AIG named limit replaces the DCL on every invoice; declined buyers (Grupo Toky, Central de Negocios, Tubosa) = $0; no floor. Match table below; unmatched buyers get no override."),
    ('6. 60-DPD suspension', "Cutoff = earliest due date among the customer's unpaid invoices (all years) + 60 days. Invoices sent after the cutoff get $0 coverage, overriding DCL and named limits. Permanent - not lifted on cure (assumption to confirm)."),
    ('7. Candidates', "Sent on/after 1/1/2026 with unpaid > 0. Pre-policy unpaid invoices are excluded ({pre} this run) but still feed clearing history."),
    ('8. Depletion', "Per customer in sent-date order (source order on ties): claimable = MIN(unpaid, MAX(limit at issuance - used by prior claims, 0)). Assumes every eligible invoice is claimed; does not use actual filed/paid claim status."),
    ('9. Customer threshold', "A customer's candidates are all included only if their total CLAIMABLE >= $25,000 (inclusive); otherwise none are."),
    ('10. Derived', "Claim first eligible date = due date + 180. Capped = unpaid > claimable. Depleted = used before > 0. Past due = file days_past_due > 0. Orange = past due; red = suspended (priority); bold teal = named limit; bold red = depleted."),
    ('11. RPP rows', "Since 2026-11-09 the TEX file includes companies on repayment plans, carrying their LOC invoices up to the last one before the RPP (is_in_repayment = 1). They run through the same rules and are flagged in the last column: {rpp_inv} invoices / {rpp_cus} customers / ${rpp_amt:,.0f} claimable this run."),
]


def write_xlsx(rep, summary, path, src_name, diag=None):
    from openpyxl import Workbook
    from openpyxl.styles import Font, PatternFill, Alignment
    from openpyxl.utils import get_column_letter
    diag = diag or {}
    asof = summary['as_of']
    asof_s = str(asof.month) + '/' + str(asof.day) + '/' + asof.strftime('%y')
    wb = Workbook(); ws = wb.active; ws.title = 'Claims Report'
    ws['A1'] = 'AIG Trade Credit Insurance - DCL-Eligible Invoice & Forecasted Claim Report'
    ws['A1'].font = Font(bold=True, size=14)
    ws['A2'] = ('Source: ' + src_name + '  |  As of: ' + asof.strftime('%B %d, %Y')
                + '  |  Eligibility: customer total CLAIMABLE >= $25,000, invoice issued on/after 1/1/2026  |  '
                  'Claims filed at 180 days past due  |  Sorted by days past due (desc), then claim eligibility date (asc)')
    rows = [
        ('Eligible Invoices', summary['eligible']),
        ('  -- Already Past Due (highlighted below)', summary['past_due']),
        ('  -- Not Yet Due', summary['not_yet_due']),
        ('Total Unpaid (USD)', summary['total_unpaid']),
        ('Total Claimable Under Coverage Limit (USD)', summary['total_claimable']),
        ('Invoices Capped by Coverage Limit', summary['capped']),
        ('Invoices w/ No Prior Clearing History (DCL = $25,000 floor, non-Named-Limit only)', summary['no_history_non_named']),
        ('Invoices w/ Coverage Suspended (60+ DPD trigger)', summary['suspended']),
        ('Excluded: Issued Before Policy Effective Date (1/1/26)', summary['pre_policy_excluded']),
        ('AIG Named-Limit Invoices / Customers', str(summary['named_invoices']) + ' / ' + str(summary['named_customers'])),
        ('Invoices w/ Claim Reduced by Prior Claims (Limit Depleted)', summary['depleted']),
        ('RPP Invoices / Customers (is_in_repayment = 1)', str(summary['rpp_invoices']) + ' / ' + str(summary['rpp_customers'])),
    ]
    for i, (k, v) in enumerate(rows, start=3):
        ws.cell(i, 1, k); c = ws.cell(i, 2, v)
        if isinstance(v, float):
            c.number_format = '$#,##0'
    note_r = 3 + len(rows)
    ws.cell(note_r, 1, 'Orange = past due as of ' + asof_s + ".   Red = DCL coverage suspended (issued after customer's 60-DPD trigger cutoff).").font = Font(italic=True)
    hdr_r = note_r + 1
    hdr_fill = PatternFill('solid', fgColor='1F3864')
    for j, (h, _) in enumerate(COLS, start=1):
        c = ws.cell(hdr_r, j, h.format(asof=asof_s))
        c.font = Font(bold=True, color='FFFFFF'); c.fill = hdr_fill
        c.alignment = Alignment(wrap_text=True, vertical='center')
    orange = PatternFill('solid', fgColor='FCE4D6'); red = PatternFill('solid', fgColor='F4B6B6')
    money = {'usdinvoicedamount', 'unpaid_usd', 'cust_unpaid', 'cust_claimable', 'limit', 'used_before', 'claimable'}
    dates = {'invoicesentdatetime', 'duedate', 'cutoff', 'claim_first_date'}
    flags = {'has_named_limit', 'capped', 'depleted', 'no_clearing_history', 'suspended'}
    recs = rep.to_dict('records')
    for i, rd in enumerate(recs, start=hdr_r + 1):
        fill = red if rd['suspended'] else (orange if rd['past_due'] else None)
        font = Font(bold=True, color='C00000') if rd['depleted'] else (Font(bold=True, color='008080') if rd['has_named_limit'] else None)
        for j, (_, k) in enumerate(COLS, start=1):
            v = rd.get(k)
            if k in flags:
                v = 'Yes' if bool(v) else 'No'
            elif k == 'is_in_repayment':
                v = 'Yes' if str(v).strip() == '1' else 'No'
            elif k in dates:
                v = None if pd.isna(v) else pd.Timestamp(v).to_pydatetime()
            elif k == 'id':
                v = int(v) if str(v).isdigit() else v
            elif k == 'days_past_due':
                v = float(v)
            elif k in money:
                v = float(v)
            c = ws.cell(i, j, v)
            if k in money: c.number_format = '#,##0.00'
            if k in dates: c.number_format = 'm/d/yyyy'
            if fill: c.fill = fill
            if font: c.font = font
    for j in range(1, len(COLS) + 1):
        ws.column_dimensions[get_column_letter(j)].width = 34 if j == 1 else 16
    ws.freeze_panes = ws.cell(hdr_r + 1, 2)

    mth = wb.create_sheet('Methodology & Assumptions')
    mth['A1'] = 'AIG DCL-Eligible Invoice & Forecasted Claim Report - Methodology'; mth['A1'].font = Font(bold=True, size=13)
    mth['A2'] = "Logic per Pablo Cortina's 'AIG DCL Claims Report: Replication Guide'; built automatically after each weekly TEX upload."
    r = 4
    fmt = dict(src=src_name, fx_fb=diag.get('fx_nearest_date_fallbacks', 0), pre=summary['pre_policy_excluded'],
               rpp_inv=summary['rpp_invoices'], rpp_cus=summary['rpp_customers'], rpp_amt=summary['rpp_claimable'])
    for h, t in METHOD:
        mth.cell(r, 1, h).font = Font(bold=True); r += 1
        mth.cell(r, 1, t.format(**fmt)).alignment = Alignment(wrap_text=True); r += 2
    mth.cell(r, 1, 'Named Limit match table (Buyer Name -> Tax ID used, and matched status)').font = Font(bold=True); r += 1
    nl = pd.read_csv(NAMED_CSV, dtype={'tax_id': str})
    for j, h in enumerate(['Buyer Name', 'Matched Tax ID', 'Named Limit (USD)', 'Status'], start=1):
        mth.cell(r, j, h).font = Font(bold=True)
    for row in nl.itertuples(index=False):
        r += 1
        mth.cell(r, 1, row.buyer); mth.cell(r, 2, row.tax_id)
        mth.cell(r, 3, float(row.limit_usd)).number_format = '$#,##0'; mth.cell(r, 4, row.status)
    mth.column_dimensions['A'].width = 120
    for col in 'BCD':
        mth.column_dimensions[col].width = 22
    wb.save(path)
    return path
