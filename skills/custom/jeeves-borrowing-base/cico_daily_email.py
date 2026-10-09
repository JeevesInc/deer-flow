"""CICO daily balances email - production version of the approved 2026-10-07 test
(daily_balances_email.py). Triggered by a Slack DM to the bot from Axel, William Filho
or Mariana (hook in app/channels/slack.py -> _handle_cico_trigger).

Flow: pull the newest 'YYMMDD Jeeves - CICO.xlsx' from Axel's OneDrive (FinOps V3/CICO,
top-level only) -> render the Bank balances tab exactly as the approved test -> send from
Brian's Gmail to the CICO distribution list -> Slack the trigger user + Brian.

Usage:
  cico_daily_email.py [--send] [--to a@x,b@y] [--file PATH | --no-fetch] [--trigger-user UID] [--force]
Without --send it only writes the HTML preview and prints a summary.
"""
import argparse, base64, datetime as dt, json, os, re, subprocess, sys, time, traceback, zipfile
from pathlib import Path
from urllib.parse import quote

import openpyxl
from openpyxl.styles import fonts
fonts.Font.family.max = 255

SKILLS = os.environ.get("SKILLS_PATH") or r"C:\Jeeves\redshift-bot\deer-flow\skills"
if SKILLS.startswith("/mnt"):
    SKILLS = r"C:\Jeeves\redshift-bot\deer-flow\skills"
sys.path.insert(0, os.path.join(SKILLS, "custom", "_shared"))

ROOT_DF = Path(r"C:\Jeeves\redshift-bot\deer-flow\backend\.deer-flow")
TREAS = ROOT_DF / "treasury"
DL = TREAS / "CICO"
LOG = TREAS / "cico_daily_email.log"
STATE = ROOT_DF / "_cico_email_state.json"
LOCK = TREAS / "cico_daily_email.lock"
M365 = Path(r"C:\Jeeves\redshift-bot\tmp\m365_session.json")
SITE = "https://jeevesorg-my.sharepoint.com/personal/axel_tryjeeves_com"
FOLDER = "/personal/axel_tryjeeves_com/Documents/Documentos/FinOps V3/CICO"
SLACK_TOOL = os.path.join(SKILLS, "custom", "slack-search", "slack_tool.py")
BRIAN_SLACK = "U05B5HGNCN9"

# Distribution = Mariana's '261007 CICO- Daily balances' (2026-10-07 23:04 MT) To-line,
# minus Brian (sender) plus Mariana (she was the sender there).
DIST = [
    "alexander@tryjeeves.com", "dileep@tryjeeves.com", "isabel@tryjeeves.com",
    "shalom@tryjeeves.com", "martha@tryjeeves.com", "lizz@tryjeeves.com",
    "axel@tryjeeves.com", "william.filho@tryjeeves.com", "debbie@tryjeeves.com",
    "pradeepan@tryjeeves.com", "robertoconta@tryjeeves.com",
    "mariana.herreracruz@tryjeeves.com",
]
TRIGGER_NAMES = {"U075NLRHYBX": "Axel", "U0A5AT863BL": "William", "U09AGT85RSN": "Mariana",
                 BRIAN_SLACK: "Brian"}
FILE_RE = re.compile(r"^(\d{6}) Jeeves - CICO(?: v(\d+))?\.xlsx$", re.I)


def say(*a):
    msg = time.strftime("%Y-%m-%d %H:%M:%S ") + " ".join(str(x) for x in a)
    print(msg, flush=True)
    with open(LOG, "a", encoding="utf-8") as f:
        f.write(msg + "\n")


def slack(uid, text):
    try:
        r = subprocess.run([sys.executable, SLACK_TOOL, "send", uid, text], timeout=60,
                           capture_output=True, text=True)
        if r.returncode: say("slack send rc", r.returncode, r.stderr[-300:])
    except Exception as e:
        say("slack send failed", uid, e)


