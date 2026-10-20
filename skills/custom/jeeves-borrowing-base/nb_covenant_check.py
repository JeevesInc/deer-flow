"""NB Credit Agreement covenant gate for the weekly borrowing base.

Why this exists (2026-09-16): the Monday NB borrowing base was drafted to NB and
Wilmington Trust while the certificate itself showed Liquidity of $7.60MM against
the Section 7.01(a) floor of $15.00MM -- a covenant failure nobody flagged.
Brian's rule: "NEVER ALLOW IT TO BE CREATED WITH A COVENANT FAILURE THAT ISN'T
FLAGGED."  report_scheduler_cron.py calls check_workbook() BEFORE it builds the
Gmail draft and withholds the draft (fail-closed) if anything fails or if this
check cannot be evaluated.

The workbook stores every covenant cell as an Excel formula with no cached value
(openpyxl writes formulas, Excel computes them on open), so nothing downstream
can read the result.  This module re-computes the same formulas in Python from
the static inputs on the tape / bank_accts / obligors / dq30_detail tabs.

Tests evaluated:
  - Section 7.01(a)  Minimum Liquidity   (floor $15MM; $25MM if Borrowings >= $100MM)
  - Section 7.01(b)  DQ30 Rate           (3-month rolling average must be < 2.50%)
  - Section 7.01(c)  EBITDA Coverage     (only inside the testing period AND when
                                          Liquidity < 7.01(a) floor + $10MM)
  - Borrowing Base sufficiency           (BB must cover Total Drawn + Advance Request)
"""
import re
from datetime import datetime

import os
from openpyxl import load_workbook


def _load(path):
    """load_workbook, tolerant of broken style indexes.

    2026-10-19 09:01: the gate crashed with an openpyxl IndexError while reading
    cell styles of the NB workbook it had just built. This check reads VALUES only,
    so on a style-table failure re-load a temp copy with every cell style ref
    (s="N") stripped from the sheet XML. Values/formulas are untouched.
    """
    try:
        return load_workbook(path)
    except IndexError:
        import re as _re, tempfile, zipfile
        fd, tmp = tempfile.mkstemp(suffix='.xlsx'); os.close(fd)
        with zipfile.ZipFile(path) as zin, zipfile.ZipFile(tmp, 'w', zipfile.ZIP_DEFLATED) as zout:
            for item in zin.infolist():
                data = zin.read(item.filename)
                if item.filename.startswith('xl/worksheets/') and item.filename.endswith('.xml'):
                    data = _re.sub(rb'(<(?:c|row|col)\b[^>]*?)\s(?:s|style)="\d+"', rb'\1', data)
                zout.writestr(item, data)
        try:
            return load_workbook(tmp)
        finally:
            try: os.remove(tmp)
            except OSError: pass

DQ30_CAP = 0.025
EBITDA_FLOOR = -1000000.0


