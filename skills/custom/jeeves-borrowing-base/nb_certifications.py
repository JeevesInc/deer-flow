#!/usr/bin/env python3
"""NB Borrowing Base Certificate (Credit Agreement Exhibit G) -- Section 6.02(b) certifications.

Every BBC (weekly 6.02(b)(x) and month-end 6.02(b)(y)) certifies:
  (i)   7.01 covenant compliance with calculations   -> Schedules 1-3 (from nb_covenant_check)
  (ii)  covenants observed / no Default or EoD       -> officer statement (config)
  (iii) no event of default / rapid amortization event under Material Indebtedness or SPV Financing
        -> officer statement (config)
  Exhibit G item 5 / Exhibit A: Organization Document amendments since last BBC (config)
Month-end only:
  (iv)  accounts opened/closed/ceased Excluded since last BBC -> Exhibit B (bank_accts diff vs baseline)
  (v)   Effective Advance Rate of each SPV Financing          -> Exhibit C (SOFOM master tape + draws)
  (vi)  Immaterial Subsidiary threshold test                 -> item 9 (Accounting inputs)

Fail-closed: anything that cannot be sourced is returned in result['pending'] and rendered
as a visible [PENDING ...] marker. The caller must not create a draft while pending is non-empty.

State (all under backend/.deer-flow/):
  nb_cert_config.json      signer, standing officer statements, SPV repayments, immaterial subs
  nb_cert_inputs.json      per-period inputs keyed by as-of date (Accounting figures, confirmations)
  nb_accounts_snapshots/   bank_accts snapshot per delivered month-end (baseline for next (iv))
  nb_accounts_baseline_schedule_5_22.json   closing-date Schedule 5.22 (first baseline)

CLI:  python nb_certifications.py --workbook <NB form xlsx> --as-of 2026-08-31 --variant month_end
"""
import datetime as dt
import json
import os
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
STATE = Path(os.environ.get('NB_CERT_STATE_DIR') or r'C:\Jeeves\redshift-bot\deer-flow\backend\.deer-flow')
CONFIG = STATE / 'nb_cert_config.json'
INPUTS = STATE / 'nb_cert_inputs.json'
SNAPS = STATE / 'nb_accounts_snapshots'
BASELINE_522 = STATE / 'nb_accounts_baseline_schedule_5_22.json'


def _load(p, default):
    try:
        return json.load(open(p, encoding='utf-8'))
    except FileNotFoundError:
        return default


def _money(x):
    return '${:,.0f}'.format(x)


def _key(acct):
    toks = re.findall(r'[0-9A-Za-z]{3,}', str(acct or ''))
    return toks[-1][-4:].lower() if toks else str(acct).lower()


# -- (iv) account delta -------------------------------------------------------
def workbook_accounts(workbook):
    import openpyxl
    ba = openpyxl.load_workbook(workbook, read_only=True)['bank_accts']
    out = []
    for row in ba.iter_rows(min_row=2, values_only=True):
        if row[0] in (None, ''):
            continue
        # Brian, 2026-09-23: an account is only non-Excluded once we have given control
        # (controlled_acct = Yes). Rows added as disclosure-only default excluded_acct to 'No'
        # without that meaning anything, so col I alone must never drive Exhibit B.
        ctl = str(row[7] or '').strip().lower().startswith('y')
        exc = str(row[8] or '').strip()
        out.append({'country': row[0], 'acct': str(row[1]), 'currency': row[2],
                    'excluded': exc if (ctl and not exc.lower().startswith('y')) else 'Yes',
                    'controlled': 'Yes' if ctl else 'No', 'acct_type': row[6],
                    'loan_party': row[12], 'purpose': row[14]})
    return out


def latest_snapshot_before(as_of):
    if not SNAPS.exists():
        return None, None
    best = None
    for f in SNAPS.glob('*.json'):
        if f.stem < as_of and (best is None or f.stem > best):
            best = f.stem
    return (best, _load(SNAPS / (best + '.json'), None)) if best else (None, None)


