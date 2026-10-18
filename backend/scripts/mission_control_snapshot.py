#!/usr/bin/env python3
"""Capital Markets Mission Control - daily snapshot writer.

Built 2026-10-04 for Alex Melikian's "daily borrowing base dashboard / mission control"
ask (gmail 1a10957b9386a6e6). Writes one as-of snapshot per day into Redshift
(analytics_sandbox.cm_mc_*), which Metabase dashboard "Capital Markets Mission Control"
reads. Live leading-indicator cards query capital_markets_dm.loc_tape directly.

Why a snapshot (not pure live SQL): the borrowing base needs inputs that are NOT in
Redshift - controlled-account cash (CICO email), drawn balances (historical_draws),
month-end liquidity, and the MX SOFOM Exhibit A recalculation. This job reuses the SAME
code paths as the lender deliverables so the dashboard never disagrees with a certificate:
  NB receivables   build_nb.pull_tape() + row_flags() + group_key()  (cfo-org-kb data_tape.sql)
  NB concentration nb_covenant_check non-duplicative x/y/z (CA "Borrowing Base" def.)
  NB cash/drawn/   nb_covenant_check.check_workbook() on the latest weekly NB workbook
    liquidity/DQ30
  MX SOFOM         .deer-flow/_cap_markets_state.json (cap_markets_metrics_refresh.py,
                   Exhibit A of the daily SOFOM master) + the master's tape tab for
                   loan-level ineligibility reasons

Facility terms are sourced from the executed agreements (see FACILITY_TERMS).

Usage:  python mission_control_snapshot.py [--as-of YYYY-MM-DD] [--nb-only] [--dry-run]
"""
import argparse
import collections
import datetime as dt
import json
import os
import re
import sys
import traceback

SK = os.environ.get('SKILLS_PATH') or os.path.join(r'C:\Jeeves', 'redshift-bot', 'deer-flow', 'skills')
BB_DIR = os.path.join(SK, 'custom', 'jeeves-borrowing-base')
sys.path.insert(0, os.path.join(SK, 'custom', '_shared'))
sys.path.insert(0, BB_DIR)

DEER = r'C:\Jeeves\redshift-bot\deer-flow'
STATE_DIR = os.path.join(DEER, 'backend', '.deer-flow')
CM_STATE = os.path.join(STATE_DIR, '_cap_markets_state.json')
NB_LIQ_REPORTED = os.path.join(STATE_DIR, 'nb_liquidity_reported.json')
SOFOM_SCRATCH = os.path.join(STATE_DIR, '_cap_markets_scratch')
THREADS = os.path.join(STATE_DIR, 'threads')
SCHEMA = 'analytics_sandbox'
LOAN_RETENTION_DAYS = 35

# ── Facility terms - every value traced to an executed document ─────────────
FACILITY_TERMS = {
    'NB': dict(
        lender='Neuberger Berman (agent: Wilmington Trust)', status='Active',
        commitment=100_000_000.0,   # Initial Loan Commitments $77.5MM + Delayed Draw Loan Commitments $22.5MM (CA defs, 8/10/26)
        cost='Term SOFR (2.50% floor) + 7.50%',
        draw_period_end=dt.date(2028, 5, 10),   # "Delayed Draw Availability Period"
        maturity=dt.date(2028, 8, 10),          # "Maturity Date"
        source='Executed NB Credit Agreement 2026-08-10'),
    'CIM MX SOFOM': dict(
        lender='CIM (MX SPV)', status='Active',
        commitment=150_000_000.0,   # "Maximum Loan Amount" $100MM -> $150MM, Second Amendment 2026-08-10
        cost='1M Term SOFR (3.00% floor) + 9.25% (<= $50MM) / 9.00% (> $50MM)',
        draw_period_end=dt.date(2029, 12, 11),  # "Draw Period" (a), as amended 2026-08-10
        maturity=dt.date(2030, 12, 11),         # "Maturity Date", as amended 2026-08-10
        source='MX SPV Loan Agreement (Dec 2025) as amended by Second Amendment 2026-08-10'),
    'BBVA SBLC': dict(
        lender='BBVA Spark', status='Signed 2026-09-09 - in issuance',
        commitment=14_000_000.0,    # Contrato de Credito line USD 14,000,000
        cost='2.70% BBVA + 0.80% SCB; cash collateral 0.64x',
        draw_period_end=None, maturity=None,   # tenor cap 360 days per SBLC (Clausula Quinta)
        source='Executed BBVA Contrato de Credito 2026-09-09'),
}
NB_CLOSING = dt.date(2026, 8, 10)
NB_STEPDOWN = dt.date(2027, 2, 10)   # Top 5 25%->20%, Top 10 35%->30% after 6 months
NB_ELIG_RATE, NB_STEPUP_RATE, NB_CASH_RATE = 0.90, 0.925, 0.95

NB_REASONS = [  # first-fail order (waterfall bars)
    ('charged_off', 'Charged off'),
    ('repayment_plan', 'Repayment plan / modified'),
    ('mx_spv', 'Encumbered - MX SPV (SOFOM)'),
    ('jurisdiction', 'Ineligible jurisdiction (not US/CO/BR)'),
    ('dpd30', 'DPD > 30'),
]
MX_REASON_LABELS = {
    'elig_juris': 'Ineligible jurisdiction', 'elig_a': 'Ineligible jurisdiction',
    'elig_f': 'Ineligible currency', 'elig_h': 'Repayment plan / modified',
    'elig_j': 'DPD > 30', 'elig_k': 'Charged off', 'elig_l': 'Ever > 45 DPD (max DPD)',
    'elig_ee': 'Credit limit net of collateral > $7MM', 'elig_ff': 'Over limit (> 105% of CL)',
    'elig_dd': 'Named debtor CL > lesser of $7MM / 10% pool', 'elig_ll': 'UW score not A-D',
}
# Exhibit A rows 40-50 (cap_markets_metrics_refresh reads them in this order). Labels taken
# from Exhibit A column B of the SOFOM master (2026-10-03).
MX_TEST_ROWS = [
    ('(a) Named Account Debtors <= 150% of policy limit', 'concentration'),
    ('(a)(i) Single obligor (pool < $40MM) <= 7.5%', 'concentration'),
    ('(a)(ii) Single obligor <= lesser of $4.5MM / 5%', 'concentration'),
    ('(b)(i) Top 3 obligors (pool < $40MM) <= 15%', 'concentration'),
    ('(b)(ii) Top 3 obligors <= lesser of $6MM / 10%', 'concentration'),
    ('(c) Non-named debtors with CL > $2MM <= 30%', 'concentration'),
    ('(d) Risk score D <= 10%', 'concentration'),
    ('(e) Start-ups <= 20%', 'concentration'),
    ('(f) Single industry <= 30%', 'concentration'),
    ('(g) High-risk industry <= 10%', 'concentration'),
    ('(h) High-risk provinces <= 10%', 'concentration'),
    ('(i) Onboarded in last 6 months <= 30%', 'concentration'),
]
# state label (cap_markets_metrics_refresh.py, Exhibit A rows 40-51) -> test label
MX_STATE_LABELS = ['(a) Named Account Debtors', 'Single Obligor (<$40M pool)', 'Single Obligor (otherwise)',
                   'Top 3 Obligors (<$40M pool)', 'Top 3 Obligors (otherwise)', 'Non-named CL > $2M', 'Risk Score D',
                   'Start-Ups', 'Single Industry', 'High-Risk Industry', 'High-Risk Provinces', 'Onboarded last 6 months']


