#!/usr/bin/env python3
"""Build the Jeeves NB Borrowing Base workbook.

Cadence (Brian, 2026-08-11 / re-confirmed 2026-08-17): runs WEEKLY on MONDAY,
as of the PREVIOUS DAY (Sunday).

Sources
  - Receivables tape: cfo-org-kb/sql/data_tape.sql @ dt = as-of date (Redshift)
  - Bank balances:    most recent CICO daily balances email (vision extraction),
                      matched to the Schedule 5.22 rows on bank_accts by
                      trailing account digits  [SKILL rule 5b]
  - Template:         the most recent prior "Jeeves NB Borrowing Base - Form - *.xlsx"
                      (clone-and-edit; never re-architect  [SKILL hard rule])

Usage
  python build_nb.py --as-of 2026-08-16 --template "<path to prior workbook>"
                     [--out-date 20260817] [--skip-cico]
"""
import argparse
import collections
import csv
import datetime as dt
import os
import re
import sys

SK = os.environ['SKILLS_PATH']
sys.path.insert(0, os.path.join(SK, 'custom', '_shared'))
sys.path.insert(0, os.path.join(SK, 'custom', 'jeeves-borrowing-base'))
sys.path.insert(0, os.path.join(SK, 'custom', 'gmail'))

ELIG_COUNTRIES = (170, 76, 840)          # Colombia, Brazil, United States
TAPE_DATA_COLS = [
    'dt', 'company_id', 'loan_id', 'name', 'ein', 'country_code', 'currency', 'product',
    'onboarding_date', 'status', 'days_past_due', 'max_dpd', 'charge_off_flag',
    'charge_off_dt', 'is_in_repayment', 'transfer_flag', 'sofom_balance_usd',
    'balance_usd', 'total_collateral_amount_usd', 'country',
]
COUNTRY_NAMES = {
    76: 'Brazil', 484: 'Mexico', 170: 'Colombia', 840: 'United States',
    826: 'United Kingdom', 40: 'Austria', 724: 'Spain', 528: 'Netherlands',
    380: 'Italy', 124: 'Canada', 276: 'Germany', 620: 'Portugal', 152: 'Chile',
    604: 'Peru', 372: 'Ireland', 196: 'Cyprus', 250: 'France',
}   # unmapped codes fall back to str(code), matching the prior workbook


# ── helpers ────────────────────────────────────────────────────────────────
def _f(v):
    if v in (None, '', 'None'):
        return None
    return float(v)


def _i(v):
    f = _f(v)
    return None if f is None else int(f)


def _b(v):
    if v in (None, '', 'None'):
        return None
    return str(v).strip().lower() in ('true', '1', 't', 'yes')


def _d(v):
    if v in (None, '', 'None'):
        return None
    s = str(v)[:19]
    for fmt in ('%Y-%m-%d %H:%M:%S', '%Y-%m-%d'):
        try:
            return dt.datetime.strptime(s, fmt)
        except ValueError:
            pass
    return None


def row_flags(r):
    """NB eligibility, mirroring the workbook's tape-tab formulas (cols U-AD)."""
    notco = 0 if _b(r['charge_off_flag']) else 1
    unenc = 0 if (str(r['transfer_flag'] or '').lower() == 'on'
                  or (_f(r['sofom_balance_usd']) or 0) > 0) else 1
    iv = 1 if (_f(r['days_past_due']) or 0) <= 30 else 0
    v = 1 if _i(r['country_code']) in ELIG_COUNTRIES else 0
    vi = 0 if _b(r['is_in_repayment']) else 1
    pos = 1 if (_f(r['balance_usd']) or 0) > 0 else 0
    return notco, unenc, iv, v, vi, pos


def is_eligible(r):
    return all(row_flags(r))


def group_key(r):
    ein = r.get('ein')
    return str(ein) if ein not in (None, '', 'None') else '(no EIN) {}'.format(r['company_id'])