def account_delta(workbook, as_of):
    cur = workbook_accounts(workbook)
    base_date, base = latest_snapshot_before(as_of)
    if base is None:
        sched = _load(BASELINE_522, None)
        if sched is None:
            return None, 'no baseline (Schedule 5.22 baseline file missing)'
        base = [{'acct': '{} {}'.format(s['bank'], s['number']), 'key': _key(s['number']),
                 'excluded': s['excluded']} for s in sched]
        base_label = 'Schedule 5.22 (Closing Date, 2026-08-10)'
    else:
        base = [dict(b, key=_key(b['acct'])) for b in base]
        base_label = 'month-end certificate as of ' + base_date
    bmap = {}
    for b in base:
        bmap.setdefault(b['key'], []).append(b)
    opened, ceased_excl, closed = [], [], []
    for c in cur:
        if c['excluded'].lower().startswith('y'):
            continue
        hits = bmap.get(_key(c['acct']), [])
        if not hits:
            opened.append(c)
        elif all(str(h['excluded']).lower().startswith('y') for h in hits):
            ceased_excl.append(c)
    # Closed = gone from the account list entirely. A scheduled non-Excluded account that is
    # still listed but not yet under control (post-closing DACA) is NOT closed.
    ckeys = {_key(c['acct']) for c in cur}
    for b in base:
        if not str(b['excluded']).lower().startswith('y') and b['key'] not in ckeys:
            closed.append(b)
    return {'baseline': base_label, 'opened': opened, 'ceased_excluded': ceased_excl,
            'closed': closed, 'current': cur}, None


def save_snapshot(workbook, as_of):
    SNAPS.mkdir(parents=True, exist_ok=True)
    json.dump(workbook_accounts(workbook), open(SNAPS / (as_of + '.json'), 'w', encoding='utf-8'),
              indent=1, default=str)


# -- (v) Effective Advance Rate ----------------------------------------------
def _drive():
    sys.path.insert(0, str(HERE.parent / '_shared'))
    from google_auth import get_credentials
    from googleapiclient.discovery import build
    return build('drive', 'v3', credentials=get_credentials())


def _fetch_drive_file(name, dest_dir):
    import io
    from googleapiclient.http import MediaIoBaseDownload
    svc = _drive()
    q = "name = '{}' and trashed = false".format(name.replace("'", "\\'"))
    files = svc.files().list(q=q, fields='files(id,name,modifiedTime)', supportsAllDrives=True,
                             includeItemsFromAllDrives=True).execute().get('files', [])
    if not files:
        return None
    fid = sorted(files, key=lambda f: f['modifiedTime'])[-1]['id']
    buf = io.BytesIO()
    dl = MediaIoBaseDownload(buf, svc.files().get_media(fileId=fid, supportsAllDrives=True))
    done = False
    while not done:
        _, done = dl.next_chunk()
    path = os.path.join(dest_dir, name)
    open(path, 'wb').write(buf.getvalue())
    return path


def effective_advance_rates(as_of, cfg, dest_dir):
    """Effective Advance Rate (NB 1.01) = Indebtedness outstanding under the SPV Financing /
    Outstanding Receivables Balance of the SPV's receivables eligible for its borrowing base."""
    import openpyxl
    out, pending = [], []
    target = dt.date.fromisoformat(as_of)
    for spv in cfg.get('spv_financings', []):
        name = spv['sofom_master_pattern'].format(yyyymmdd=as_of.replace('-', ''))
        path = _fetch_drive_file(name, dest_dir)
        if not path:
            pending.append('Effective Advance Rate {}: {} not found on Drive'.format(spv['name'], name))
            continue
        wb = openpyxl.load_workbook(path, read_only=True)
        rows = wb['tape'].iter_rows(values_only=True)
        hdr = next(rows)
        ix = {h: i for i, h in enumerate(hdr)}
        elig, n, bad = 0.0, 0, False
        for r in rows:
            d = r[ix['dt']]
            d = d.date() if hasattr(d, 'date') else d
            if d == target:
                v = r[ix['eligible_balance_usd']]
                if isinstance(v, str):
                    bad = True
                    break
                elig += float(v or 0)
                n += 1
        if bad:
            pending.append('{}: eligible_balance_usd is a formula, not a value'.format(name))
            continue
        draws = 0.0
        for r in wb['historical_draws'].iter_rows(min_row=2, values_only=True):
            d, amt = r[1], r[2]
            if d is not None and amt and (d.date() if hasattr(d, 'date') else d) <= target:
                draws += float(amt)
        repaid = sum(float(p['amount']) for p in spv.get('principal_repayments', [])
                     if p['date'] <= as_of)
        if n == 0 or elig <= 0:
            pending.append('Effective Advance Rate {}: no eligible rows dated {} in {}'.format(
                spv['name'], as_of, name))
            continue
        out.append({'name': spv['name'], 'outstanding': draws - repaid, 'draws': draws,
                    'repaid': repaid, 'eligible': elig, 'rate': (draws - repaid) / elig,
                    'source': name, 'rows': n})
    return out, pending


