#!/usr/bin/env python3
"""NB Monthly Reporting Cron — fires on the 14th of each month.

Scope (set by Brian 2026-09-14): the Neuberger Berman monthly reporting
requirements under the executed Credit Agreement, Article VI.

  * Section 6.01(c) — monthly unaudited consolidated financials, due 40 days
    after month-end (30 days from the month ending 2027-03-31). Jeeves Capital
    Markets does NOT produce these; Accounting does (Kacper Chrobot prepares,
    Shalom Hassan reviews, Alexander Melikian approves). Brian explicitly said
    "skip the financials" — this cron does not build them, it CHASES them: if
    the prior month's package has not arrived by the 14th, it drafts (never
    sends) a reminder email to Shalom / Alex / Kacper and DMs Brian the verbatim
    draft for approval.
  * Section 6.02(b)(y) — month-end Borrowing Base Certificate (Exhibit G),
    due 15 Business Days after month-end, with the month-end-only
    certifications: account opened/closed delta, Effective Advance Rate of each
    SPV Financing, Immaterial Subsidiary threshold test.
  * Section 6.02(f) — month-end cash report (Liquidity of the Loan Parties),
    delivered concurrently with the monthly BBC. E-mail delivery permitted.

The BBC itself is produced by report_scheduler_cron (NB Borrowing Base); this
cron only states the deadline so the month-end variant is never silently
skipped behind the weekly one.

Env vars:
  NB_MONTHLY_DAY  (default 14)  — day of month to fire
  NB_MONTHLY_HOUR (default 8)   — local hour to fire
"""

import json
import logging
import os
import sys
import time
import traceback
from datetime import date, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / '_shared'))
from env_loader import load_env
load_env()

logging.basicConfig(
    level=logging.INFO,
    format='[NBMONTHLY %(asctime)s] %(message)s',
    datefmt='%H:%M:%S',
)
log = logging.getLogger('nb_monthly_reporting')

FIRE_DAY = int(os.environ.get('NB_MONTHLY_DAY', '14'))
FIRE_HOUR = int(os.environ.get('NB_MONTHLY_HOUR', '8'))
CHECK_INTERVAL_SECS = 3600

BRIAN_EMAIL = 'brian.mauck@tryjeeves.com'
FINANCE_RECIPIENTS = [
    'shalom@tryjeeves.com',          # Shalom Hassan
    'alexander@tryjeeves.com',       # Alexander Melikian (CFO)
    'kacper.chrobot@tryjeeves.com',  # Kacper Chrobot (Corporate Controller)
]

# Section 6.01(c): 40 days after month-end, tightening to 30 days for the month
# ending 2027-03-31 and after.
TIGHTEN_FROM_MONTH_END = date(2027, 3, 31)


# ---------------------------------------------------------------- date helpers

def _nth_weekday(year, month, weekday, n):
    d = date(year, month, 1)
    d += timedelta(days=(weekday - d.weekday()) % 7)
    return d + timedelta(days=7 * (n - 1))


def _last_weekday(year, month, weekday):
    d = date(year, month + 1, 1) - timedelta(days=1) if month < 12 else date(year, 12, 31)
    return d - timedelta(days=(d.weekday() - weekday) % 7)


def _observed(d):
    """Federal observance shift: Sat -> prior Fri, Sun -> following Mon."""
    if d.weekday() == 5:
        return d - timedelta(days=1)
    if d.weekday() == 6:
        return d + timedelta(days=1)
    return d


def us_bank_holidays(year):
    """NY banking holidays — the 'Business Day' carve-out in the Credit Agreement
    ('a day on which banks in New York are authorized or required to close')."""
    fixed = [date(year, 1, 1), date(year, 6, 19), date(year, 7, 4),
             date(year, 11, 11), date(year, 12, 25)]
    out = {_observed(d) for d in fixed}
    out.add(_nth_weekday(year, 1, 0, 3))    # MLK Day
    out.add(_nth_weekday(year, 2, 0, 3))    # Presidents Day
    out.add(_last_weekday(year, 5, 0))      # Memorial Day
    out.add(_nth_weekday(year, 9, 0, 1))    # Labor Day
    out.add(_nth_weekday(year, 10, 0, 2))   # Columbus Day
    out.add(_nth_weekday(year, 11, 3, 4))   # Thanksgiving
    return out