# ---------------------------------------------------------------- fetch (SharePoint)
def fetch_latest():
    sys.path.insert(0, r"C:\Jeeves\redshift-bot\scripts")
    import zscaler_reauth as zr
    zr._load_env_config()
    from playwright.sync_api import sync_playwright

    def login(page):
        page.goto(SITE + "/Documents/Forms/All.aspx", wait_until="domcontentloaded", timeout=60000)
        okta_tries = 0
        for _ in range(40):
            time.sleep(2)
            u = page.url
            if "sharepoint.com" in u and "login" not in u:
                return True
            if "login.microsoftonline.com" in u:
                f = page.locator('input[name="loginfmt"]')
                if f.count() and f.first.is_visible():
                    f.first.fill(zr.OKTA_USERNAME); page.locator("#idSIButton9").first.click(); continue
                k = page.locator("#idSIButton9, #acceptButton")
                if k.count() and k.first.is_visible():
                    k.first.click(); continue
                acct = page.locator('div[data-test-id="' + zr.OKTA_USERNAME + '"], div.table[role="button"]')
                if acct.count() and acct.first.is_visible():
                    acct.first.click(); continue
            if "okta" in u.lower():
                okta_tries += 1
                if okta_tries > 3:
                    break
                btn = page.get_by_text("Open Okta Verify", exact=True)
                if btn.count():
                    zr._handle_open_okta_verify_button(page)
                else:
                    zr._handle_okta_flow(page)
                try: page.wait_for_url(lambda x: "okta" not in x.lower(), timeout=60000)
                except Exception: pass
        page.screenshot(path=str(TREAS / "cico_email_login_fail.png"))
        return False

    with sync_playwright() as p:
        browser = zr._launch_edge_with_retry(p, args=["--no-first-run", "--no-default-browser-check"])
        kw = {"accept_downloads": True}
        if M365.exists(): kw["storage_state"] = str(M365)
        ctx = browser.new_context(**kw)
        page = ctx.new_page()
        if not login(page):
            browser.close(); raise RuntimeError("SharePoint login failed (see cico_email_login_fail.png)")
        ctx.storage_state(path=str(M365))
        q = quote(FOLDER.replace("'", "''"))
        r = ctx.request.get(SITE + f"/_api/web/GetFolderByServerRelativeUrl('{q}')/Files",
                            headers={"Accept": "application/json;odata=nometadata"}, timeout=60000)
        if not r.ok: raise RuntimeError(f"list failed {r.status}")
        cands = []
        for f in r.json().get("value", []):
            m = FILE_RE.match(f["Name"])
            if m: cands.append((m.group(1), f.get("TimeLastModified", ""), f))
        if not cands: raise RuntimeError("no 'YYMMDD Jeeves - CICO.xlsx' in FinOps V3/CICO")
        cands.sort(key=lambda c: (c[0], c[1]))
        f = cands[-1][2]
        dest = DL / f["Name"]
        fq = quote(f["ServerRelativeUrl"].replace("'", "''"))
        resp = ctx.request.get(SITE + f"/_api/web/GetFileByServerRelativeUrl('{fq}')/$value", timeout=300000)
        if not resp.ok: raise RuntimeError(f"download failed {resp.status} {f['Name']}")
        DL.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(resp.body())
        browser.close()
        say("fetched", f["Name"], "modified", f.get("TimeLastModified"))
        return dest, f.get("TimeLastModified")


FETCH_RESULT = TREAS / "cico_fetch_result.json"
REAUTH_DIR = os.path.join(SKILLS, "custom", "zscaler-reauth")


def in_session_zero():
    import ctypes
    sid = ctypes.c_ulong()
    k32 = ctypes.windll.kernel32
    return bool(k32.ProcessIdToSessionId(k32.GetCurrentProcessId(), ctypes.byref(sid))) and sid.value == 0