# -- (vi) Immaterial Subsidiary test ------------------------------------------
LIMITS = {'assets': 1_000_000, 'rev_ttm': 2_500_000, 'pct_t3m': 0.05,
          'agg_assets': 2_000_000, 'agg_rev_ttm': 5_000_000, 'agg_pct_t3m': 0.10}


def immaterial_test(as_of, cfg, inputs):
    subs = cfg.get('immaterial_subsidiaries', [])
    # Brian, 2026-09-23: "We do not need to check with Kacper, just have Alex sign it."
    # Item 9 is certified by the Responsible Officer's signature; no Accounting figures.
    if cfg.get('immaterial_mode', 'officer_attestation') == 'officer_attestation':
        return {'rows': [], 'agg': None, 'breaches': [], 'agg_breach': False, 'exceeds': False,
                'attested': True, 'source': 'Responsible Officer attestation'}, []
    per = inputs.get(as_of, {}).get('immaterial', {})
    have = per.get('subsidiaries', {})
    need = [s for s in subs if s not in have]
    if need or 'consolidated_rev_t3m' not in per:
        miss = list(need) + (['consolidated T3M gross revenue'] if 'consolidated_rev_t3m' not in per else [])
        return None, ['Immaterial Subsidiary test: Accounting figures missing -- ' + '; '.join(miss)]
    cons = float(per['consolidated_rev_t3m'])
    rows, breaches = [], []
    agg = {'assets': 0.0, 'rev_ttm': 0.0, 'rev_t3m': 0.0}
    for s in subs:
        v = have[s]
        a, rt, r3 = float(v['total_assets']), float(v['rev_ttm']), float(v['rev_t3m'])
        pct = r3 / cons if cons else 0.0
        agg['assets'] += a
        agg['rev_ttm'] += rt
        agg['rev_t3m'] += r3
        if a >= LIMITS['assets'] or rt >= LIMITS['rev_ttm'] or pct >= LIMITS['pct_t3m']:
            breaches.append(s)
        rows.append({'name': s, 'assets': a, 'rev_ttm': rt, 'rev_t3m': r3, 'pct': pct})
    agg_pct = agg['rev_t3m'] / cons if cons else 0.0
    agg_breach = (agg['assets'] >= LIMITS['agg_assets'] or agg['rev_ttm'] >= LIMITS['agg_rev_ttm']
                  or agg_pct >= LIMITS['agg_pct_t3m'])
    return {'rows': rows, 'agg': dict(agg, pct=agg_pct), 'breaches': breaches,
            'agg_breach': agg_breach, 'exceeds': bool(breaches or agg_breach),
            'source': per.get('source', '')}, []