# ── tape pull ──────────────────────────────────────────────────────────────
def pull_tape(as_of):
    from redshift import connect
    sql_path = os.path.join(SK, 'custom', 'cfo-org-kb', 'sql', 'data_tape.sql')
    sql = open(sql_path, encoding='utf-8').read().replace('{}', as_of)
    conn = connect()
    cur = conn.cursor()
    cur.execute(sql)
    cols = [d[0] for d in cur.description]
    rows = [dict(zip(cols, r)) for r in cur.fetchall()]
    cur.close()
    conn.close()
    if not rows:
        raise RuntimeError('No tape rows returned for dt={} — refusing to build'.format(as_of))
    for r in rows:
        for k, v in list(r.items()):
            r[k] = None if v is None else (v if isinstance(v, (int, float, bool, dt.datetime,
                                                              dt.date)) else str(v))
    return rows


def load_tape_csv(path):
    return list(csv.DictReader(open(path, encoding='utf-8')))


# ── CICO ───────────────────────────────────────────────────────────────────
def fetch_cico(msg_id=None):
    """Return (balances_dict, subject, date_header) from the latest CICO email."""
    import tempfile
    import cico_fetch_balances as cico
    service = cico._get_gmail_service()
    if msg_id is None:
        res = service.users().messages().list(
            userId='me', q='subject:"CICO- Daily balances" newer_than:14d',
            maxResults=5).execute()
        msgs = res.get('messages', [])
        if not msgs:
            raise RuntimeError('No CICO daily balances email in the last 14 days')
        msg_id = msgs[0]['id']
    msg = service.users().messages().get(userId='me', id=msg_id, format='full').execute()
    hdrs = {h['name']: h['value'] for h in msg['payload'].get('headers', [])}
    with tempfile.TemporaryDirectory() as tmp:
        paths = cico._download_images(service, msg_id, msg['payload'], tmp)
        if not paths:
            raise RuntimeError('CICO email {} has no image attachments'.format(msg_id))
        balances = cico._call_claude_vision(paths)
    balances = {k: v for k, v in balances.items() if not k.startswith('__')}
    if len(balances) < 12:
        raise RuntimeError('CICO extraction gate: only {} rows extracted'.format(len(balances)))
    return balances, hdrs.get('Subject', ''), hdrs.get('Date', '')


def _norm(s):
    return re.sub(r'[^0-9a-zA-Z]', '', str(s or '')).lower()


def match_cico_to_rows(labels, balances):
    """labels: {row: col-P label}. Match by trailing account digits / token.

    Returns (matched {row: (key, balance)}, unmatched [(row, label, reason)]).
    """
    keys = sorted(balances, key=lambda k: -len(_norm(k)))
    matched, unmatched = {}, []
    for row, label in labels.items():
        nlabel = _norm(label)
        hits = [k for k in keys if _norm(k) and _norm(k) in nlabel]
        if not hits:
            unmatched.append((row, label, 'no CICO key found in label'))
            continue
        best = [k for k in hits if len(_norm(k)) == len(_norm(hits[0]))]
        if len(best) > 1:
            unmatched.append((row, label, 'ambiguous: ' + ', '.join(best)))
            continue
        matched[row] = (hits[0], balances[hits[0]])
    return matched, unmatched


