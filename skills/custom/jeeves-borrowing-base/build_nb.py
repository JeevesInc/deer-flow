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


def nearest_cico_filler(as_of, primary_msg_id, window_days=45, max_emails=8):
    """Brian, 2026-09-23: when an account is missing from the as-of CICO email,
    "use the closest date and just fill from that". Returns a callable that,
    given {row: label} still unmatched, pulls CICO emails in order of distance
    from as_of (ties -> the earlier date) and returns {row: (key, val, source)}.
    """
    def _fill(unmatched_labels):
        import cico_fetch_balances as cico
        service = cico._get_gmail_service()
        target = dt.date.fromisoformat(str(as_of))
        after = (target - dt.timedelta(days=window_days + 2)).strftime('%Y/%m/%d')
        before = (target + dt.timedelta(days=window_days + 2)).strftime('%Y/%m/%d')
        res = service.users().messages().list(
            userId='me', maxResults=100,
            q='subject:"CICO- Daily balances" after:{} before:{}'.format(after, before)).execute()
        cands = []
        for m in res.get('messages', []):
            if m['id'] == primary_msg_id:
                continue
            md = service.users().messages().get(userId='me', id=m['id'], format='metadata',
                                                metadataHeaders=['Subject']).execute()
            subj = {h['name']: h['value'] for h in md['payload'].get('headers', [])}.get('Subject', '')
            mm = re.match(r'\s*(\d{2})(\d{2})(\d{2})\s*CICO', subj)
            if not mm:
                continue
            d = dt.date(2000 + int(mm.group(1)), int(mm.group(2)), int(mm.group(3)))
            if abs((d - target).days) <= window_days:
                cands.append((abs((d - target).days), d, m['id'], subj))
        cands.sort()
        # Only spend vision calls on emails whose text actually mentions the
        # missing account (e.g. 'Privy' first appears in the 260911 email, 11
        # days after the 8/31 month-end). Falls back to all candidates.
        words = set()
        for lab in unmatched_labels.values():
            m0 = re.match(r'\s*([A-Za-z]{3,})', str(lab or ''))
            if m0:
                words.add(m0.group(1))
        if words:
            q2 = 'subject:"CICO- Daily balances" after:{} before:{} ({})'.format(
                after, before, ' OR '.join(sorted(words)))
            hit = {m['id'] for m in service.users().messages().list(
                userId='me', maxResults=100, q=q2).execute().get('messages', [])}
            narrowed = [c for c in cands if c[2] in hit]
            if narrowed:
                cands = narrowed
        out, left = {}, dict(unmatched_labels)
        for _, d, mid, subj in cands[:max_emails]:
            if not left:
                break
            try:
                bal, _, _ = fetch_cico(mid)
            except Exception as e:
                print('  CICO fill: skip {!r} ({})'.format(subj, e), file=sys.stderr)
                continue
            got, _miss = match_cico_to_rows(left, bal)
            for row, (key, val) in got.items():
                out[row] = (key, val, '{} ({})'.format(subj.strip(), d.isoformat()))
                left.pop(row, None)
        return out
    return _fill


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
def build(tape, template, out_path, as_of, cico=None, cico_meta=None, fx=None,
          cico_fill=None):
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
        filled = {}
        if unmatched and cico_fill:
            filled = cico_fill({row: label for row, label, _ in unmatched})
            unmatched = [u for u in unmatched if u[0] not in filled]
            from openpyxl.comments import Comment
            for row, (key, val, src) in filled.items():
                ba.cell(row=row, column=17).value = val
                ba.cell(row=row, column=17).comment = Comment(
                    'Not in the as-of CICO email; filled from closest-date email: ' + src,
                    'DeerFlow')
                print('  CICO FILLED row {}: {!r} = {} from {}'.format(
                    row, ba.cell(row=row, column=16).value, val, src))
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
    dq['C25'] = '=SUMIFS(tape!$R:$R,tape!$W:$W,1,tape!$K:$K,">30")'
    dq['C25'].number_format = '#,##0.00'
    dq['B26'] = 'Unencumbered Receivables — total at ' + lbl
    dq['C26'] = '=SUMIFS(tape!$R:$R,tape!$W:$W,1)'
    dq['C26'].number_format = '#,##0.00'
    dq['B27'] = 'Spot DQ30 at ' + lbl
    print('dq30 spot memo @ {}: {:,.2f} / {:,.2f} = {:.4%}'.format(
        lbl, unenc_dq30, unenc_total, unenc_dq30 / unenc_total if unenc_total else 0))

    refresh_dq30_backup(wb, as_of)

    wb.save(out_path)
    print('saved:', out_path)
    return {'tape_rows': len(tape), 'obligor_groups': len(groups),
            'eligible_balance': sum(bal.values()),
            'unenc_total': unenc_total, 'unenc_dq30': unenc_dq30}