# -- render Exhibit G ---------------------------------------------------------
def render(out_path, as_of, variant, cov_res, cfg, inputs, delta=None, ears=None, imm=None,
           pending=()):
    import docx
    from docx.shared import Pt
    per = inputs.get(as_of, {})
    st = dict(cfg.get('standing_statements', {}))
    st.update(per.get('statements', {}))
    m = cov_res['metrics']
    tests = {t['name']: t for t in cov_res['tests']}
    d = docx.Document()
    d.styles['Normal'].font.name = 'Times New Roman'
    d.styles['Normal'].font.size = Pt(11)

    def p(t='', b=False):
        d.add_paragraph().add_run(t).bold = b

    date_long = dt.date.fromisoformat(as_of).strftime('%B %d, %Y').replace(' 0', ' ')
    p('BORROWING BASE CERTIFICATE', True)
    p('Certification Period End Date: ' + date_long)
    p('To:\tWilmington Trust, National Association, as Administrative Agent, and the Lenders party '
      'to the Credit Agreement (as defined below)')
    p('Re:\tCredit Agreement, dated as of August 10, 2026 (as amended, restated, amended and restated, '
      'supplemented or otherwise modified from time to time and in effect on the date hereof, the '
      '\u201cCredit Agreement\u201d), among Jeeves Inc., a Delaware corporation, as borrower (the '
      '\u201cBorrower\u201d), the Lenders (as defined therein) from time to time party thereto, '
      'Wilmington Trust, National Association (\u201cWilmington Trust\u201d), as administrative agent '
      'for the Lenders (in such capacity, together with its successors and assigns, the '
      '\u201cAdministrative Agent\u201d), and Wilmington Trust, as collateral agent for the Secured '
      'Parties (in such capacity, together with its successors and assigns, the \u201cCollateral '
      'Agent\u201d). Capitalized terms used but not otherwise defined herein have the meanings '
      'provided in the Credit Agreement.')
    p('Ladies and Gentlemen:')
    s = cfg['signer']
    p('The undersigned Responsible Officer hereby certifies as of the date hereof that {pr} is the {t} '
      'of the Borrower and that, in {pos} capacity as such, {pr} is authorized to execute and deliver '
      'this Borrowing Base Certificate to the Administrative Agent and the Lenders on behalf of the '
      'Borrower and its Subsidiaries, and that:'.format(pr=s.get('pronoun', 'he'), t=s['title'],
                                                         pos=s.get('possessive', 'his')))

    def nc(name):
        return '' if tests.get(name, {}).get('ok') else 'non-'

    p('1.\tSet forth on Schedule 1 is a calculation of Liquidity as at the close of business on the '
      'Certification Period End Date reflecting {}compliance with the requirement of Section 7.01(a) '
      'of the Credit Agreement for such period.'.format(nc('Section 7.01(a) Minimum Liquidity')))
    p('2.\tSet forth on Schedule 2 is a calculation of Maximum Delinquency as at the close of business '
      'on the Certification Period End Date reflecting {}compliance with the requirement of Section '
      '7.01(b) of the Credit Agreement for such period.'.format(nc('Section 7.01(b) DQ30 Rate')))
    p('3.\tSet forth on Schedule 3 is a calculation of EBITDA Coverage as at the close of business on '
      'the Certification Period End Date reflecting {}compliance with the requirement of Section '
      '7.01(c) of the Credit Agreement for such period.'.format(nc('Section 7.01(c) EBITDA Coverage')))
    period = 'calendar month' if variant == 'month_end' else 'period'
    p('4.\tTo the best of my knowledge, the Borrower and the Loan Parties have during the {} ended as '
      'of the above date observed or performed all of their respective covenants and other '
      'agreements, and satisfied every condition, contained in the Credit Agreement and the other '
      'Loan Documents to which each is a party to. No Responsible Officer of the Borrower has '
      'obtained knowledge of any Default or Event of Default{}'.format(
          period, '.' if not st.get('default_exceptions') else
          ', except as specified herein: ' + st['default_exceptions']))
    p('5.\tAttached hereto as Exhibit A are copies of any amendment, supplement or other modification '
      'with respect to the Organization Documents of any Loan Party, since the most recently '
      'delivered Borrowing Base Certificate.')
    p('6.\tNo \u201cevent of default\u201d, \u201crapid amortization event\u201d or substantially similar '
      'event under any Material Indebtedness or indebtedness incurred under any or in connection with '
      'any SPV financing has occurred and is continuing{}'.format(
          '.' if not st.get('spv_event_exceptions') else
          ', except as specified herein: ' + st['spv_event_exceptions']))
    if variant == 'month_end':
        p('7.\tAttached hereto as Exhibit B is a list of each Deposit Account, Securities Account or '
          'other account of the Loan Parties (other than any Excluded Account) that has opened or '
          'closed, or ceased to become an Excluded Account, since the most recently delivered '
          'Borrowing Base Certificate.')
        p('8.\tAttached hereto as Exhibit C is a list of the Effective Advance Rate of each SPV Financing.')
        if imm and imm.get('attested'):
            p('9.\tThe assets or gross revenues (as determined in accordance with GAAP) of any Subsidiary '
              'which is an Immaterial Subsidiary or all Subsidiaries which are Immaterial Subsidiaries, '
              'in either case does not exceed the thresholds set forth in the definition of '
              '\u201cImmaterial Subsidiary\u201d.')
        elif imm:
            desc = '; '.join('{}: total assets {}, TTM gross revenues {}, T3M gross revenues {} ({:.2%} '
                             'of consolidated)'.format(r['name'], _money(r['assets']), _money(r['rev_ttm']),
                                                        _money(r['rev_t3m']), r['pct']) for r in imm['rows'])
            a = imm['agg']
            desc += '; aggregate: total assets {}, TTM gross revenues {}, T3M {:.2%} of consolidated'.format(
                _money(a['assets']), _money(a['rev_ttm']), a['pct'])
            p('9.\tThe assets or gross revenues (as determined in accordance with GAAP) of any Subsidiary '
              'which is an Immaterial Subsidiary or all Subsidiaries which are Immaterial Subsidiaries, '
              'in either case {} the thresholds set forth in the definition of \u201cImmaterial '
              'Subsidiary\u201d. {}.'.format('exceeds' if imm['exceeds'] else 'does not exceed', desc))
        else:
            p('9.\t[PENDING -- Immaterial Subsidiary test: Accounting figures not yet received]')
    p()
    p('IN WITNESS WHEREOF, the undersigned has executed this Borrowing Base Certificate as of the date '
      'first above written.')
    p('Jeeves Inc., a Delaware corporation')
    p('By:\t______________________________')
    p('\tName: ' + s['name'])
    p('\tTitle: ' + s['title'])
    d.add_page_break()
    p('Exhibit A to Borrowing Base Certificate', True)
    p(st.get('org_doc_amendments') or 'None.')
    if variant == 'month_end':
        p('Exhibit B to Borrowing Base Certificate', True)
        if delta is None:
            p('[PENDING -- account delta could not be computed]')
        else:
            items = ([('Opened', c) for c in delta['opened']] +
                     [('Ceased to be an Excluded Account', c) for c in delta['ceased_excluded']] +
                     [('Closed', c) for c in delta['closed']])
            if not items:
                p('None.')
            else:
                t = d.add_table(rows=1, cols=4)
                t.style = 'Table Grid'
                for i, h in enumerate(['Account', 'Currency', 'Purpose', 'Change']):
                    t.rows[0].cells[i].text = h
                for what, c in items:
                    r = t.add_row().cells
                    r[0].text = str(c.get('acct'))
                    r[1].text = str(c.get('currency') or '')
                    r[2].text = str(c.get('purpose') or c.get('acct_type') or '')
                    r[3].text = what
        p('Exhibit C to Borrowing Base Certificate', True)
        if not ears:
            p('[PENDING -- Effective Advance Rate not computed]')
        else:
            t = d.add_table(rows=1, cols=4)
            t.style = 'Table Grid'
            for i, h in enumerate(['SPV Financing', 'Indebtedness outstanding',
                                   'Eligible Outstanding Receivables Balance', 'Effective Advance Rate']):
                t.rows[0].cells[i].text = h
            for e in ears:
                r = t.add_row().cells
                r[0].text = e['name']
                r[1].text = _money(e['outstanding'])
                r[2].text = _money(e['eligible'])
                r[3].text = '{:.2%}'.format(e['rate'])
    d.add_page_break()
    p('Schedule 1 to Borrowing Base Certificate', True)
    p('Section 7.01(a) \u2013 Minimum Liquidity Covenant')
    p('Liquidity: {}\nMinimum required: {}\nCushion: {}\nResult: {}'.format(
        _money(m['liquidity']), _money(m['liquidity_floor']), _money(m['liquidity_cushion']),
        'Compliant' if tests['Section 7.01(a) Minimum Liquidity']['ok'] else 'NOT COMPLIANT'))
    p('Schedule 2 to Borrowing Base Certificate', True)
    p('Section 7.01(b) \u2013 Maximum Delinquency')
    lines = ['{}: {:.2%}'.format(k, v) for k, v in m['dq30_monthly']]
    p('\n'.join(lines) + '\nThree-month rolling average: {:.2%}\nMaximum: 2.50%\nResult: {}'.format(
        m['dq30_avg'], 'Compliant' if tests['Section 7.01(b) DQ30 Rate']['ok'] else 'NOT COMPLIANT'))
    p('Schedule 3 to Borrowing Base Certificate', True)
    p('Section 7.01(c) \u2013 EBITDA Coverage')
    p(tests['Section 7.01(c) EBITDA Coverage']['detail'])
    if pending:
        d.add_page_break()
        p('INTERNAL -- REMOVE BEFORE DELIVERY: pending items', True)
        for x in pending:
            p('- ' + x)
    d.save(out_path)
    return out_path