def fetch():
    """Edge (headed) cannot start in session 0 (gateway runs as SYSTEM) -> hop to Brian's desktop session."""
    if not in_session_zero():
        return fetch_latest()
    sys.path.insert(0, REAUTH_DIR)
    from session_launcher import run_in_user_session
    FETCH_RESULT.unlink(missing_ok=True)
    rc, out = run_in_user_session([sys.executable, os.path.abspath(__file__), "--fetch-only"], timeout=600)
    if rc != 0 or not FETCH_RESULT.exists():
        raise RuntimeError(f"SharePoint fetch in user session failed rc={rc}: {str(out)[-300:]}")
    r = json.loads(FETCH_RESULT.read_text(encoding="utf-8"))
    return Path(r["path"]), r["modified"]


def latest_local():
    c = [(FILE_RE.match(p.name).group(1), p.stat().st_mtime, p) for p in DL.glob("*.xlsx") if FILE_RE.match(p.name)]
    return sorted(c)[-1][2]


# ---------------------------------------------------------------- build (approved logic)
def hidden_rows(path, sheet):
    z = zipfile.ZipFile(path)
    wbx = z.read("xl/workbook.xml").decode("utf8")
    rid = re.search(r'<sheet[^>]*name="%s"[^>]*r:id="([^"]+)"' % re.escape(sheet), wbx).group(1)
    rels = z.read("xl/_rels/workbook.xml.rels").decode("utf8")
    tgt = re.search(r'Id="%s"[^>]*Target="([^"]+)"' % rid, rels) or re.search(r'Target="([^"]+)"[^>]*Id="%s"' % rid, rels)
    t = tgt.group(1).lstrip("/"); t = t if t.startswith("xl") else "xl/" + t
    hid = set()
    with z.open(t) as f:
        buf = ""
        for chunk in iter(lambda: f.read(1 << 20), b""):
            buf += chunk.decode("utf8", "ignore")
            for m in re.finditer(r"<row [^>]*>", buf):
                tag = m.group(0)
                if 'hidden="1"' in tag or 'hidden="true"' in tag:
                    hid.add(int(re.search(r' r="(\d+)"', tag).group(1)))
            buf = buf[-2000:]
    return hid