DQ30_HDR = ['month_end', 'company_id', 'loan_id', 'name', 'country', 'currency_code',
            'days_past_due', 'balance_usd', 'transfer_flag', 'jvs_remaining_local',
            'sofom_financed_usd', 'unencumbered', 'dq30_numerator_usd', 'dpd_bucket']


def month_end_window(as_of, n=3):
    """The n most recently ENDED calendar month ends as of `as_of` (oldest first)."""
    d = _d(as_of)
    first = dt.date(d.year, d.month, 1)
    ends = []
    for _ in range(n):
        end = first - dt.timedelta(days=1)
        ends.append(end)
        first = dt.date(end.year, end.month, 1)
    return list(reversed(ends))


def pull_dq30_detail(month_ends):
    """Loan-level month-end backup behind the Section 7.01(b) DQ30 Rate."""
    from redshift import connect
    sql_path = os.path.join(SK, 'custom', 'cfo-org-kb', 'sql',
                            'nb_dq30_monthend_detail.sql')
    lits = ', '.join("date '{}'".format(d.isoformat()) for d in month_ends)
    sql = open(sql_path, encoding='utf-8').read().replace('{}', lits)
    cur = connect().cursor()
    cur.execute(sql)
    cols = [c[0] for c in cur.description]
    return [dict(zip(cols, r)) for r in cur.fetchall()]