def add_business_days(start, n):
    hol = us_bank_holidays(start.year) | us_bank_holidays(start.year + 1)
    d, count = start, 0
    while count < n:
        d += timedelta(days=1)
        if d.weekday() < 5 and d not in hol:
            count += 1
    return d


def prior_month_end(today):
    first = today.replace(day=1)
    return first - timedelta(days=1)


def financials_due(month_end):
    days = 30 if month_end >= TIGHTEN_FROM_MONTH_END else 40
    return month_end + timedelta(days=days), days


# ---------------------------------------------------------------- state

def _state_path():
    here = Path(__file__).resolve()
    return here.parents[3] / 'backend' / '.deer-flow' / '_nb_monthly_reporting_state.json'


def load_state():
    p = _state_path()
    if p.exists():
        try:
            with open(p) as f:
                return json.load(f)
        except Exception:
            log.warning("state file unreadable, starting fresh")
    return {'last_run': None, 'last_period': None, 'run_count': 0}


def save_state(state):
    p = _state_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, 'w') as f:
        json.dump(state, f, indent=2)


def monthly_run_due(last_run_iso, now):
    """Fire when the most recent scheduled fire time (this month's FIRE_DAY at
    FIRE_HOUR, else last month's) is later than the last successful run. Catches
    up after downtime instead of silently skipping a month."""
    due = now.replace(day=min(FIRE_DAY, 28), hour=FIRE_HOUR,
                      minute=0, second=0, microsecond=0)
    if due > now:
        prev_end = now.replace(day=1) - timedelta(days=1)
        due = due.replace(year=prev_end.year, month=prev_end.month)
    if not last_run_iso:
        return now.day == FIRE_DAY and now.hour >= FIRE_HOUR
    try:
        return datetime.fromisoformat(last_run_iso) < due
    except Exception:
        return now.day == FIRE_DAY and now.hour >= FIRE_HOUR


# ---------------------------------------------------------------- prompt