def build_certificate(workbook, as_of, variant, cov_res, out_dir):
    cfg = _load(CONFIG, None)
    if cfg is None:
        raise RuntimeError('missing ' + str(CONFIG))
    inputs = _load(INPUTS, {})
    per = inputs.get(as_of, {})
    pending = []
    for k in cfg.get('required_period_confirmations', {}).get(variant, []):
        if not per.get('confirmations', {}).get(k):
            pending.append('Officer confirmation not recorded for {}: {}'.format(
                as_of, cfg['confirmation_text'][k]))
    delta = ears = imm = None
    if variant == 'month_end':
        delta, err = account_delta(workbook, as_of)
        if err:
            pending.append('Account delta: ' + err)
        elif (delta['opened'] or delta['ceased_excluded'] or delta['closed']) and \
                not per.get('confirmations', {}).get('account_delta_reviewed'):
            pending.append('Exhibit B lists {} account change(s) vs {} -- review list, then set '
                           'confirmations.account_delta_reviewed'.format(
                               len(delta['opened']) + len(delta['ceased_excluded']) + len(delta['closed']),
                               delta['baseline']))
        ears, p2 = effective_advance_rates(as_of, cfg, out_dir)
        pending += p2
        imm, p3 = immaterial_test(as_of, cfg, inputs)
        pending += p3
    stamp = as_of.replace('-', '')
    kind = 'Monthly' if variant == 'month_end' else 'Weekly'
    out = os.path.join(out_dir, 'Certificate - NB {} Borrowing Base Certificate - {}.docx'.format(kind, stamp))
    render(out, as_of, variant, cov_res, cfg, inputs, delta, ears, imm, pending)
    return {'path': out, 'pending': pending, 'delta': delta, 'ears': ears, 'imm': imm}