def refresh_dq30_backup(wb, as_of, force=False):
    """(Re)build the dq30_detail backup tab and wire the dq30 tab to it.

    The covenant is a 3-month rolling average measured at month ends, so the
    backup only needs re-pulling when the 3-month window rolls (i.e. once a
    month).  Everything on the dq30 tab is a live SUMIFS over this tab -- no
    hard-coded numerator/denominator inputs  [SKILL rule 6: no frozen refs].
    """
    import openpyxl
    from openpyxl.styles import Alignment, Font
    from openpyxl.utils import get_column_letter

    MONEY, PCT, DATEF, INT = '#,##0.00', '0.00%', 'm/d/yyyy', '#,##0'
    dq = wb['dq30']
    window = month_end_window(as_of)
    have = [dq['G{}'.format(r)].value for r in (7, 8, 9)]
    have = [v.date() if hasattr(v, 'date') else v for v in have]
    if (not force and have == window and 'dq30_detail' in wb.sheetnames
            and wb['dq30_detail'].max_row > 1):
        print('dq30 backup: window {} unchanged, {} rows retained'.format(
            [d.isoformat() for d in window], wb['dq30_detail'].max_row - 1))
        return None

    rows = pull_dq30_detail(window)
    if 'dq30_detail' in wb.sheetnames:
        del wb['dq30_detail']
    ws = wb.create_sheet('dq30_detail', wb.sheetnames.index('dq30') + 1)
    for i, h in enumerate(DQ30_HDR, start=1):
        ws.cell(row=1, column=i, value=h).font = Font(bold=True)
    for i, r in enumerate(rows):
        rr = 2 + i
        cc = _i(r['country_code'])
        ws.cell(row=rr, column=1, value=_d(r['dt']).date()).number_format = DATEF
        ws.cell(row=rr, column=2, value=_i(r['company_id']))
        ws.cell(row=rr, column=3, value=_i(r['loan_id']))
        ws.cell(row=rr, column=4, value=r['name'] or None)
        ws.cell(row=rr, column=5, value=COUNTRY_NAMES.get(cc, str(cc)))
        ws.cell(row=rr, column=6, value=_i(r['currency']))
        ws.cell(row=rr, column=7, value=_i(r['days_past_due']))
        ws.cell(row=rr, column=8, value=_f(r['balance_usd'])).number_format = MONEY
        ws.cell(row=rr, column=9, value=r['transfer_flag'])
        ws.cell(row=rr, column=10, value=_f(r['jvs_remaining'])).number_format = MONEY
        ws.cell(row=rr, column=11,
                value=_f(r['financed_balance_usd'])).number_format = MONEY
        ws.cell(row=rr, column=12, value='=IF(AND($I{0}="on",N($K{0})>0),0,1)'.format(rr))
        ws.cell(row=rr, column=13,
                value='=IF(AND($L{0}=1,N($G{0})>30),$H{0},0)'.format(rr)
                ).number_format = MONEY
        ws.cell(row=rr, column=14,
                value=('=IF(N($G{0})<=0,"Current",IF($G{0}<=30,"1-30",IF($G{0}<=60,'
                       '"31-60",IF($G{0}<=90,"61-90",">90"))))').format(rr))
    ws.freeze_panes = 'A2'
    ws.auto_filter.ref = 'A1:N{}'.format(len(rows) + 1)
    for col, w in zip('ABCDEFGHIJKLMN',
                      [11, 11, 10, 30, 14, 13, 15, 15, 12, 17, 17, 13, 18, 11]):
        ws.column_dimensions[col].width = w

    # ── wire the covenant rows to the backup ──
    tags = ['Month -2', 'Month -1', 'Current month-end']
    dq['G6'] = 'Month-end (key)'
    dq['G6'].font = Font(bold=True)
    for i, d in enumerate(window):
        r = 7 + i
        dq['B{}'.format(r)] = '{} - {}'.format(tags[i], d.strftime('%B %d, %Y'))
        dq['G{}'.format(r)] = d
        dq['G{}'.format(r)].number_format = DATEF
        dq['C{}'.format(r)] = ('=SUMIFS(dq30_detail!$H:$H,dq30_detail!$A:$A,$G{0},'
                               'dq30_detail!$L:$L,1,dq30_detail!$G:$G,">30")').format(r)
        dq['C{}'.format(r)].number_format = MONEY
        dq['D{}'.format(r)] = ('=SUMIFS(dq30_detail!$H:$H,dq30_detail!$A:$A,$G{0},'
                               'dq30_detail!$L:$L,1)').format(r)
        dq['D{}'.format(r)].number_format = MONEY
        dq['F{}'.format(r)] = ('Live SUMIFS over dq30_detail (capital_markets_dm.loc_tape '
                               '@ month-end; unencumbered = not MX SOFOM-financed, '
                               'ex charge-offs and repayment plans)')
    dq['B22'] = ('Monthly figures are live SUMIFS over the dq30_detail backup tab; the '
                 'covenant is measured as of the last day of each calendar month.')

    # ── backup recon: unencumbered pool by DPD bucket ──
    dq['B30'] = 'Backup - Unencumbered pool by DPD bucket (live SUMIFS over dq30_detail)'
    dq['B30'].font = Font(bold=True)
    dq['B31'] = 'DPD bucket'
    dq['B31'].font = Font(bold=True)
    for i in range(3):
        col = get_column_letter(3 + i)
        c = dq['{}31'.format(col)]
        c.value = '=$G${}'.format(7 + i)
        c.number_format = DATEF
        c.font = Font(bold=True)
    buckets = [('Current (0 or less DPD)', '"<=0"', None),
               ('1 - 30 DPD', '">=1"', '"<=30"'),
               ('31 - 60 DPD', '">=31"', '"<=60"'),
               ('61 - 90 DPD', '">=61"', '"<=90"'),
               ('Over 90 DPD', '">90"', None)]
    for j, (lbl, c1, c2) in enumerate(buckets):
        rr = 32 + j
        dq['B{}'.format(rr)] = lbl
        for i in range(3):
            col = get_column_letter(3 + i)
            crit = 'dq30_detail!$G:$G,{}'.format(c1)
            if c2:
                crit += ',dq30_detail!$G:$G,{}'.format(c2)
            cell = dq['{}{}'.format(col, rr)]
            cell.value = ('=SUMIFS(dq30_detail!$H:$H,dq30_detail!$A:$A,{c}$31,'
                          'dq30_detail!$L:$L,1,{cr})').format(c=col, cr=crit)
            cell.number_format = MONEY
    for rr, lbl in [(37, 'Total Unencumbered Receivables (b)'),
                    (38, 'Unencumbered Receivables more than 30 DPD (a)'),
                    (39, 'Monthly DQ30 (a)/(b)'),
                    (40, 'Check vs rows 7-9 (must be 0.00%)'),
                    (41, 'Loan records in the month-end pool')]:
        dq['B{}'.format(rr)] = lbl
        if rr in (37, 38, 39):
            dq['B{}'.format(rr)].font = Font(bold=True)
    for i in range(3):
        col = get_column_letter(3 + i)
        dq['{}37'.format(col)] = ('=SUMIFS(dq30_detail!$H:$H,dq30_detail!$A:$A,{c}$31,'
                                  'dq30_detail!$L:$L,1)').format(c=col)
        dq['{}37'.format(col)].number_format = MONEY
        dq['{}38'.format(col)] = '=SUM({c}34:{c}36)'.format(c=col)
        dq['{}38'.format(col)].number_format = MONEY
        dq['{}39'.format(col)] = '=IF({c}37=0,"",{c}38/{c}37)'.format(c=col)
        dq['{}39'.format(col)].number_format = PCT
        dq['{}40'.format(col)] = '={c}39-E{r}'.format(c=col, r=7 + i)
        dq['{}40'.format(col)].number_format = PCT
        dq['{}41'.format(col)] = ('=SUMPRODUCT((dq30_detail!$A$2:$A${n}={c}$31)*'
                                  '(dq30_detail!$L$2:$L${n}=1))').format(
                                      n=len(rows) + 1, c=col)
        dq['{}41'.format(col)].number_format = INT
    dq['B43'] = ('Backup: dq30_detail carries every non-charged-off, non-repayment-plan '
                 'loan record on capital_markets_dm.loc_tape at each of the three month '
                 'ends ({} records). Encumbrance (col L) and the >30 DPD numerator '
                 '(col M) are in-sheet formulas off the raw fields, so rows 7-9 tie to '
                 'the detail with no hard-coded inputs.'.format(len(rows)))
    dq['B43'].alignment = Alignment(wrap_text=True, vertical='top')

    # replicate the sheet math for the console (no Excel on this box)
    agg = {}
    for r in rows:
        d = _d(r['dt']).date()
        agg.setdefault(d, [0.0, 0.0, 0])
        if _i(r['unencumbered']) == 1:
            bal = _f(r['balance_usd']) or 0.0
            agg[d][1] += bal
            agg[d][2] += 1
            if (_f(r['days_past_due']) or 0) > 30:
                agg[d][0] += bal
    rates = []
    for d in sorted(agg):
        a, b, n = agg[d]
        rate = a / b if b else 0
        rates.append(rate)
        print('dq30 backup {}: {:,.2f} / {:,.2f} = {:.4%} ({:,} unencumbered rows)'
              .format(d.isoformat(), a, b, rate, n))
    avg = sum(rates) / len(rates) if rates else 0
    print('dq30 backup: 3-month rolling average {:.4%} (cap 2.50%, step-up <1.50%)'
          .format(avg))
    return {'window': [d.isoformat() for d in window], 'detail_rows': len(rows),
            'monthly': rates, 'rolling_avg': avg}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--as-of', required=True)
    ap.add_argument('--template', required=True)
    ap.add_argument('--out-date', default=None)
    ap.add_argument('--tape-csv', default=None, help='reuse an already-pulled tape CSV')
    ap.add_argument('--cico-msg', default=None)
    ap.add_argument('--skip-cico', action='store_true')
    ap.add_argument('--cico-fill-nearest', action='store_true',
                    help='fill accounts missing from the as-of CICO email from the closest-dated one')
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

    cico = meta = filler = None
    if not a.skip_cico:
        cico, subj, date_hdr = fetch_cico(a.cico_msg)
        m = re.match(r'\s*(\d{2})(\d{2})(\d{2})', subj)
        meta = '{}-{}-{}'.format(m.group(3), dt.date(2000, int(m.group(2)), 1).strftime('%b'),
                                 m.group(1)) if m else subj
        print('CICO email: {!r} | {} | {} accounts'.format(subj, date_hdr, len(cico)))
        if a.cico_fill_nearest:
            filler = nearest_cico_filler(a.as_of, a.cico_msg)

    out_date = a.out_date or dt.date.today().strftime('%Y%m%d')
    out = os.path.join(os.environ['OUTPUTS_PATH'],
                       'Jeeves NB Borrowing Base - Form - {}.xlsx'.format(out_date))
    stats = build(tape, a.template, out, a.as_of, cico, meta, fx, cico_fill=filler)
    print('STATS:', stats)


if __name__ == '__main__':
    main()
