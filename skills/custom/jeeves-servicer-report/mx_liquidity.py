"""MX servicer report - Exhibit B G82 "Liquidity" (Section 8.1(x), MX Loan Agreement).

Liquidity = Unrestricted Cash of Parent + consolidated Subsidiaries: cash available for general
corporate purposes, NOT in a reserve account, NOT collateral for any other facility, NOT subject to
a Lien (Agent/CIM liens excepted). Definition identical in the executed LSA, 1st Amendment
(2026-07-14) and 2nd Amendment (2026-08-10).

Method approved by Brian 2026-11-12 ("Use the 50 number and make sure you update it going forward"):
    CICO 'Bank balances' Total cash at the last balance date <= period end
    (the 'Restricted deposits' block is already outside Total cash)
  - every account above Total cash whose Purpose contains 'collateral' or 'reserve'
    (Oct-26: BTG 5506 SCD collateral, JPM 0244 / JPM MX 7091 customer collateral, Monex 6135 trust reserve)
Oct-26 (10/30 balances, 261030 CICO) = 50,194,887.

G82 was a hardcode of 29,682,880 (5/29 CICO) from the 20260715 report through 20261115 - never refreshed.

CLI:
  python mx_liquidity.py --period-end 2026-10-31                          # compute + print bridge
  python mx_liquidity.py --period-end 2026-10-31 --report <xlsx> --write  # also write G82 + comment
"""
import argparse
import datetime as dt
import re
from pathlib import Path

import openpyxl
from openpyxl.comments import Comment
from openpyxl.styles import fonts

fonts.Font.family.max = 255  # CICO workbooks carry font family ids > 14

CICO_DIR = Path(r"C:\Jeeves\redshift-bot\deer-flow\backend\.deer-flow\treasury\CICO")
FILE_RE = re.compile(r"^(\d{6}) Jeeves - CICO(?: v\d+)?\.xlsx$", re.I)
EXCLUDE_PURPOSE = ("collateral", "reserve")


def _grid(path):
    wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
    return [list(r) for r in wb["Bank balances"].iter_rows(min_row=1, max_row=700, values_only=True)]


def _total_row(grid):
    for r in range(8, len(grid)):
        labs = [str(x).strip().lower() for x in grid[r][:10] if isinstance(x, str)]
        if "total cash" in labs:
            return r
    raise ValueError("CICO: 'Total cash' row not found")


def _date_col(grid, period_end):
    hdr = grid[6]
    tot = _total_row(grid)
    best = None
    for i, v in enumerate(hdr):
        if isinstance(v, dt.datetime) and v.date() <= period_end:
            filled = sum(1 for r in range(8, tot) if isinstance(grid[r][i], (int, float)) and grid[r][i] != 0)
            if filled > 30 and (best is None or v.date() > best[1]):
                best = (i, v.date())
    return best


def find_cico(period_end):
    """CICO snapshot AS SENT for the last balance date <= period_end: the EARLIEST local file that carries
    that date's column. Later files restate prior days (Oct-26: 261030 file shows 10/30 Total cash 52.84M,
    261110 file restates 10/30 to 67.36M); the servicer report uses the as-sent figure, consistent with the
    CICO email and the lender figures already reported (Brian 2026-11-12: "Use the 50 number")."""
    cands = []
    for f in CICO_DIR.glob("*.xlsx"):
        m = FILE_RE.match(f.name)
        if m:
            d = dt.datetime.strptime(m.group(1), "%y%m%d").date()
            if period_end - dt.timedelta(days=6) <= d <= period_end + dt.timedelta(days=10):
                cands.append((d, f))
    hits = []
    for d, f in sorted(cands):
        try:
            g = _grid(f)
            hit = _date_col(g, period_end)
        except Exception:
            continue
        if hit and (period_end - hit[1]).days <= 5:
            hits.append((hit[1], d, f, g, hit))
    if not hits:
        raise FileNotFoundError(f"No CICO file in {CICO_DIR} with balances within 5 days before {period_end}")
    target = max(h[0] for h in hits)
    _, _, f, g, hit = min((h for h in hits if h[0] == target), key=lambda h: h[1])
    return f, g, hit


def compute(period_end):
    f, grid, (col, bal_date) = find_cico(period_end)
    hdr = grid[6]
    pcol = next(i for i, v in enumerate(hdr) if isinstance(v, str) and v.strip().lower() == "purpose")
    tot = _total_row(grid)
    total = float(grid[tot][col])
    excl = []
    for r in range(8, tot):
        v = grid[r][col]
        purpose = str(grid[r][pcol] or "").lower()
        if isinstance(v, (int, float)) and v and any(k in purpose for k in EXCLUDE_PURPOSE):
            label = " ".join(str(x).strip() for x in grid[r][2:6] if x is not None)
            excl.append((label, str(grid[r][pcol]).strip(), float(v)))
    liq = total - sum(e[2] for e in excl)
    return dict(file=f.name, balance_date=bal_date, total_cash=total, exclusions=excl, liquidity=liq)


def bridge_text(res):
    lines = [f"Liquidity (8.1(x)) = Unrestricted Cash at {res['balance_date']:%m/%d/%Y}, source {res['file']} (Bank balances).",
             f"CICO Total cash (restricted deposits already excluded): {res['total_cash']:,.0f}"]
    lines += [f"less {lab} ({p[:40]}): ({v:,.0f})" for lab, p, v in res["exclusions"]]
    lines.append(f"= {res['liquidity']:,.0f}. Built by mx_liquidity.py.")
    return "\n".join(lines)


def write(report_path, res, out_path=None):
    wb = openpyxl.load_workbook(report_path)
    ws = wb["Exhibit B"]
    assert str(ws["B82"].value).startswith("b) Liquidity"), f"Exhibit B B82 is not the Liquidity row: {ws['B82'].value!r}"
    before = ws["G82"].value
    ws["G82"].value = round(res["liquidity"])
    ws["G82"].comment = Comment(bridge_text(res), "DeerFlow")
    ws["G82"].comment.width, ws["G82"].comment.height = 420, 160
    wb.save(out_path or report_path)
    return before, ws["G82"].value


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--period-end", required=True, help="last calendar day of the reporting month, YYYY-MM-DD")
    ap.add_argument("--report")
    ap.add_argument("--write", action="store_true")
    a = ap.parse_args()
    res = compute(dt.date.fromisoformat(a.period_end))
    print(bridge_text(res))
    if a.write:
        b, n = write(a.report, res)
        print(f"G82: {b} -> {n}")