def main():
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument('--workbook', required=True)
    ap.add_argument('--as-of', required=True)
    ap.add_argument('--variant', default='month_end', choices=['weekly', 'month_end'])
    ap.add_argument('--out-dir', default=os.environ.get('OUTPUTS_PATH', '.'))
    ap.add_argument('--save-snapshot', action='store_true',
                    help='record this workbook as the account baseline (run once the certificate is delivered)')
    a = ap.parse_args()
    if a.save_snapshot:
        save_snapshot(a.workbook, a.as_of)
        print('snapshot saved for', a.as_of)
        return
    sys.path.insert(0, str(HERE))
    from nb_covenant_check import check_workbook
    cov = check_workbook(a.workbook)
    res = build_certificate(a.workbook, a.as_of, a.variant, cov, a.out_dir)
    print('CERT:', res['path'])
    for e in res['ears'] or []:
        print('EAR {}: {} / {} = {:.4%} ({} rows, draws {}, repaid {}, {})'.format(
            e['name'], _money(e['outstanding']), _money(e['eligible']), e['rate'], e['rows'],
            _money(e['draws']), _money(e['repaid']), e['source']))
    if res['delta']:
        dd = res['delta']
        print('ACCOUNT DELTA vs {}: opened {}, ceased-excluded {}, closed {}'.format(
            dd['baseline'], len(dd['opened']), len(dd['ceased_excluded']), len(dd['closed'])))
    print('PENDING ({}):'.format(len(res['pending'])))
    for x in res['pending']:
        print('  -', x)


if __name__ == '__main__':
    main()