def _f(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def log(*a):
    print('[mission_control]', *a, flush=True)


# ── NB ─────────────────────────────────────────────────────────────────────
def latest_nb_workbook(max_stamp=None):
    best = None
    for d, ds, fs in os.walk(THREADS):
        for f in fs:
            m = re.match(r'Jeeves NB Borrowing Base - (?:Form - )?(\d{8})\.xlsx$', f)
            if m and 'outputs' in d and (max_stamp is None or m.group(1) <= max_stamp):
                p = os.path.join(d, f)
                key = (m.group(1), os.path.getmtime(p))
                if best is None or key > best[0]:
                    best = (key, p)
    return best[1] if best else None


def nb_concentration(groups, eligible, as_of):
    """groups: list of net balances (after credit support), desc. CA x/y/z, non-duplicative."""
    top5_lim, top10_lim = (0.25, 0.35) if as_of <= NB_STEPDOWN else (0.20, 0.30)
    c1 = sum(groups[:1]); c5 = sum(groups[:5]); c10 = sum(groups[:10])
    e1 = max(0.0, c1 - 0.05 * eligible)
    e5 = max(0.0, c5 - e1 - top5_lim * eligible)
    e10 = max(0.0, c10 - (e1 + e5) - top10_lim * eligible)
    return dict(c1=c1, c5=c5, c10=c10, e1=e1, e5=e5, e10=e10,
                top5_lim=top5_lim, top10_lim=top10_lim)


def build_nb(as_of, wb_metrics):
    import build_nb as bn
    tape = bn.pull_tape(as_of.isoformat())
    log('NB tape rows', len(tape))
    loans, gross = [], 0.0
    reason_amt = collections.OrderedDict((k, 0.0) for k, _ in NB_REASONS)
    elig_bal, fx_exposed = 0.0, 0.0
    dq_tot = dq_over = 0.0
    bal_g, coll_g, meta = collections.defaultdict(float), collections.defaultdict(dict), {}
    for r in tape:
        bal = _f(r.get('balance_usd'))
        notco, unenc, iv, v, vi, pos = bn.row_flags(r)
        # daily DQ30 spot (same basis as the NB daily reading: total balance, ex CO, ex RPP, unencumbered)
        if notco and vi and unenc:
            dq_tot += bal
            if _f(r.get('days_past_due')) > 30:
                dq_over += bal
        if bal <= 0:
            continue
        gross += bal
        reason = None
        for key, ok in (('charged_off', notco), ('repayment_plan', vi), ('mx_spv', unenc),
                        ('jurisdiction', v), ('dpd30', iv)):
            if not ok:
                reason = key
                break
        if reason:
            reason_amt[reason] += bal
        else:
            elig_bal += bal
            if str(r.get('currency')) not in ('840', '840.0'):
                fx_exposed += bal
            k = bn.group_key(r)
            bal_g[k] += bal
            coll_g[k][r['company_id']] = _f(r.get('total_collateral_amount_usd'))
            meta.setdefault(k, (r.get('name'), bn.COUNTRY_NAMES.get(bn._i(r['country_code']), str(r['country_code']))))
        loans.append(dict(loan_id=str(r.get('loan_id')), company_id=str(r.get('company_id')),
                          name=(r.get('name') or '')[:250],
                          country=bn.COUNTRY_NAMES.get(bn._i(r['country_code']), str(r['country_code'])),
                          product=str(r.get('product') or '')[:50], dpd=int(_f(r.get('days_past_due'))),
                          balance_usd=bal, eligible=0 if reason else 1,
                          reason=dict(NB_REASONS)[reason] if reason else 'Eligible'))
    obligors = []
    for k, b in bal_g.items():
        c = sum(coll_g[k].values())
        obligors.append(dict(group_key=k, name=(meta[k][0] or '')[:250], country=meta[k][1],
                             balance=b, credit_support=c, net=b - c))
    obligors.sort(key=lambda o: -o['net'])
    nets = [o['net'] for o in obligors]
    conc = nb_concentration(nets, elig_bal, as_of)
    conc_sd = nb_concentration(nets, elig_bal, NB_STEPDOWN + dt.timedelta(days=1))
    excess = conc['e1'] + conc['e5'] + conc['e10']
    excess_sd = conc_sd['e1'] + conc_sd['e5'] + conc_sd['e10']
    counted = elig_bal - excess
    rate = wb_metrics.get('advance_rate') or NB_ELIG_RATE
    recv_bb = counted * rate
    cash_bb = NB_CASH_RATE * _f(wb_metrics.get('bb_cash'))
    bb = recv_bb + cash_bb
    drawn = _f(wb_metrics.get('borrowings'))
    commit = FACILITY_TERMS['NB']['commitment']
    bb_excess = bb - drawn
    undrawn = commit - drawn
    availability = min(bb_excess, undrawn)
    binding = ('Commitment (undrawn DDTL)' if undrawn <= bb_excess else 'Borrowing base')
    if bb_excess < 0:
        binding = 'Borrowing base DEFICIT'
    t = FACILITY_TERMS['NB']
    fac = dict(facility='NB', lender=t['lender'], status=t['status'], commitment=commit, drawn=drawn,
               gross_receivables=gross, ineligible=sum(reason_amt.values()), eligible=elig_bal,
               excess_concentration=excess, receivables_counted=counted, advance_rate=rate,
               receivable_bb=recv_bb, cash_bb=cash_bb, other_bb=0.0, borrowing_base=bb,
               bb_excess=bb_excess, undrawn_commitment=undrawn, availability=availability,
               binding_constraint=binding, cost=t['cost'], draw_period_end=t['draw_period_end'],
               maturity=t['maturity'], fx_exposed_share=(fx_exposed / elig_bal if elig_bal else 0),
               excess_pct_stepdown=(excess_sd / elig_bal if elig_bal else 0),
               data_as_of='Receivables {} (live tape); cash/drawn/liquidity from weekly BB as of {}'.format(
                   as_of, wb_metrics.get('_wb_as_of')),
               source=t['source'] + '; ' + os.path.basename(wb_metrics.get('_wb_path', '')))
    wf = [('Gross receivables', gross, 'total')]
    for key, label in NB_REASONS:
        wf.append(('- ' + label, -reason_amt[key], 'decrease'))
    wf += [('Eligible receivables', elig_bal, 'total'),
           ('- Excess conc: largest obligor > 5%', -conc['e1'], 'decrease'),
           ('- Excess conc: top 5 > {:.0%}'.format(conc['top5_lim']), -conc['e5'], 'decrease'),
           ('- Excess conc: top 10 > {:.0%}'.format(conc['top10_lim']), -conc['e10'], 'decrease'),
           ('Receivables counted', counted, 'total'),
           ('- Advance rate haircut ({:.1%})'.format(1 - rate), -(counted - recv_bb), 'decrease'),
           ('+ Controlled-account cash @ 95%', cash_bb, 'increase'),
           ('Borrowing base', bb, 'total'),
           ('- Drawn', -drawn, 'decrease'),
           ('BB excess / (deficit)', bb_excess, 'total')]
    # tests
    tests = []
    full_avail = max(availability, 0.0)
    liq, floor = _f(wb_metrics.get('liquidity')), _f(wb_metrics.get('liquidity_floor'))
    liq_src = 'NB BB as of {} (test date {})'.format(wb_metrics.get('_wb_as_of'), wb_metrics.get('liquidity_test_date'))
    # 7.01(a) is a MONTH-END test: show the figure actually reported to NB for the latest month-end
    # (nb_liquidity_reported.json, maintained by hand), not a recomputation off the weekly workbook.
    # Schema (2026-10-16, shared with build_nb.py's month-end pin): {test_date: {liquidity, source,
    # accounts, [floor]}}. The legacy flat {test_date, liquidity, floor, source} form is still accepted.
    try:
        rep = json.load(open(NB_LIQ_REPORTED, encoding='utf-8'))
        if 'liquidity' in rep:
            rep = {rep['test_date']: rep}
        dates = sorted(k for k in rep if k <= str(as_of) and isinstance(rep[k], dict) and 'liquidity' in rep[k])
        if dates:
            td = dates[-1]
            r = rep[td]
            liq = float(r['liquidity'])
            if r.get('floor') is not None:
                floor = float(r['floor'])
            elif td < '2026-10-23':
                floor = 15000000.0   # pre step-up: borrowings < $100MM until the $20.5M draw funds 10/23
            # else keep the workbook floor (steps to $25MM once borrowings >= $100MM)
            liq_src = 'Reported to NB: {} (test date {})'.format(r.get('source', ''), td)
        else:
            print('[mission_control] WARN no reported 7.01(a) entry <= {}; using weekly workbook'.format(as_of))
    except FileNotFoundError:
        pass
    tests.append(dict(test='7.01(a) Minimum Liquidity (month-end)', category='covenant', actual=liq,
                      threshold=floor, direction='min', unit='usd', headroom=liq - floor,
                      headroom_usd=liq - floor, availability_lost_if_breached_usd=full_avail,
                      consequence='Event of Default - draws blocked; floor steps to $25MM once borrowings >= $100MM',
                      source=liq_src))
    dqa = wb_metrics.get('dq30_avg')
    tests.append(dict(test='7.01(b) DQ30 Rate, 3-mo avg (month-end)', category='covenant',
                      actual=dqa, threshold=0.025, direction='max', unit='pct',
                      headroom=(0.025 - dqa) if dqa is not None else None,
                      headroom_usd=((0.025 - dqa) * dq_tot) if dqa is not None else None,
                      availability_lost_if_breached_usd=full_avail,
                      consequence='Event of Default - draws blocked',
                      source='NB BB as of {}: {}'.format(wb_metrics.get('_wb_as_of'),
                              ', '.join('{} {:.2%}'.format(m, v) for m, v in wb_metrics.get('dq30_monthly') or [] if v is not None))))
    spot = dq_over / dq_tot if dq_tot else None
    tests.append(dict(test='DQ30 spot (daily early warning, not a test point)', category='trigger',
                      actual=spot, threshold=0.025, direction='max', unit='pct',
                      headroom=(0.025 - spot) if spot is not None else None,
                      headroom_usd=((0.025 - spot) * dq_tot) if spot is not None else None,
                      availability_lost_if_breached_usd=None,
                      consequence='Feeds the month-end 3-mo average',
                      source='loc_tape {} (unencumbered, ex CO / RPP)'.format(as_of)))
    tests.append(dict(test='Advance-rate step-up: DQ30 < 1.50% x3 months', category='upside',
                      actual=dqa, threshold=0.015, direction='max', unit='pct',
                      headroom=(0.015 - dqa) if dqa is not None else None, headroom_usd=None,
                      availability_lost_if_breached_usd=-(NB_STEPUP_RATE - NB_ELIG_RATE) * counted,
                      consequence='Upside: 90% -> 92.5% adds ${:,.0f} of BB (also needs Liquidity >= floor + $2.5MM, agent confirmation)'.format(
                          (NB_STEPUP_RATE - NB_ELIG_RATE) * counted),
                      source='CA "Receivables Advance Step-Up Event"'))
    # CA x/y/z are sequential: top-5 is measured after the largest-obligor exclusion, top-10 after both
    for lbl, cval, lim, exc in (('Largest obligor', conc['c1'], 0.05, conc['e1']),
                                ('Top 5 obligors (after x)', conc['c5'] - conc['e1'], conc['top5_lim'], conc['e5']),
                                ('Top 10 obligors (after x, y)', conc['c10'] - conc['e1'] - conc['e5'], conc['top10_lim'], conc['e10'])):
        pct = cval / elig_bal if elig_bal else 0
        tests.append(dict(test='Concentration: {} <= {:.0%}'.format(lbl, lim), category='concentration',
                          actual=pct, threshold=lim, direction='max', unit='pct', headroom=lim - pct,
                          headroom_usd=(lim - pct) * elig_bal, availability_lost_if_breached_usd=exc * rate,
                          consequence='Excess over limit is excluded from the base (lost now: ${:,.0f})'.format(exc * rate),
                          source='Live tape; obligors grouped by EIN, net of credit support'))
    tests.append(dict(test='Concentration step-down 2027-02-10 (top5 20% / top10 30%)', category='scheduled',
                      actual=excess_sd / elig_bal if elig_bal else 0, threshold=None, direction='max', unit='pct',
                      headroom=None, headroom_usd=None,
                      availability_lost_if_breached_usd=(excess_sd - excess) * rate,
                      consequence='On today\'s book the step-down removes ${:,.0f} of availability'.format((excess_sd - excess) * rate),
                      source='CA Top Five / Top Ten Obligor Concentration Limit'))
    tests.append(dict(test='Borrowing base covers borrowings', category='covenant', actual=bb, threshold=drawn,
                      direction='min', unit='usd', headroom=bb_excess, headroom_usd=bb_excess,
                      availability_lost_if_breached_usd=full_avail,
                      consequence='Mandatory prepayment of the deficit', source='This snapshot'))
    for o_i, o in enumerate(obligors):
        o['rank'] = o_i + 1
        o['pct_of_eligible'] = o['net'] / elig_bal if elig_bal else 0
    return fac, wf, tests, loans, obligors[:50]


# ── MX SOFOM ───────────────────────────────────────────────────────────────
def build_mx():
    st = json.load(open(CM_STATE, encoding='utf-8'))
    mx = st.get('mx_sofom') or {}
    if mx.get('borrowing_base') is None:
        raise RuntimeError('mx_sofom.borrowing_base missing in _cap_markets_state.json')
    fx = _f(mx['usdmxn_rate'])
    tape_dt = (mx.get('tape_as_of') or '')[:10]
    gross = _f(mx['total_receivables_mxn']) / fx
    inel = _f(mx['ineligible_mxn']) / fx
    elig = _f(mx['eligible_mxn']) / fx
    counted = _f(mx['receivables_counted_mxn']) / fx
    cash_bb, swap_bb = _f(mx.get('cash_bb_usd')), _f(mx.get('swap_bb_usd'))
    bb = _f(mx['borrowing_base'])
    drawn = _f(mx['total_drawn'])
    recv_bb = bb - cash_bb - swap_bb
    rate = recv_bb / counted if counted else None
    commit = FACILITY_TERMS['CIM MX SOFOM']['commitment']
    bb_excess = bb - drawn
    undrawn = (commit - drawn) if commit else None
    availability = min(bb_excess, undrawn) if undrawn is not None else bb_excess
    # loan-level reasons from the master tape tab (latest dt)
    loans, reason_amt = [], collections.OrderedDict()
    master = os.path.join(SOFOM_SCRATCH, 'Jeeves SOFOM Borrowing Base - Master - {}.xlsx'.format(
        (dt.date.fromisoformat(tape_dt) if tape_dt else dt.date.today()).strftime('%Y%m%d')))
    if os.path.exists(master):
        from openpyxl import load_workbook
        wb = load_workbook(master, read_only=True)
        ws = wb['tape']
        it = ws.iter_rows(values_only=True)
        h = list(next(it))
        ix = {c: i for i, c in enumerate(h) if c}
        ecols = [c for c in h if c and str(c).startswith('elig_')]
        rows = [r for r in it if r and r[ix['dt']] is not None]
        mdt = max(r[ix['dt']] for r in rows)
        for r in rows:
            if r[ix['dt']] != mdt:
                continue
            bal = _f(r[ix['sofom_balance_usd']])
            if bal <= 0:
                continue
            reason = None
            for c in ecols:
                if r[ix[c]] in (0, False):
                    reason = MX_REASON_LABELS.get(c, 'Eligibility clause ' + c.replace('elig_', '(') + ')')
                    break
            if reason:
                reason_amt[reason] = reason_amt.get(reason, 0.0) + bal
            loans.append(dict(loan_id=str(r[ix['loan_id']]), company_id=str(r[ix['company_id']]),
                              name=str(r[ix['name']] or '')[:250], country='Mexico',
                              product=str(r[ix['product']] or '')[:50], dpd=int(_f(r[ix['days_past_due']])),
                              balance_usd=bal, eligible=0 if reason else 1, reason=reason or 'Eligible'))
        log('MX master tape', os.path.basename(master), 'dt', mdt, 'loans', len(loans))
    t = FACILITY_TERMS['CIM MX SOFOM']
    fac = dict(facility='CIM MX SOFOM', lender=t['lender'], status=t['status'], commitment=commit,
               drawn=drawn, gross_receivables=gross, ineligible=inel, eligible=elig,
               excess_concentration=elig - counted, receivables_counted=counted, advance_rate=rate,
               receivable_bb=recv_bb, cash_bb=cash_bb, other_bb=swap_bb, borrowing_base=bb,
               bb_excess=bb_excess, undrawn_commitment=undrawn, availability=availability,
               binding_constraint='Borrowing base DEFICIT' if bb_excess < 0 else (
                   'Borrowing base' if (undrawn is None or bb_excess <= undrawn) else 'Commitment'),
               cost=t['cost'], draw_period_end=t['draw_period_end'], maturity=t['maturity'],
               fx_exposed_share=1.0, excess_pct_stepdown=None,
               data_as_of='SOFOM master Exhibit A, tape {} @ USDMXN {}'.format(tape_dt, fx),
               source=t['source'] + '; ' + str(mx.get('source_file')))
    wf = [('Gross receivables', gross, 'total')]
    tape_inel = sum(reason_amt.values())
    for k, v in sorted(reason_amt.items(), key=lambda kv: -kv[1]):
        wf.append(('- ' + k, -v, 'decrease'))
    if abs(inel - tape_inel) > 1:
        wf.append(('- Other ineligible (Exhibit A vs tape reconciling)', -(inel - tape_inel), 'decrease'))
    wf += [('Eligible receivables', elig, 'total'),
           ('- Excess concentration (Exhibit A)', -(elig - counted), 'decrease'),
           ('Receivables counted', counted, 'total'),
           ('- Advance rate haircut ({:.1%})'.format(1 - rate if rate else 0), -(counted - recv_bb), 'decrease'),
           ('+ Collection-account cash', cash_bb, 'increase'),
           ('+ Swap contract value', swap_bb, 'increase'),
           ('Borrowing base', bb, 'total'),
           ('- Drawn', -drawn, 'decrease'),
           ('BB excess / (deficit)', bb_excess, 'total')]
    tests = []
    covs = mx.get('covenants') or []
    by_label = {c.get('test'): c for c in covs}
    matched = [(row, by_label[sl]) for row, sl in zip(MX_TEST_ROWS, MX_STATE_LABELS) if sl in by_label]
    if len(matched) >= len(MX_TEST_ROWS) - 1:
        for (label, cat), c in matched:
            a = c.get('actual_pct'); lim = c.get('limit_pct')
            exc_usd = _f(c.get('excess_mxn')) / fx
            tests.append(dict(test=label, category=cat, actual=(a / 100) if a is not None else None,
                              threshold=(lim / 100) if lim is not None else None, direction='max', unit='pct',
                              headroom=((lim - a) / 100) if (a is not None and lim is not None) else None,
                              headroom_usd=(((lim - a) / 100) * elig) if (a is not None and lim is not None) else None,
                              availability_lost_if_breached_usd=exc_usd * (rate or 0),
                              consequence='Excess excluded from the base (lost now: ${:,.0f})'.format(exc_usd * (rate or 0)),
                              source='SOFOM master Exhibit A row, tape {}'.format(tape_dt)))
    else:
        log('WARN MX covenant labels matched', len(matched), 'of', len(MX_TEST_ROWS), '- skipped (fail-closed)')
    tests.append(dict(test='Payment Rate 3-mo avg >= 98% (for 90% advance)', category='trigger',
                      actual=None, threshold=0.98, direction='min', unit='pct', headroom=None, headroom_usd=None,
                      availability_lost_if_breached_usd=0.05 * counted,
                      consequence='< 98%: advance 90% -> 85%; < 95%: 80%. Exhibit A L62 is a manual input; flow-based method under discussion with CIM',
                      source='MX LA "Advance Rate"'))
    tests.append(dict(test='Net Portfolio Yield 3-mo avg >= 0%', category='trigger', actual=None, threshold=0.0,
                      direction='min', unit='pct', headroom=None, headroom_usd=None,
                      availability_lost_if_breached_usd=0.10 * counted,
                      consequence='NPY < 0: advance rate drops to 80%. Exhibit A L63 is a manual input',
                      source='MX LA "Advance Rate"'))
    tests.append(dict(test='EoD trigger: Payment Rate 3-mo avg >= 90%', category='covenant', actual=None,
                      threshold=0.90, direction='min', unit='pct', headroom=None, headroom_usd=None,
                      availability_lost_if_breached_usd=max(availability, 0.0),
                      consequence='Event of Default Trigger Event - draw period ends at Agent discretion',
                      source='MX LA "Event of Default Trigger Event" (a); Exhibit A L62 manual input'))
    tests.append(dict(test='EoD trigger: Net Portfolio Yield 3-mo avg >= -5%', category='covenant', actual=None,
                      threshold=-0.05, direction='min', unit='pct', headroom=None, headroom_usd=None,
                      availability_lost_if_breached_usd=max(availability, 0.0),
                      consequence='Event of Default Trigger Event (NPY is the excess-spread test in this facility)',
                      source='MX LA "Event of Default Trigger Event" (b); Exhibit A L63 manual input'))
    tests.append(dict(test='Borrowing base covers borrowings', category='covenant', actual=bb, threshold=drawn,
                      direction='min', unit='usd', headroom=bb_excess, headroom_usd=bb_excess,
                      availability_lost_if_breached_usd=max(availability, 0.0),
                      consequence='Sec. 2.6(c): prepay, or add eligible receivables / collection cash, within 3 Business Days',
                      source='Exhibit A L81 vs L83'))
    return fac, wf, tests, loans, tape_dt


def build_recycling(n=30):
    """Monex F/12340 recycling requests (Finance Ops daily email) -> MXN figures per request."""
    sys.path.insert(0, os.path.join(SK, 'custom', 'gmail'))
    import subprocess
    tool = os.path.join(SK, 'custom', 'gmail', 'gmail_tool.py')
    out = subprocess.run([sys.executable, tool, 'search', 'subject:"Recycling Request" from:tryjeeves.com newer_than:60d', str(n)],
                         capture_output=True, text=True, timeout=180).stdout
    ids = re.findall(r'ID:\s*(\w+)', out)
    rows = []
    num = lambda x: float(x.replace(',', ''))
    for mid in ids:
        body = subprocess.run([sys.executable, tool, 'read', mid], capture_output=True, text=True, timeout=120).stdout
        subj = re.search(r'Subject:\s*(?:Re:\s*)?Jeeves (\d+)\w* Recycling Request', body)
        if not subj or re.search(r'Subject:\s*Re:', body):
            continue
        date = re.search(r'Date:\s*(.+)', body).group(1).strip()
        def g(label):
            m = re.search(label + r'\s+(-?\s?[\d,]+\.\d+)', body)
            return num(m.group(1).replace(' ', '')) if m else None
        from email.utils import parsedate_to_datetime
        rows.append(dict(facility='CIM MX SOFOM', request_no=int(subj.group(1)),
                         sent_at=parsedate_to_datetime(date).replace(tzinfo=None),
                         requested_mxn=g(r'Recycle'), current_mxn=g(r'Current'), directed_mxn=g(r'Directed'),
                         recycling_2_mxn=g(r'Recycling -2'), recycling_1_mxn=g(r'Recycling -1'),
                         reqd_mxn=g(r'Reqd'), avail_to_recycle_mxn=g(r'Avail to Recyc'), gmail_id=mid))
    log('recycling requests parsed', len(rows))
    return rows


def bbva_row():
    t = FACILITY_TERMS['BBVA SBLC']
    return dict(facility='BBVA SBLC', lender=t['lender'], status=t['status'], commitment=t['commitment'],
                drawn=None, gross_receivables=None, ineligible=None, eligible=None, excess_concentration=None,
                receivables_counted=None, advance_rate=None, receivable_bb=None, cash_bb=None, other_bb=None,
                borrowing_base=None, bb_excess=None, undrawn_commitment=None, availability=None,
                binding_constraint='Cash collateral (0.64x); not a borrowing-base facility',
                cost=t['cost'], draw_period_end=None, maturity=None, fx_exposed_share=None,
                excess_pct_stepdown=None, data_as_of='Static terms; issued amount not yet in a system of record',
                source=t['source'])


# ── Redshift write ─────────────────────────────────────────────────────────
DDL = {
 'cm_mc_facility_daily': """as_of date, facility varchar(64), lender varchar(128), status varchar(128),
   commitment float8, drawn float8, gross_receivables float8, ineligible float8, eligible float8,
   excess_concentration float8, receivables_counted float8, advance_rate float8, receivable_bb float8,
   cash_bb float8, other_bb float8, borrowing_base float8, bb_excess float8, undrawn_commitment float8,
   availability float8, binding_constraint varchar(128), cost varchar(256), draw_period_end date,
   maturity date, fx_exposed_share float8, excess_pct_stepdown float8, data_as_of varchar(512),
   source varchar(512), loaded_at timestamp""",
 'cm_mc_waterfall_daily': """as_of date, facility varchar(64), step_order int, step varchar(256),
   amount_usd float8, kind varchar(16), loaded_at timestamp""",
 'cm_mc_tests_daily': """as_of date, facility varchar(64), test varchar(256), category varchar(32),
   actual float8, threshold float8, direction varchar(8), unit varchar(8), headroom float8,
   headroom_usd float8, status varchar(16), availability_lost_if_breached_usd float8,
   consequence varchar(512), source varchar(512), loaded_at timestamp""",
 'cm_mc_loans_daily': """as_of date, facility varchar(64), loan_id varchar(64), company_id varchar(64),
   name varchar(256), country varchar(64), product varchar(64), dpd int, balance_usd float8,
   eligible int, reason varchar(256), loaded_at timestamp""",
 'cm_mc_mx_recycling': """as_of date, facility varchar(64), request_no int, sent_at timestamp,
   requested_mxn float8, current_mxn float8, directed_mxn float8, recycling_2_mxn float8,
   recycling_1_mxn float8, reqd_mxn float8, avail_to_recycle_mxn float8, gmail_id varchar(32), loaded_at timestamp""",
 'cm_mc_receivable_forecast': """run_as_of date, dt date, nb_pool float8, mx float8, total float8,
   total_p10 float8, total_p90 float8, holdout_wape float8, loaded_at timestamp""",
 'cm_mc_obligors_daily': """as_of date, facility varchar(64), rank int, group_key varchar(128),
   name varchar(256), country varchar(64), balance float8, credit_support float8, net float8,
   pct_of_eligible float8, loaded_at timestamp""",
}


def status_of(t):
    a, th = t.get('actual'), t.get('threshold')
    if t['category'] in ('upside', 'scheduled', 'forward'):
        return 'INFO'
    if a is None or th is None:
        return 'NO DATA'
    room = (th - a) if t['direction'] == 'max' else (a - th)
    if room < 0:
        # a concentration limit is a haircut, not a default: the excess is already out of the base
        return 'EXCESS' if t['category'] == 'concentration' else 'BREACH'
    scale = abs(th) if th else 1.0
    if t['unit'] == 'pct' and room < min(0.15 * scale, 0.02):
        return 'WATCH'
    if t['unit'] == 'usd' and room < 0.10 * scale:
        return 'WATCH'
    return 'OK'


def write(conn, as_of, facilities, waterfalls, tests, loans, obligors, recycling=()):
    cur = conn.cursor()
    now = dt.datetime.now()
    for tbl, cols in DDL.items():
        cur.execute('create table if not exists {}.{} ({})'.format(SCHEMA, tbl, cols))
    def ins(tbl, rows, keys):
        if not rows:
            return
        cur.execute('delete from {}.{} where as_of = %s and facility in ({})'.format(
            SCHEMA, tbl, ','.join(['%s'] * len({r['facility'] for r in rows}))),
            [as_of] + sorted({r['facility'] for r in rows}))
        allk = ['as_of'] + keys + ['loaded_at']
        for i in range(0, len(rows), 400):
            chunk = rows[i:i + 400]
            ph = ','.join(['(' + ','.join(['%s'] * len(allk)) + ')'] * len(chunk))
            vals = []
            for r in chunk:
                vals += [as_of] + [r.get(k) for k in keys] + [now]
            cur.execute('insert into {}.{} ({}) values {}'.format(SCHEMA, tbl, ','.join(allk), ph), vals)
    fkeys = [c.split()[0] for c in DDL['cm_mc_facility_daily'].replace('\n', ' ').split(',')]
    fkeys = [k for k in fkeys if k not in ('as_of', 'loaded_at')]
    ins('cm_mc_facility_daily', facilities, fkeys)
    ins('cm_mc_waterfall_daily', waterfalls, ['facility', 'step_order', 'step', 'amount_usd', 'kind'])
    ins('cm_mc_tests_daily', tests, ['facility', 'test', 'category', 'actual', 'threshold', 'direction', 'unit',
                                     'headroom', 'headroom_usd', 'status', 'availability_lost_if_breached_usd',
                                     'consequence', 'source'])
    ins('cm_mc_loans_daily', loans, ['facility', 'loan_id', 'company_id', 'name', 'country', 'product', 'dpd',
                                     'balance_usd', 'eligible', 'reason'])
    ins('cm_mc_obligors_daily', obligors, ['facility', 'rank', 'group_key', 'name', 'country', 'balance',
                                           'credit_support', 'net', 'pct_of_eligible'])
    if recycling:
        cur.execute('delete from {}.cm_mc_mx_recycling'.format(SCHEMA))
        ins('cm_mc_mx_recycling', list(recycling), ['facility', 'request_no', 'sent_at', 'requested_mxn', 'current_mxn',
                                                    'directed_mxn', 'recycling_2_mxn', 'recycling_1_mxn', 'reqd_mxn',
                                                    'avail_to_recycle_mxn', 'gmail_id'])
    cur.execute('delete from {}.cm_mc_loans_daily where as_of < %s'.format(SCHEMA),
                [as_of - dt.timedelta(days=LOAN_RETENTION_DAYS)])
    conn.commit()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--as-of', default=None)
    ap.add_argument('--nb-only', action='store_true')
    ap.add_argument('--dry-run', action='store_true')
    a = ap.parse_args()
    as_of = dt.date.fromisoformat(a.as_of) if a.as_of else dt.date.today() - dt.timedelta(days=1)
    if as_of >= dt.date.today():
        sys.exit('as-of must be before today (loc_tape for today is partial)')
    import nb_covenant_check as ncc
    wbp = latest_nb_workbook((as_of + dt.timedelta(days=1)).strftime('%Y%m%d'))
    if not wbp:
        sys.exit('no NB workbook found - fail closed')
    res = ncc.check_workbook(wbp)
    wbm = dict(res['metrics'])
    wbm['_wb_path'] = wbp
    wbm['_wb_as_of'] = (dt.datetime.strptime(re.search(r'(\d{8})', os.path.basename(wbp)).group(1), '%Y%m%d').date()
                        - dt.timedelta(days=1)).isoformat()
    log('NB workbook', os.path.basename(wbp), 'BB', round(wbm['borrowing_base']), 'drawn', wbm['borrowings'])
    facilities, waterfalls, tests, loans, obligors, recycling = [], [], [], [], [], []
    nf, nwf, nt, nl, no = build_nb(as_of, wbm)
    facilities.append(nf)
    waterfalls += [dict(facility='NB', step_order=i + 1, step=s, amount_usd=v, kind=k) for i, (s, v, k) in enumerate(nwf)]
    tests += [dict(t, facility='NB') for t in nt]
    loans += [dict(l, facility='NB') for l in nl]
    obligors += [dict(o, facility='NB') for o in no]
    if not a.nb_only:
        try:
            mf, mwf, mt, ml, _ = build_mx()
            facilities.append(mf)
            waterfalls += [dict(facility='CIM MX SOFOM', step_order=i + 1, step=s, amount_usd=v, kind=k) for i, (s, v, k) in enumerate(mwf)]
            try:
                perf = mx_performance()
                mt = apply_mx_performance(mt, perf, _f((json.load(open(CM_STATE, encoding='utf-8')).get('mx_sofom') or {}).get('receivable_bb_usd')))
            except Exception:
                log('MX performance (Exhibit B) failed: ' + traceback.format_exc())
            tests += [dict(t, facility='CIM MX SOFOM') for t in mt]
            loans += [dict(l, facility='CIM MX SOFOM') for l in ml]
        except Exception:
            log('MX build failed:\n' + traceback.format_exc())
        facilities.append(bbva_row())
        try:
            recycling = build_recycling()
        except Exception:
            recycling = []
            log('recycling parse failed: ' + traceback.format_exc())
    for t in tests:
        t['status'] = status_of(t)
    for f in facilities:
        log('{:14} BB {:>16} drawn {:>14} avail {:>14} binding: {}'.format(
            f['facility'], '{:,.0f}'.format(f['borrowing_base']) if f['borrowing_base'] is not None else '-',
            '{:,.0f}'.format(f['drawn']) if f['drawn'] is not None else '-',
            '{:,.0f}'.format(f['availability']) if f['availability'] is not None else '-', f['binding_constraint']))
    for t in tests:
        log('  [{:7}] {:14} {}'.format(t['status'], t['facility'], t['test']))
    if a.dry_run:
        for w in waterfalls:
            log('   wf', w['facility'], w['step_order'], w['step'], '{:,.0f}'.format(w['amount_usd']))
        return
    import redshift_util as ru
    conn = ru.connect()
    write(conn, as_of, facilities, waterfalls, tests, loans, obligors, recycling)
    if not a.dry_run and not a.nb_only:
        try:
            refresh_forecast(conn, run_model=(dt.date.today().weekday() == 0 or '--forecast' in sys.argv))
        except Exception:
            log('forecast refresh failed: ' + traceback.format_exc())
    log('wrote snapshot', as_of, 'facilities', len(facilities), 'tests', len(tests), 'loans', len(loans))


def mx_performance():
    """Payment Rate and Net Portfolio Yield as CALCULATED in Exhibit B of the latest monthly
    Jeeves Mexico Servicer Report (Drive, CIM Reporting): NPY = 12 x (interest + fee collections
    - WHT - servicing fee - cost of funds - other fees - charge-offs) / average daily balance;
    PR = 1 - 31-60 DPD / total at month-end. Exhibit A L62/L63 of the daily master are only a
    carried copy of these."""
    import io, openpyxl
    sys.path.insert(0, os.path.join(SK, 'custom', '_shared'))
    from google_auth import get_credentials
    from googleapiclient.discovery import build as gbuild
    from googleapiclient.http import MediaIoBaseDownload
    drv = gbuild('drive', 'v3', credentials=get_credentials(), cache_discovery=False)
    res = drv.files().list(q="name contains 'Jeeves Mexico Servicer Report - 2' and trashed = false",
                           orderBy='name desc', pageSize=5, fields='files(id,name,modifiedTime)',
                           supportsAllDrives=True, includeItemsFromAllDrives=True).execute()
    files = [f for f in res.get('files', []) if f['name'].endswith('.xlsx')]
    if not files:
        raise RuntimeError('no servicer report found in Drive')
    cache = os.path.join(DEER, 'backend', '.deer-flow', '_mission_control')
    os.makedirs(cache, exist_ok=True)
    # Newest report whose Exhibit B is populated. The current month's report sits in Drive as an
    # unfilled draft until it is finalised (formula cells have no cached values), so fall back to the
    # prior month instead of crashing. Re-download when Drive's copy is newer than the cache.
    ws = f = None
    for cand in files:
        path = os.path.join(cache, cand['name'])
        drive_mt = dt.datetime.fromisoformat(cand['modifiedTime'].replace('Z', '+00:00')).timestamp()
        if not os.path.exists(path) or os.path.getmtime(path) < drive_mt:
            buf = io.BytesIO()
            dl = MediaIoBaseDownload(buf, drv.files().get_media(fileId=cand['id'], supportsAllDrives=True))
            done = False
            while not done:
                _, done = dl.next_chunk()
            open(path, 'wb').write(buf.getvalue())
        w = openpyxl.load_workbook(path, read_only=True, data_only=True)['Exhibit B']
        if all(w[c].value is not None for c in ('G51', 'H51', 'H52', 'H53', 'H54', 'H57', 'H58', 'H59', 'H60')):
            ws, f = w, cand
            break
        log('MX performance: Exhibit B not populated in', cand['name'], '- trying prior report')
    if ws is None:
        raise RuntimeError('no servicer report with a populated Exhibit B')
    g = lambda c: ws[c].value
    months = [(g('G51'), g('H51'), g('H57')), (None, g('H52'), g('H58')), (None, g('H53'), g('H59'))]
    m0 = g('G51')
    m0 = m0.date() if hasattr(m0, 'date') else m0
    series = []
    for k, (_, npy, pr) in enumerate(months):
        me = (m0.replace(day=1) - dt.timedelta(days=1)) if k == 1 else m0
        if k == 2:
            me = ((m0.replace(day=1) - dt.timedelta(days=1)).replace(day=1) - dt.timedelta(days=1))
        series.append(dict(month_end=me, npy=float(npy), pr=float(pr)))
    perf = dict(file=f['name'], npy_3m=float(g('H54')), pr_3m=float(g('H60')), series=series,
                npy_t=float(g('H51')), npy_t1=float(g('H52')), pr_t=float(g('H57')), pr_t1=float(g('H58')))
    log('MX performance', f['name'], 'PR 3m', round(perf['pr_3m'], 4), 'NPY 3m', round(perf['npy_3m'], 4))
    return perf


def apply_mx_performance(tests, perf, receivable_bb_usd):
    src = 'Servicer Report Exhibit B ({}) H54/H60'.format(perf['file'])
    nxt = perf['series'][0]['month_end'] + dt.timedelta(days=1)
    nxt_label = nxt.strftime('%B')
    for t in tests:
        nm = t['test']
        if 'Payment Rate 3-mo' in nm:
            t['actual'] = perf['pr_3m']
        elif 'Net Portfolio Yield 3-mo' in nm:
            t['actual'] = perf['npy_3m']
        else:
            continue
        t['headroom'] = t['actual'] - t['threshold']
        t['source'] = src
    # what next month's single-month figure must be, once the oldest month rolls off
    for thr, label, lost in ((0.0, 'advance rate holds at 90% (NPY 3-mo >= 0%)', receivable_bb_usd / 0.9 * 0.10),
                             (-0.05, 'no Event of Default trigger (NPY 3-mo >= -5%)', None)):
        need = 3 * thr - perf['npy_t'] - perf['npy_t1']
        tests.append(dict(test='{} NPY needed so {}'.format(nxt_label, label), category='forward', actual=need,
                          threshold=None, direction='info', unit='pct', headroom=None, headroom_usd=None,
                          availability_lost_if_breached_usd=lost,
                          consequence=('Miss -> advance rate 90% -> 80% (Exhibit A L65), about ${:,.0f} of borrowing base'.format(lost)
                                       if lost else 'Miss -> Event of Default Trigger Event; Agent may end the Draw Period'),
                          source='Computed from Exhibit B H51/H52: need = 3 x threshold - (latest two months)'))
    return tests


def refresh_forecast(conn, run_model=False, weeks=26):
    """Receivable balance forecast by country x product (cfo-org-kb receivable_balance_weekly_run.py,
    pooled XGBoost) -> analytics_sandbox.cm_mc_receivable_forecast, which drives the forward view.
    run_model=True re-runs the model to a 26-week horizon (about 16 min; Mondays only)."""
    import glob, subprocess
    import pandas as pd
    outdir = os.environ.get('OUTPUTS_PATH') or os.path.join(DEER, 'backend', '.deer-flow', '_mission_control')
    os.makedirs(outdir, exist_ok=True)
    if run_model:
        through = (dt.date.today() + dt.timedelta(weeks=weeks)).isoformat()
        script = os.path.join(SK, 'custom', 'cfo-org-kb', 'scripts', 'receivable_balance_weekly_run.py')
        env = dict(os.environ, OUTPUTS_PATH=outdir)
        r = subprocess.run([sys.executable, script, '--through', through, '--no-upload', '--no-slack', '--no-ledger', '--no-collections'],
                           capture_output=True, text=True, timeout=3600, env=env)
        log('forecast run rc', r.returncode, r.stdout[-200:])
    files = sorted(glob.glob(os.path.join(outdir, 'Model - Receivable Balance Forecast - *.xlsx')), key=os.path.getmtime)
    if not files:
        log('no forecast workbook in', outdir); return
    f = files[-1]
    w = pd.read_excel(f, sheet_name='Forecast daily')
    if w['Date'].max() < pd.Timestamp(dt.date.today() + dt.timedelta(weeks=12)):
        log('latest forecast workbook is short-horizon - forward view keeps the prior path'); return
    hm = pd.read_excel(f, sheet_name='Hold-out metrics')
    tot = hm[hm['series'].astype(str).str.upper().str.startswith('TOTAL')]
    wape = float(tot['wape_model'].iloc[0]) if len(tot) else None
    nb_cols = [c for c in w.columns if str(c).split(' | ')[0] in ('US', 'CO', 'BR')]
    run_as_of = re.search(r'(\d{8})', os.path.basename(f)).group(1)
    rows = [(dt.datetime.strptime(run_as_of, '%Y%m%d').date(), r.Date.date(), float(r[nb_cols].sum()),
             float(r['MX | Card'] + r['MX | Jeeves Pay']), float(r['TOTAL']), float(r['TOTAL P10']), float(r['TOTAL P90']), wape)
            for _, r in w.iterrows()]
    cur = conn.cursor()
    cur.execute('delete from {}.cm_mc_receivable_forecast'.format(SCHEMA))
    cur.executemany('insert into {}.cm_mc_receivable_forecast (run_as_of, dt, nb_pool, mx, total, total_p10, total_p90, holdout_wape, loaded_at) '
                    'values (%s,%s,%s,%s,%s,%s,%s,%s,getdate())'.format(SCHEMA), rows)
    conn.commit()
    log('forecast loaded', os.path.basename(f), len(rows), 'days, hold-out WAPE', wape)


def _alert(msg):
    import subprocess
    tool = os.path.join(SK, 'custom', 'slack-search', 'slack_tool.py')
    try:
        subprocess.run([sys.executable, tool, 'send', 'U05B5HGNCN9', msg], capture_output=True, text=True, timeout=60)
    except Exception:
        pass


if __name__ == '__main__':
    try:
        main()
    except SystemExit as e:
        if e.code not in (0, None):
            _alert('Mission Control snapshot did not run: {}. Dashboard 11645 is showing the prior day.'.format(e.code))
        raise
    except Exception:
        tb = traceback.format_exc()
        log(tb)
        _alert('Mission Control snapshot failed - dashboard 11645 is showing the prior day. Error: ' + tb.strip().splitlines()[-1][:300])
        sys.exit(1)