# ── workbook ───────────────────────────────────────────────────────────────
def build(tape, template, out_path, as_of, cico=None, cico_meta=None, fx=None):
    import openpyxl
    from openpyxl.cell.cell import MergedCell
    from openpyxl.formula.translate import Translator

    wb = openpyxl.load_workbook(template)
    t = wb['tape']
    hdr = {c.value: c.column for c in t[1]}
    for name in TAPE_DATA_COLS:
        if name not in hdr:
            raise RuntimeError('template tape tab is missing column ' + name)

    # formula templates from the template's first data row (cols U..AE)
    formula_cols = [c for c in range(hdr['country'] + 1, t.max_column + 1)]
    templates = {}
    for c in formula_cols:
        cell = t.cell(row=2, column=c)
        templates[c] = (cell.value, cell.coordinate)

    old_max = t.max_row
    for r in range(2, old_max + 1):                     # clear
        for c in range(1, t.max_column + 1):
            cell = t.cell(row=r, column=c)
            if not isinstance(cell, MergedCell):
                cell.value = None

    conv = {
        'dt': _d, 'onboarding_date': _d, 'charge_off_dt': _d,
        'company_id': lambda v: v if isinstance(v, str) else _i(v),
        'loan_id': _i, 'country_code': _i, 'currency': _i,
        'days_past_due': _i, 'max_dpd': _i,
        'charge_off_flag': _b, 'is_in_repayment': _b,
        'sofom_balance_usd': _f, 'balance_usd': _f, 'total_collateral_amount_usd': _f,
    }
    for i, r in enumerate(tape):
        row = 2 + i
        cc = _i(r['country_code'])
        r = dict(r)
        r['country'] = COUNTRY_NAMES.get(cc, str(cc))
        for name in TAPE_DATA_COLS:
            val = r.get(name)
            fn = conv.get(name)
            t.cell(row=row, column=hdr[name]).value = fn(val) if fn else val
        for c, (ftxt, origin) in templates.items():
            if isinstance(ftxt, str) and ftxt.startswith('='):
                t.cell(row=row, column=c).value = Translator(
                    ftxt, origin=origin).translate_formula(
                        t.cell(row=row, column=c).coordinate)
            else:
                t.cell(row=row, column=c).value = ftxt
    print('tape: wrote {} rows (was {})'.format(len(tape), old_max - 1))

    # ── obligors ──
    o = wb['obligors']
    o_templates = {}
    for c in range(7, o.max_column + 1):
        cell = o.cell(row=2, column=c)
        o_templates[c] = (cell.value, cell.coordinate)

    bal = collections.defaultdict(float)
    coll = collections.defaultdict(dict)
    meta = {}
    for r in tape:
        if not is_eligible(r):
            continue
        k = group_key(r)
        bal[k] += _f(r['balance_usd']) or 0
        coll[k][r['company_id']] = _f(r['total_collateral_amount_usd']) or 0
        meta.setdefault(k, (r.get('name'), COUNTRY_NAMES.get(_i(r['country_code']),
                                                             str(_i(r['country_code'])))))
    groups = []
    for k, b in bal.items():
        if b <= 0:
            continue
        c = sum(coll[k].values())
        groups.append((k, meta[k][0], meta[k][1], b, c, b - c))
    groups.sort(key=lambda g: -g[5])

    o_old = o.max_row
    for r in range(2, o_old + 1):
        for c in range(1, o.max_column + 1):
            cell = o.cell(row=r, column=c)
            if not isinstance(cell, MergedCell):
                cell.value = None
    for i, (k, nm, ctry, b, c, net) in enumerate(groups):
        row = 2 + i
        o.cell(row=row, column=1).value = i + 1
        o.cell(row=row, column=2).value = k
        o.cell(row=row, column=3).value = nm
        o.cell(row=row, column=4).value = ctry
        o.cell(row=row, column=5).value = b
        o.cell(row=row, column=6).value = c
        for cc_, (ftxt, origin) in o_templates.items():
            if isinstance(ftxt, str) and ftxt.startswith('='):
                o.cell(row=row, column=cc_).value = Translator(
                    ftxt, origin=origin).translate_formula(
                        o.cell(row=row, column=cc_).coordinate)
    print('obligors: wrote {} eligible obligor groups (was {})'.format(len(groups), o_old - 1))

    # ── bank_accts ──
    ba = wb['bank_accts']
    if cico:
        labels = {r: ba.cell(row=r, column=16).value for r in range(2, ba.max_row + 1)
                  if ba.cell(row=r, column=1).value not in (None, '')}
        matched, unmatched = match_cico_to_rows(labels, cico)
        # Accounts the CICO report itself documents as not in use are legitimately
        # absent from the email; keep the prior workbook value rather than failing.
        kept = []
        for item in list(unmatched):
            row, label, reason = item
            if 'not in use' in str(label).lower():
                prior = ba.cell(row=row, column=17).value
                kept.append((row, label, prior))
                unmatched.remove(item)
        for row, label, prior in kept:
            print('  CICO: row {} {!r} not in the email (documented not in use) — '
                  'retained prior value {}'.format(row, label, prior))
        if unmatched:
            for row, label, reason in unmatched:
                print('  CICO UNMATCHED row {}: {!r} — {}'.format(row, label, reason),
                      file=sys.stderr)
            raise RuntimeError('{} bank_accts row(s) unmatched — refusing to zero-fill'
                               .format(len(unmatched)))
        for row, (key, val) in matched.items():
            ba.cell(row=row, column=17).value = val
        if cico_meta:
            stamp = cico_meta
            ba.cell(row=1, column=16).value = 'CICO acct ({} daily balances email)'.format(stamp)
        print('bank_accts: updated {} balances from CICO'.format(len(matched)))
        if fx:
            n = 0
            for row in labels:
                ccy = ba.cell(row=row, column=3).value
                if ccy in fx:
                    ba.cell(row=row, column=18).value = round(fx[ccy], 6)
                    n += 1
            print('bank_accts: refreshed {} FX rates from tape spot_rate'.format(n))

    # ── dq30 spot memo ──
    dq = wb['dq30']
    unenc_total = sum((_f(r['balance_usd']) or 0) for r in tape
                      if row_flags(r)[1] == 1)
    unenc_dq30 = sum((_f(r['balance_usd']) or 0) for r in tape
                     if row_flags(r)[1] == 1 and (_f(r['days_past_due']) or 0) > 30)
    d = _d(as_of)
    lbl = '{}/{}/{}'.format(d.month, d.day, d.year)
    dq['B25'] = 'Unencumbered Receivables >30 DPD at ' + lbl
    dq['C25'] = unenc_dq30
    dq['B26'] = 'Unencumbered Receivables — total at ' + lbl
    dq['C26'] = unenc_total
    dq['B27'] = 'Spot DQ30 at ' + lbl
    print('dq30 spot memo @ {}: {:,.2f} / {:,.2f} = {:.4%}'.format(
        lbl, unenc_dq30, unenc_total, unenc_dq30 / unenc_total if unenc_total else 0))

    wb.save(out_path)
    print('saved:', out_path)
    return {'tape_rows': len(tape), 'obligor_groups': len(groups),
            'eligible_balance': sum(bal.values()),
            'unenc_total': unenc_total, 'unenc_dq30': unenc_dq30}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--as-of', required=True)
    ap.add_argument('--template', required=True)
    ap.add_argument('--out-date', default=None)
    ap.add_argument('--tape-csv', default=None, help='reuse an already-pulled tape CSV')
    ap.add_argument('--cico-msg', default=None)
    ap.add_argument('--skip-cico', action='store_true')
    a = ap.parse_args()

    tape = load_tape_csv(a.tape_csv) if a.tape_csv else pull_tape(a.as_of)
    print('tape rows for {}: {}'.format(a.as_of, len(tape)))

    fx = {}
    ccy_map = {986: 'BRL', 170: 'COP', 484: 'MXN', 840: 'USD', 826: 'GBP',
               978: 'EUR', 124: 'CAD'}
    rates = collections.defaultdict(list)
    for r in tape:
        sr, c = _f(r.get('spot_rate')), _i(r.get('currency'))
        if sr and c in ccy_map:
            rates[ccy_map[c]].append(sr)
    for k, v in rates.items():
        v.sort()
        fx[k] = v[len(v) // 2]
    print('FX (USD per 1 local, tape spot_rate median):',
          {k: round(v, 6) for k, v in sorted(fx.items())})

    cico = meta = None
    if not a.skip_cico:
        cico, subj, date_hdr = fetch_cico(a.cico_msg)
        m = re.match(r'\s*(\d{2})(\d{2})(\d{2})', subj)
        meta = '{}-{}-{}'.format(m.group(3), dt.date(2000, int(m.group(2)), 1).strftime('%b'),
                                 m.group(1)) if m else subj
        print('CICO email: {!r} | {} | {} accounts'.format(subj, date_hdr, len(cico)))

    out_date = a.out_date or dt.date.today().strftime('%Y%m%d')
    out = os.path.join(os.environ['OUTPUTS_PATH'],
                       'Jeeves NB Borrowing Base - Form - {}.xlsx'.format(out_date))
    stats = build(tape, a.template, out, a.as_of, cico, meta, fx)
    print('STATS:', stats)


if __name__ == '__main__':
    main()