def _f(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def _n(v):
    """Excel N(): numbers pass through, TRUE is 1, text is 0."""
    if v is True:
        return 1.0
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        return float(v)
    return 0.0


def check_workbook(path):
    wb = _load(path)

    # ---- eligible receivables (tape: F country, K dpd, M charge-off, O modified,
    #      P transfer_flag, Q sofom_balance_usd, R balance_usd)
    tp = wb['tape']
    elig_bal = 0.0
    for r in range(2, tp.max_row + 1):
        bal = _f(tp.cell(r, 18).value)
        if bal <= 0:
            continue
        not_co = 0 if _n(tp.cell(r, 13).value) == 1 else 1
        unenc = 0 if (_n(tp.cell(r, 16).value) == 1 or _f(tp.cell(r, 17).value) > 0) else 1
        dpd30 = 1 if _n(tp.cell(r, 11).value) <= 30 else 0
        dom = 1 if _f(tp.cell(r, 6).value) in (170.0, 76.0, 840.0) else 0
        notmod = 0 if _n(tp.cell(r, 15).value) == 1 else 1
        if not_co * unenc * dpd30 * dom * notmod == 1:
            elig_bal += bal

    # ---- excess concentration (Summary rows 23-25, non-duplicative)
    ob = wb['obligors']
    cum, cums = 0.0, {}
    for r in range(2, ob.max_row + 1):
        if ob.cell(r, 1).value in (None, ''):
            break
        cum += _f(ob.cell(r, 5).value) - _f(ob.cell(r, 6).value)
        cums[r] = cum
    e1 = max(0.0, cums.get(2, 0.0) - 0.05 * elig_bal)
    e5 = max(0.0, cums.get(6, 0.0) - e1 - 0.25 * elig_bal)
    e10 = max(0.0, cums.get(11, 0.0) - (e1 + e5) - 0.35 * elig_bal)
    excess = e1 + e5 + e10
    counted = elig_bal - excess

    # ---- cash (bank_accts: C ccy, G acct_type, H controlled, I excluded,
    #      J fx_hedged, Q local balance, R fx rate)
    ba = wb['bank_accts']
    bb_cash = liquidity = total_cash = 0.0
    # 2026-09-28: Section 7.01 is tested on the last Business Day of the month.
    # build_nb writes test-date balances to cols S/T (Liquidity in col U); when
    # present, Liquidity uses them and the BB cash pool stays on as-of balances.
    _u1 = str(ba.cell(1, 21).value or '')
    # 2026-10-14: the form's liquidity tab (E7 = SUM(bank_accts!U)) is driven by the test-date
    # balances in cols S/T even when the U1 header text is blank, so detect month-end mode from
    # the data too. Without this the check fell back to as-of balances (10/5 form: $12.29MM vs the
    # $16.02MM 9/30 figure on the certificate).
    me_mode = ('month-end' in _u1.lower()
               or any(ba.cell(r, 19).value not in (None, '') for r in range(2, ba.max_row + 1)))
    _td = re.search(r'(\d{2}-[A-Za-z]{3}-\d{2})', _u1)
    liq_date = (_td.group(1) if _td else 'month-end (bank_accts S/T)') if me_mode \
        else 'as-of date (no month-end columns)'
    for r in range(2, ba.max_row + 1):
        if ba.cell(r, 1).value in (None, ''):
            continue
        ccy, typ = ba.cell(r, 3).value, ba.cell(r, 7).value
        ctrl, excl, hedge = ba.cell(r, 8).value, ba.cell(r, 9).value, ba.cell(r, 10).value
        usd = _f(ba.cell(r, 17).value) * _f(ba.cell(r, 18).value)
        total_cash += usd
        haircut = 0.8 if (ccy != 'USD' and hedge != 'Y') else 1.0
        if typ in ('Collections', 'Origination') and ctrl == 'Yes' and excl != 'Yes':
            bb_cash += haircut * usd
        if ctrl == 'Yes' and excl != 'Yes':
            liquidity += haircut * (_f(ba.cell(r, 19).value) * _f(ba.cell(r, 20).value)
                                    if me_mode else usd)
    liq = wb['liquidity']
    trust_co = _f(liq['E8'].value)      # Colombian Security Trust (manual input)
    trust_mx = _f(liq['E9'].value)      # Mexican Trustee (manual input)
    excluded_atc = _f(liq['E10'].value)
    liquidity += trust_co + trust_mx + excluded_atc

    # ---- borrowings
    hd = wb['historical_draws']
    drawn = sum(_f(hd.cell(r, 3).value) for r in range(2, hd.max_row + 1))
    adv_req = _f(wb['Summary']['L76'].value)
    borrowings = drawn + adv_req
    floor = 25000000.0 if borrowings >= 100000000 else 15000000.0
    cushion = liquidity - floor

    # ---- DQ30 (dq30_detail: A month_end, G dpd, H balance_usd,
    #      I transfer_flag, K sofom_financed_usd)
    dd = wb['dq30_detail']
    tot, over = {}, {}
    for r in range(2, dd.max_row + 1):
        me = dd.cell(r, 1).value
        if me is None:
            continue
        if dd.cell(r, 9).value == 'on' and _n(dd.cell(r, 11).value) > 0:
            continue                      # encumbered -- outside the covenant pool
        me = str(me)[:10]
        bal = _f(dd.cell(r, 8).value)
        tot[me] = tot.get(me, 0.0) + bal
        if _n(dd.cell(r, 7).value) > 30:
            over[me] = over.get(me, 0.0) + bal
    months = sorted(tot)
    monthly = [(m, (over.get(m, 0.0) / tot[m]) if tot[m] else None) for m in months]
    usable = [v for _, v in monthly if v is not None]
    dq_avg = sum(usable) / len(usable) if len(usable) == 3 else None

    # ---- EBITDA coverage (Section 7.01(c))
    eb = wb['ebitda']
    end_date = tp.cell(2, 1).value
    closing = eb['E10'].value
    if isinstance(closing, str):
        m = re.match(r'=DATE\((\d+),\s*(\d+),\s*(\d+)\)', closing.strip())
        closing = datetime(int(m.group(1)), int(m.group(2)), int(m.group(3))) if m else None
    testing_from = None
    if closing is not None:
        testing_from = closing.replace(year=closing.year + 1)
    if testing_from is None:
        # fail closed -- an unreadable Closing Date must not silently pass 7.01(c)
        in_period, ebitda_unknown = True, True
    else:
        in_period, ebitda_unknown = bool(end_date and end_date >= testing_from), False
    ebitda_triggered = in_period and (liquidity < floor + 10000000.0)
    ebitda_avg = sum(_f(eb.cell(34, c).value) for c in (3, 4, 5)) / 3.0

    # ---- step-up + borrowing base
    # Step-up DQ prong = each of the 3 month-ends < 1.50% (three consecutive months), NOT the average
    step_up = (len(usable) == 3 and all(v < 0.015 for v in usable)
               and liquidity >= 17500000.0 and liquidity >= 27500000.0)
    rate = 0.925 if step_up else 0.90
    bb = counted * rate + bb_cash * 0.95
    deficit = bb - borrowings

    tests = []
    tests.append({
        'name': 'Section 7.01(a) Minimum Liquidity',
        'ok': liquidity >= floor,
        'detail': 'Liquidity ${:,.0f} as of {} vs floor ${:,.0f} (cushion ${:,.0f})'.format(
            liquidity, liq_date, floor, cushion)})
    tests.append({
        'name': 'Section 7.01(b) DQ30 Rate',
        'ok': (dq_avg is not None and dq_avg < DQ30_CAP),
        'detail': ('3-mo rolling avg {:.4%} vs cap {:.2%}'.format(dq_avg, DQ30_CAP)
                   if dq_avg is not None else 'input required -- month-end data incomplete')})
    tests.append({
        'name': 'Section 7.01(c) EBITDA Coverage',
        'ok': ((not ebitda_triggered) or (ebitda_avg >= EBITDA_FLOOR)) and not ebitda_unknown,
        'detail': ('Closing Date unreadable -- cannot evaluate 7.01(c)' if ebitda_unknown
                   else 'not triggered (testing begins {})'.format(testing_from.date())
            if not ebitda_triggered else
            'triggered -- trailing 3-mo avg ${:,.0f} vs floor ${:,.0f}'.format(
                ebitda_avg, EBITDA_FLOOR))})
    tests.append({
        'name': 'Borrowing Base sufficiency',
        'ok': deficit >= 0,
        'detail': 'BB ${:,.0f} vs borrowings ${:,.0f} ({} ${:,.0f})'.format(
            bb, borrowings, 'excess' if deficit >= 0 else 'DEFICIT', abs(deficit))})

    failures = [t for t in tests if not t['ok']]
    return {
        'ok': not failures,
        'tests': tests,
        'failures': failures,
        'metrics': {
            'eligible_receivables': elig_bal, 'excess_concentration': excess,
            'receivables_counted': counted, 'total_cash': total_cash,
            'bb_cash': bb_cash, 'liquidity': liquidity, 'liquidity_floor': floor,
            'liquidity_cushion': cushion, 'liquidity_test_date': liq_date, 'trust_co': trust_co, 'trust_mx': trust_mx,
            'drawn': drawn, 'advance_request': adv_req, 'borrowings': borrowings,
            'dq30_monthly': monthly, 'dq30_avg': dq_avg, 'step_up': step_up,
            'advance_rate': rate, 'borrowing_base': bb, 'bb_excess_deficit': deficit,
        },
    }


def format_report(res):
    lines = ['COVENANT CHECK: ' + ('PASS' if res['ok'] else 'FAIL')]
    for t in res['tests']:
        lines.append('  [{}] {} -- {}'.format('OK' if t['ok'] else 'FAIL',
                                              t['name'], t['detail']))
    return '\n'.join(lines)


if __name__ == '__main__':
    import sys
    r = check_workbook(sys.argv[1])
    print(format_report(r))
    for k, v in r['metrics'].items():
        print('  {:24} {}'.format(k, '{:,.2f}'.format(v) if isinstance(v, float) else v))
    sys.exit(0 if r['ok'] else 2)