def _build_prompt(period_month_end=None, run_number=1):
    now = datetime.now()
    me = period_month_end or prior_month_end(now.date())
    period_label = me.strftime('%B %Y')
    fin_due, fin_days = financials_due(me)
    bbc_due = add_business_days(me, 15)

    return (
        "NB MONTHLY REPORTING CHECK #" + str(run_number) + " -- " + now.strftime('%A, %B %d %Y') + "\n"
        "Reporting period: " + period_label + " (month end " + me.isoformat() + ")\n\n"
        "You are DeerFlow-Analyst running Brian Mauck's monthly Neuberger Berman\n"
        "reporting check. Authority is the EXECUTED NB Credit Agreement, Article VI.\n"
        "Use LIVE Gmail only -- never stored memory -- to decide what has arrived.\n\n"
        "## Deadlines for this period (already computed, do not recompute)\n"
        "  * Sec 6.01(c) monthly financials: due " + fin_due.strftime('%A %Y-%m-%d')
        + "  (" + str(fin_days) + " days after month end)\n"
        "  * Sec 6.02(b)(y) month-end Borrowing Base Certificate: due "
        + bbc_due.strftime('%A %Y-%m-%d') + "  (15 Business Days after month end)\n"
        "  * Sec 6.02(f) month-end cash report (Liquidity): due with the BBC, same date\n\n"
        "## STEP 1 -- have the " + period_label + " financials arrived?\n"
        "Accounting owns them (Kacper Chrobot prepares, Shalom Hassan reviews,\n"
        "Alexander Melikian approves). Brian does NOT build these. Run these searches\n"
        "(gmail_tool.py caps at 10 results -- pass a count, and union the results):\n"
        "  python /mnt/skills/custom/gmail/gmail_tool.py search \"subject:financials newer_than:45d\" 20\n"
        "  python /mnt/skills/custom/gmail/gmail_tool.py search \"from:kacper.chrobot@tryjeeves.com newer_than:45d\" 20\n"
        "  python /mnt/skills/custom/gmail/gmail_tool.py search \"from:shalom@tryjeeves.com newer_than:45d\" 20\n"
        "RECEIVED means a message whose subject/body delivers the " + period_label + "\n"
        "monthly financial statements package (the recurring '<Month> <Year> ... Financials'\n"
        "thread). A message about an EARLIER month is NOT this period. Read the message,\n"
        "do not judge from the preview.\n\n"
        "## STEP 2A -- if RECEIVED\n"
        "Do NOT draft anything. DM Brian one line: period, who sent it, when, whether\n"
        "Alex has approved it yet, and the " + fin_due.strftime('%Y-%m-%d') + " NB deadline.\n\n"
        "## STEP 2B -- if NOT RECEIVED\n"
        "Create a Gmail DRAFT -- do NOT send it. Per Brian's standing pattern, thread the\n"
        "reminder onto the most recent monthly-financials email chain and advance the\n"
        "period rather than reopening the old ask:\n"
        "  python /mnt/skills/custom/gmail/gmail_tool.py draft <message_id> --body-file <path> "
        "--cc \"" + ', '.join(FINANCE_RECIPIENTS[1:]) + "\"\n"
        "(`draft` takes the body as argv[3]: --body-file MUST be argv[3], never an empty\n"
        "string first, or the draft ships blank.) Recipients: "
        + ', '.join(FINANCE_RECIPIENTS) + ".\n"
        "If no suitable thread exists, use draft-new with ALL recipients comma-separated\n"
        "in the single `to` arg (draft-new silently drops --cc).\n\n"
        "Body requirements -- short, Brian's voice, no hedging, no pleasantries beyond one\n"
        "line. State: (1) asking for the " + period_label + " monthly financial statements,\n"
        "(2) they are required under NB Credit Agreement Section 6.01(c) and are due to the\n"
        "Administrative Agent and Lenders by " + fin_due.strftime('%B %d, %Y') + ",\n"
        "(3) ask for a date if they are not ready. Do not promise anything to NB.\n\n"
        "## STEP 3 -- always\n"
        "DM Brian (Slack) a SHORT status, and if you created a draft include the VERBATIM\n"
        "draft body and the draft ID in that same message so he can approve or edit\n"
        "without opening anything. Then add one line on the month-end BBC + cash report\n"
        "due " + bbc_due.strftime('%Y-%m-%d') + " and whether the month-end variant has\n"
        "been produced. No progress narration, no restating these instructions.\n"
    )


# ---------------------------------------------------------------- run

def run_check(period_month_end=None):
    state = load_state()
    run_number = state.get('run_count', 0) + 1
    me = period_month_end or prior_month_end(datetime.now().date())
    prompt = _build_prompt(me, run_number)

    try:
        from autonomous_dispatch import dispatch
        dispatched = dispatch(
            prompt,
            notification=None,
            report_result=False,
            category="NB Monthly Reporting",
            source_id="nb-monthly-" + me.strftime('%Y%m'),
            source_metadata={"period": me.isoformat(), "run_number": run_number},
        )
    except Exception as e:
        log.error("dispatch failed: %s", e)
        traceback.print_exc()
        return False

    if not dispatched:
        log.warning("dispatch rejected (agent at capacity) -- will retry next tick")
        return False

    state['run_count'] = run_number
    state['last_run'] = datetime.now().isoformat()
    state['last_period'] = me.isoformat()
    save_state(state)
    log.info("NB monthly reporting check dispatched for %s", me.isoformat())
    return True