def build(FILE):
    wb = openpyxl.load_workbook(FILE, read_only=True, data_only=True)
    ws = wb["Bank balances"]
    grid = [list(r) for r in ws.iter_rows(min_row=1, max_row=600, values_only=True)]
    hdr = grid[6]

    def find(label):
        for i, v in enumerate(hdr):
            if isinstance(v, str) and v.strip().lower().startswith(label): return i
    ci_country, ci_bank, ci_acct, ci_ccy = find("country"), find("bank"), find("account"), find("original")
    date_cols = [i for i, v in enumerate(hdr) if isinstance(v, dt.datetime)]
    start = 8
    def rowtxt(r): return " ".join(str(v) for v in grid[r][:10] if v is not None).lower()
    summ = next(r for r in range(len(grid)) if "summary by country" in rowtxt(r))
    end = next(r for r in range(summ, len(grid)) if "total cash" in rowtxt(r)) + 1
    HIDDEN = hidden_rows(FILE, "Bank balances")
    cim = next(r for r in range(start, end) if "cim reporting" in rowtxt(r))
    nb = next(r for r in range(len(grid)) if "nb controlled accounts" in rowtxt(r))
    nb_end = next(r for r in range(nb, len(grid)) if "total cash" in rowtxt(r))
    ORDER = list(range(start, cim)) + list(range(nb, nb_end + 1)) + list(range(summ, end))
    RENAME = {nb: "NB Liquidity", nb_end: "Total NB Liquidity"}

    def filled(ci):
        return sum(1 for r in range(start, end) if isinstance(grid[r][ci], (int, float)) and grid[r][ci] != 0)
    dc = [c for c in date_cols if filled(c) > 30]
    cur, prev = dc[-1], dc[-2]
    d_cur, d_prev = hdr[cur], hdr[prev]

    def fmt(v):
        if not isinstance(v, (int, float)) or abs(v) < 0.5: return "-"
        s = f"{abs(v):,.0f}"
        return f"({s})" if v < 0 else s

    rows_html, big_moves = [], []
    for r in ORDER:
        if (r + 1) in HIDDEN and r not in RENAME: continue
        row = grid[r]
        labels = [row[ci_country], row[ci_bank], row[ci_acct], row[ci_ccy]]
        v, p = row[cur], row[prev]
        txt = [str(x).strip() if x is not None else "" for x in labels]
        if nb < r < nb_end and not any(txt):
            t2 = [str(x).strip() for x in row[:ci_ccy + 3] if isinstance(x, str) and x.strip()]
            txt = (t2 + ["", "", "", ""])[:4]
        section = next((str(x) for x in row[:ci_country + 1] if isinstance(x, str) and x.strip() and x.strip() != "x"), "")
        if r in RENAME: section, txt = RENAME[r], ['', '', '', '']
        if not any(txt) and not section: continue
        if not any(txt):
            is_total = isinstance(v, (int, float))
            style = "font-weight:bold;background:#f2f2f2" if is_total else "font-weight:bold;padding-top:10px"
            delta = (v - p) if isinstance(v, (int, float)) and isinstance(p, (int, float)) else None
            rows_html.append(f'<tr style="{style}"><td colspan="4">{section}</td><td align="right">{fmt(v) if is_total else ""}</td>'
                             f'<td align="right">{fmt(delta) if delta is not None else ""}</td></tr>')
            continue
        if not isinstance(v, (int, float)) and not isinstance(p, (int, float)): continue
        delta = (v or 0) - (p or 0) if isinstance(v, (int, float)) else None
        flag = ' style="color:#b00000"' if delta is not None and abs(delta) >= 1_000_000 else ""
        if flag: big_moves.append(f"{txt[1]} {txt[2]} {fmt(delta)}")
        rows_html.append("<tr>" + "".join(f"<td>{t}</td>" for t in txt) +
                         f'<td align="right">{fmt(v)}</td><td align="right"{flag}>{fmt(delta)}</td></tr>')

    total_v, total_p = grid[end - 1][cur], grid[end - 1][prev]
    html = f"""<div style="font-family:Calibri,Arial;font-size:12px">
<p><b>Bank balances &ndash; {d_cur:%d-%b-%y}</b> (USD; &Delta; vs {d_prev:%d-%b-%y}; moves &ge; $1M in red)</p>
<table cellspacing="0" cellpadding="3" style="border-collapse:collapse;font-size:12px">
<tr style="background:#1f3864;color:white"><th align="left">Country</th><th align="left">Bank / platform</th>
<th align="left">Account</th><th align="left">Ccy</th><th align="right">{d_cur:%d-%b-%y}</th><th align="right">&Delta; d/d</th></tr>
{''.join(rows_html)}
</table>
<p style="color:#888">NB Liquidity = CICO &lsquo;NB controlled accounts&rsquo; block (BTG 6540 and JPM 9269 at 80%), as Axel built it. Generated from {os.path.basename(FILE)} (Bank balances tab).</p></div>"""
    return dict(html=html, d_cur=d_cur, d_prev=d_prev, rows=len(rows_html), total=total_v,
                total_delta=(total_v - total_p) if isinstance(total_v, (int, float)) and isinstance(total_p, (int, float)) else None,
                big_moves=big_moves)