# ------------------------------------------------- executed-certificate watch
# Brian, 2026-09-22: "I should not have to ping you." When an officer's
# certificate is out for signature in Box, this cron polls Box hourly for the
# EXECUTED copy and, the moment it lands, dispatches the agent to rebuild the
# NB distribution draft. Nothing is ever sent automatically.

CERT_WATCH_FILE = '_nb_cert_watch.json'
CERT_PLACEHOLDER_MARKERS = ('[____', '[CONFIRM]', '[PENDIENTE]', '{{')


def _cert_watch_path():
    return _state_path().parent / CERT_WATCH_FILE


def load_cert_watch():
    p = _cert_watch_path()
    if p.exists():
        try:
            with open(p, encoding='utf-8') as f:
                return json.load(f)
        except Exception:
            log.warning("cert watch file unreadable, starting fresh")
    return {'watches': []}


def save_cert_watch(data):
    p = _cert_watch_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, 'w', encoding='utf-8') as f:
        json.dump(data, f, indent=2)


def _box_client():
    box_dir = Path(__file__).resolve().parent.parent / 'box'
    if str(box_dir) not in sys.path:
        sys.path.insert(0, str(box_dir))
    from box_client import get_client
    return get_client()


def _pdf_text(raw: bytes) -> str:
    try:
        import io
        from pypdf import PdfReader
        reader = PdfReader(io.BytesIO(raw))
        return "\n".join((pg.extract_text() or '') for pg in reader.pages)
    except Exception as e:
        log.warning("pdf text extraction failed: %s", e)
        return ''


def find_executed_cert(name_fragment, unsigned_size=None):
    """Look for an EXECUTED copy of a document in Box.

    Box Sign puts completed documents under 'My Signed Documents' (a per-request
    subfolder) and drops a '<name> Signing Log.pdf' beside the source in
    'My Sign Requests'. A file sitting only in 'My Sign Requests' is the
    UNSIGNED source we uploaded -- never treat it as executed (2026-09-22).

    Returns a dict or None. 'signed' is False when the file is found but still
    carries placeholder markers or a blank signature block.
    """
    client = _box_client()
    frag = name_fragment.lower()
    hits = []
    res = client.search.search_for_content(
        query=name_fragment, limit=50,
        fields=['name', 'size', 'modified_at', 'type', 'path_collection'])
    for e in getattr(res, 'entries', []) or []:
        if getattr(e, 'type', '') != 'file' or not e.name.lower().endswith('.pdf'):
            continue
        if frag not in e.name.lower():
            continue
        try:
            path = "/".join([p.name for p in e.path_collection.entries])
        except Exception:
            path = ''
        hits.append({'id': e.id, 'name': e.name, 'size': getattr(e, 'size', 0),
                     'path': path, 'modified_at': str(getattr(e, 'modified_at', ''))})

    signed_area = [h for h in hits if 'my signed documents' in h['path'].lower()
                   and 'signing log' not in h['name'].lower()]
    has_log = any('signing log' in h['name'].lower() for h in hits)
    if not signed_area:
        return None

    cand = sorted(signed_area, key=lambda h: h['modified_at'], reverse=True)[0]
    raw = client.downloads.download_file(cand['id']).read()
    text = _pdf_text(raw)
    placeholders = [m for m in CERT_PLACEHOLDER_MARKERS if m in text]
    cand['has_signing_log'] = has_log
    cand['placeholders'] = placeholders
    cand['same_size_as_unsigned'] = (unsigned_size is not None
                                     and cand['size'] == unsigned_size)
    cand['signed'] = bool(has_log) and not placeholders and not cand['same_size_as_unsigned']
    return cand