def send_mail(html, subject, to):
    from email.mime.text import MIMEText
    from google_auth import get_credentials
    from googleapiclient.discovery import build as gbuild
    m = MIMEText(html, "html"); m["to"] = ", ".join(to); m["subject"] = subject
    svc = gbuild("gmail", "v1", credentials=get_credentials())
    return svc.users().messages().send(userId="me", body={"raw": base64.urlsafe_b64encode(m.as_bytes()).decode()}).execute().get("id")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--send", action="store_true"); ap.add_argument("--to")
    ap.add_argument("--file"); ap.add_argument("--no-fetch", action="store_true")
    ap.add_argument("--trigger-user"); ap.add_argument("--force", action="store_true")
    ap.add_argument("--fetch-only", action="store_true")
    a = ap.parse_args()
    if a.fetch_only:
        pth, mod = fetch_latest()
        FETCH_RESULT.write_text(json.dumps({"path": str(pth), "modified": mod}), encoding="utf-8")
        return
    who = TRIGGER_NAMES.get(a.trigger_user or "", a.trigger_user or "manual")

    if LOCK.exists() and time.time() - LOCK.stat().st_mtime < 900:
        say("already running - lock held")
        if a.trigger_user: slack(a.trigger_user, "The CICO email is already being generated - confirmation coming shortly.")
        return
    LOCK.write_text(str(os.getpid()))
    try:
        if a.file: FILE, mod = Path(a.file), None
        elif a.no_fetch: FILE, mod = latest_local(), None
        else: FILE, mod = fetch()
        b = build(FILE)
        key = f"{b['d_cur']:%Y-%m-%d}"
        subject = f"{b['d_cur']:%y%m%d} CICO - Daily balances"
        (TREAS / "cico_daily_email_preview.html").write_text(b["html"], encoding="utf-8")
        tot = f"${b['total']:,.0f}" if isinstance(b["total"], (int, float)) else "n/a"
        dlt = (f" ({'+' if b['total_delta'] >= 0 else '-'}${abs(b['total_delta']):,.0f} d/d)"
               if b["total_delta"] is not None else "")
        say("built", FILE.name, "date", key, "prev", f"{b['d_prev']:%Y-%m-%d}", "rows", b["rows"], "total", tot, dlt)
        if not a.send: return

        st = json.loads(STATE.read_text(encoding="utf-8")) if STATE.exists() else {"sent": {}}
        prior = st["sent"].get(key)
        if prior and not a.force and not a.to:
            msg = (f"'{subject}' already went out at {prior['at']} (triggered by {prior['by']}), and the latest "
                   f"filled date in {FILE.name} is still {b['d_cur']:%d-%b}. If today's balances aren't in the CICO yet, "
                   f"update it and message me again. To re-send the same date, message 'CICO resend'.")
            if a.trigger_user: slack(a.trigger_user, msg)
            say("skip - already sent", key); return
        to = a.to.split(",") if a.to else DIST
        mid = send_mail(b["html"], subject, to)
        if not a.to:
            st["sent"][key] = {"at": time.strftime("%Y-%m-%d %H:%M"), "by": who, "file": FILE.name,
                               "file_modified": mod, "msg_id": mid, "total": b["total"], "to": to}
            STATE.write_text(json.dumps(st, indent=1, default=str), encoding="utf-8")
        say("sent", subject, "to", len(to), "id", mid)
        moves = ("; $1M+ moves: " + ", ".join(b["big_moves"][:6])) if b["big_moves"] else ""
        summary = (f"Sent '{subject}' to {len(to)} recipients - balances as of {b['d_cur']:%d-%b-%y} "
                   f"from {FILE.name}. Total cash {tot}{dlt}{moves}.")
        if a.trigger_user and a.trigger_user != BRIAN_SLACK: slack(a.trigger_user, summary)
        slack(BRIAN_SLACK, summary + f" Triggered by {who}.")
    except Exception as e:
        say("ERROR", traceback.format_exc())
        err = f"CICO daily balances email FAILED ({type(e).__name__}: {str(e)[:200]}). Nothing was sent."
        if a.trigger_user and a.trigger_user != BRIAN_SLACK: slack(a.trigger_user, err + " Brian has been notified.")
        slack(BRIAN_SLACK, err + f" Triggered by {who}. Log: treasury\\cico_daily_email.log")
        sys.exit(1)
    finally:
        LOCK.unlink(missing_ok=True)


if __name__ == "__main__":
    main()