def _build_cert_prompt(watch, found):
    return (
        "The EXECUTED " + watch['label'] + " just landed in Box -- Brian does not want to be\n"
        "asked to ping you about it. Box file id " + found['id'] + " (" + found['name'] + ",\n"
        + str(found['size']) + " bytes, " + found['path'] + ").\n\n"
        "1. Download it with the box skill and re-read the signature block yourself. A file is\n"
        "   only executed if the signature line, Name, Title and Date are all populated and no\n"
        "   '[____' / '[CONFIRM]' placeholder remains. If it is NOT truly signed, STOP, build no\n"
        "   draft, and DM Brian one line saying what is still blank.\n"
        "2. If it IS executed, create a Gmail DRAFT (never send) to " + watch['recipients'] + "\n"
        "   with subject '" + watch['subject'] + "', attaching the executed certificate and\n"
        "   " + watch['companion'] + ". List the thread's existing drafts first and update or\n"
        "   delete-then-recreate rather than creating a second one.\n"
        "3. DM Brian the VERBATIM draft body, the draft ID, the recipient list and both\n"
        "   attachment filenames with byte sizes, in that one message. No progress narration.\n"
    )


def check_cert_watches():
    data = load_cert_watch()
    changed = False
    for w in data.get('watches', []):
        if w.get('status') != 'pending':
            continue
        try:
            found = find_executed_cert(w['name_fragment'], w.get('unsigned_size'))
        except Exception as e:
            log.warning("cert watch box lookup failed for %s: %s", w.get('label'), e)
            continue
        if not found:
            continue
        if not found.get('signed'):
            log.info("cert watch: %s found in signed area but not verified signed (%s)",
                     w.get('label'), found.get('placeholders'))
            continue
        try:
            from autonomous_dispatch import dispatch
            ok = dispatch(
                _build_cert_prompt(w, found),
                notification=None,
                report_result=False,
                category="NB Executed Certificate",
                source_id="nb-cert-" + str(found['id']),
                source_metadata={'label': w.get('label'), 'box_file_id': found['id']},
            )
        except Exception as e:
            log.error("cert watch dispatch failed: %s", e)
            continue
        if ok:
            w['status'] = 'dispatched'
            w['found'] = found
            w['dispatched_at'] = datetime.now().isoformat()
            changed = True
            log.info("cert watch: dispatched draft rebuild for %s", w.get('label'))
    if changed:
        save_cert_watch(data)


def run_loop():
    log.info("NB Monthly Reporting cron started. Fires day %d at %02d:00.", FIRE_DAY, FIRE_HOUR)
    while True:
        try:
            check_cert_watches()
        except Exception as e:
            log.error("cert watch error: %s", e)
            traceback.print_exc()
        try:
            if monthly_run_due(load_state().get('last_run'), datetime.now()):
                run_check()
        except Exception as e:
            log.error("loop error: %s", e)
            traceback.print_exc()
        time.sleep(CHECK_INTERVAL_SECS)


if __name__ == '__main__':
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    if hasattr(sys.stderr, 'reconfigure'):
        sys.stderr.reconfigure(encoding='utf-8', errors='replace')

    import argparse
    parser = argparse.ArgumentParser(description='NB Monthly Reporting Cron')
    parser.add_argument('mode', nargs='?', choices=['once', 'show-prompt', 'dates'])
    parser.add_argument('--period', help='month end YYYY-MM-DD (default: prior month end)')
    args = parser.parse_args()

    pme = date.fromisoformat(args.period) if args.period else None

    if args.mode == 'show-prompt':
        print(_build_prompt(pme, load_state().get('run_count', 0) + 1))
    elif args.mode == 'dates':
        m = pme or prior_month_end(datetime.now().date())
        fd, fdays = financials_due(m)
        print('period month end :', m.isoformat())
        print('financials due   :', fd.strftime('%a %Y-%m-%d'), '(%d days)' % fdays)
        print('BBC + cash due   :', add_business_days(m, 15).strftime('%a %Y-%m-%d'), '(15 BD)')
    elif args.mode == 'once':
        run_check(pme)
    else:
        run_loop()
