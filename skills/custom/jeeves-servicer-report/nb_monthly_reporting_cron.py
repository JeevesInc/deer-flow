#!/usr/bin/env python3
"""NB Monthly Reporting Cron — the full NB monthly package, ahead of every due date.

2026-09-23 (Brian: "I want all of it done before the requirement"): besides the
14th financials chase, run_schedule() builds the 6.02(b)(y)/6.02(f) month-end BBC
at BD+3, builds the 6.01(c) package the day Accounting delivers, escalates at
due-10, and DMs Brian from due-3BD if Sent does not show a deliverable. Drafts only.

Original scope — fires on the 14th of each month.

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


# =====================================================================
# Full-package schedule (Brian, 2026-09-23: "I want all of it done before
# the requirement"). Every NB monthly deliverable now has a TARGET well
# inside its contractual due date, and a deterministic guard that DMs Brian
# if Gmail in:sent does not show it going out in time. Nothing is ever sent
# automatically -- every lender-facing artifact is a Gmail DRAFT.
#
#   Milestone                     Target                     Contract due
#   ---------------------------   ------------------------   -----------------
#   6.02(b)(y) month-end BBC      BD+3 after month end       BD+15
#   + 6.02(f) cash report         (same draft)               with the BBC
#   6.01(c) financials chase #1   the 14th (existing)        ME+40 (30 from 3/27)
#   6.01(c) package build         the day Accounting's       ME+40
#                                 package lands (daily probe)
#   6.01(c) chase #2 / escalate   due - 10 days              ME+40
#   Deadline guard (both)         from due - 3 BD, daily     --
# =====================================================================

ME_BBC_BD = int(os.environ.get('NB_ME_BBC_BD', '3'))
ME_BBC_HOUR = int(os.environ.get('NB_ME_BBC_HOUR', '9'))
MAX_ATTEMPTS = 3
GUARD_BD = 3
ESCALATE_DAYS_BEFORE = 10
ACCOUNTING_SENDERS = ('kacper.chrobot@tryjeeves.com', 'shalom@tryjeeves.com',
                      'alexander@tryjeeves.com')


def _dm(text):
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent / '_shared'))
        from notify import dm_owner
        dm_owner(text)
    except Exception as e:
        log.error("owner DM failed: %s", e)


def _gmail():
    gdir = str(Path(__file__).resolve().parent.parent / 'gmail')
    if gdir not in sys.path:
        sys.path.insert(0, gdir)
    import gmail_tool
    return gmail_tool._get_service()


def _gmail_ids(query, n=10):
    res = _gmail().users().messages().list(userId='me', q=query, maxResults=n).execute()
    return [m['id'] for m in res.get('messages', [])]


def _long_date(d):
    return d.strftime('%B ') + str(d.day) + d.strftime(', %Y')


def me_bbc_subject(me):
    return 'Jeeves Monthly Borrowing Base Certificate - As of ' + _long_date(me)


def fin_subject(me):
    return 'Jeeves, Inc. - Monthly Financial Statements - ' + me.strftime('%B %Y') + ' (Section 6.01(c))'


def sent_to_lenders(subject_fragment):
    """True if Brian's Sent folder shows the deliverable going to NB/WT."""
    q = ('in:sent (to:nb.com OR to:wilmingtontrust.com) subject:"' + subject_fragment
         + '" newer_than:120d')
    return bool(_gmail_ids(q, 3))


def last_business_day_on_or_before(d):
    hol = us_bank_holidays(d.year)
    while d.weekday() >= 5 or d in hol:
        d -= timedelta(days=1)
    return d


def find_month_end_cico(me):
    """Mariana's '<YYMMDD> CICO- Daily balances' for the last business day of
    the month. Returns a Gmail message id or None (never falls back to the
    latest file -- a month-end certificate must carry month-end balances)."""
    bd = last_business_day_on_or_before(me)
    stamp = bd.strftime('%y%m%d')
    ids = _gmail_ids('subject:"' + stamp + ' CICO" newer_than:180d', 5)
    return (ids[0] if ids else None), bd


def _pstate(state, me):
    return state.setdefault('periods', {}).setdefault(me.strftime('%Y%m'), {})


# ------------------------------------------------ 6.02(b)(y) / 6.02(f)

def _build_me_bbc_prompt(me, res):
    bbc_due = add_business_days(me, 15)
    cov = res.get('cov_res') or {}
    cov_lines = "\n".join('    - ' + t['name'] + ': ' + t['detail'] for t in cov.get('tests', []))
    return (
        "NB MONTH-END BORROWING BASE CERTIFICATE -- " + me.strftime('%B %Y') + "\n\n"
        "The month-end NB borrowing base as of " + me.isoformat() + " was built and PASSED the\n"
        "fail-closed covenant gate (nb_covenant_check.py):\n" + cov_lines + "\n"
        "Workbook: " + str(res.get('final_wb')) + "\nDrive: " + str(res.get('drive_link')) + "\n\n"
        "This is the Section 6.02(b)(y) month-end Borrowing Base Certificate (Exhibit G) plus the\n"
        "Section 6.02(f) month-end cash report (Liquidity of the Loan Parties). Contract due date:\n"
        + bbc_due.strftime('%A %Y-%m-%d') + " (15 Business Days). Load the jeeves-servicer-report\n"
        "and jeeves-borrowing-base skills first; authority is the EXECUTED NB Credit Agreement.\n\n"
        "1. Build the Exhibit G certificate (all six 6.02(b) certifications) with the\n"
        "   deterministic module -- do NOT hand-derive any figure:\n"
        "     python /mnt/skills/custom/jeeves-borrowing-base/nb_certifications.py --workbook \"" + str(res.get('final_wb')) + "\" --as-of " + me.isoformat() + " --variant month_end\n"
        "   It writes 'Certificate - NB Monthly Borrowing Base Certificate - YYYYMMDD.docx' with\n"
        "   Schedules 1-3 (7.01), Exhibit A (org docs), Exhibit B (accounts vs prior month-end\n"
        "   snapshot / Schedule 5.22), Exhibit C (Effective Advance Rate from the SOFOM master +\n"
        "   historical_draws) and item 9 (Immaterial Subsidiary test from Accounting inputs in\n"
        "   .deer-flow/nb_cert_inputs.json). Upload it to the same NB Reporting month folder.\n"
        "   If it prints PENDING items: create NO lender draft and NO Box upload; DM Brian the\n"
        "   pending list verbatim and STOP. Officer items (ii), (iii), Exhibit A and (vi) are\n"
        "   certified by Alexander Melikian signing -- never ask Accounting for them (Brian 2026-09-23).\n"
        "2. Read Exhibit G. If it requires a Responsible Officer signature, prepare the signature\n"
        "   page for Alexander Melikian, upload to Box, and register a 'pending' entry in\n"
        "   .deer-flow/_nb_cert_watch.json (label, name_fragment, unsigned_size, subject '"
        + me_bbc_subject(me) + "', recipients from nb_bb_distribution.json, companion = the\n"
        "   workbook) so the executed-certificate watcher builds the draft -- no draft until signed.\n"
        "   Otherwise create the Gmail DRAFT now (never send): To/Cc from\n"
        "   .deer-flow/nb_bb_distribution.json, NEW thread, subject '" + me_bbc_subject(me) + "',\n"
        "   workbook attached, body in Brian's voice ('Hi all-', short prose, no bullets) stating\n"
        "   it is delivered pursuant to Sections 6.02(b)(y) and 6.02(f) and giving the three\n"
        "   certifications and month-end Liquidity.\n"
        "3. If ANY certification cannot be sourced, create NO draft and DM Brian one line naming\n"
        "   what is missing. Never write a placeholder into a lender-facing file or body.\n"
        "4. DM Brian: verbatim draft body, draft ID, recipients, attachment names + byte sizes,\n"
        "   and the headline numbers (eligible, BB, Liquidity vs $15MM floor, DQ30). No narration.\n"
    )


def run_month_end_bbc(me, force=False):
    state = load_state()
    ps = _pstate(state, me).setdefault('me_bbc', {'status': 'pending', 'attempts': 0})
    if ps.get('status') in ('handed_off', 'withheld') and not force:
        return True
    ps['attempts'] = ps.get('attempts', 0) + 1
    ps['last_attempt'] = datetime.now().isoformat()
    save_state(state)

    try:
        cico_id, cico_bd = find_month_end_cico(me)
    except Exception as e:
        cico_id, cico_bd = None, me
        log.error("month-end CICO lookup failed: %s", e)
    if not cico_id:
        log.warning("no month-end CICO email for %s yet", cico_bd)
        if ps['attempts'] >= MAX_ATTEMPTS:
            _dm(":warning: *NB month-end BBC " + me.strftime('%b %Y') + "* not built: no '"
                + cico_bd.strftime('%y%m%d') + " CICO- Daily balances' email found after "
                + str(ps['attempts']) + " tries. Due " + add_business_days(me, 15).isoformat() + ".")
            ps['status'] = 'failed'
            save_state(state)
        return False

    bb_dir = str(Path(__file__).resolve().parent.parent / 'jeeves-borrowing-base')
    if bb_dir not in sys.path:
        sys.path.insert(0, bb_dir)
    try:
        import report_scheduler_cron as rsc
        res = rsc.run_nb_bb(as_of=me.isoformat(), variant='month_end', cico_msg=cico_id)
    except Exception as e:
        log.error("month-end build failed: %s", e)
        if ps['attempts'] >= MAX_ATTEMPTS:
            ps['status'] = 'failed'
        save_state(state)
        return False

    state = load_state()
    ps = _pstate(state, me)['me_bbc']
    ps['final_wb'] = res.get('final_wb')
    ps['drive_link'] = res.get('drive_link')
    if not res.get('gate_ok'):
        # Covenant failure or unevaluable check: run_nb_bb already flagged it on
        # Slack. Rule of record 2026-09-16 -- no outbound artifact of any kind.
        ps['status'] = 'withheld'
        save_state(state)
        return True
    try:
        from autonomous_dispatch import dispatch
        ok = dispatch(_build_me_bbc_prompt(me, res), notification=None, report_result=False,
                      category="NB Month-End BBC", source_id="nb-me-bbc-" + me.strftime('%Y%m'),
                      source_metadata={'period': me.isoformat()})
    except Exception as e:
        log.error("month-end dispatch failed: %s", e)
        ok = False
    ps['status'] = 'handed_off' if ok else 'built_not_dispatched'
    save_state(state)
    return bool(ok)


# ------------------------------------------------ 6.01(c) package

def financials_received(me):
    """Cheap probe: a message from Accounting naming the period + financials."""
    senders = ' OR '.join('from:' + s for s in ACCOUNTING_SENDERS)
    q = '(' + senders + ') "' + me.strftime('%B %Y') + '" financials newer_than:60d'
    ids = _gmail_ids(q, 5)
    return ids[0] if ids else None


def _build_fin_package_prompt(me, msg_id):
    fin_due, fin_days = financials_due(me)
    return (
        "NB SECTION 6.01(c) PACKAGE -- " + me.strftime('%B %Y') + "\n\n"
        "Accounting's " + me.strftime('%B %Y') + " financials appear to have landed (Gmail " + msg_id + ").\n"
        "Contract due date " + fin_due.strftime('%A %Y-%m-%d') + " (" + str(fin_days) + " days). Load\n"
        "jeeves-servicer-report (read the 6.01(c) HARD RULES) plus gmail, box, google-drive.\n\n"
        "1. Read the thread yourself. If it is NOT the " + me.strftime('%B %Y') + " package (earlier\n"
        "   month, a question, a draft), STOP and DM Brian nothing. If Alex has not approved it yet,\n"
        "   still build steps 2-3 so it is ready the moment he does.\n"
        "2. Download the attachments and build 'Financials - NB Monthly " + me.strftime('%B %Y')
        + " - " + me.strftime('%Y%m%d') + ".xlsx'\n"
        "   cloned from the July 2026 one ('Financials - NB Monthly July 2026 - 20260731.xlsx').\n"
        "   Upload to Drive (Debt/Neuberger Berman/Reporting/" + me.strftime('%Y%m') + "/, --new).\n"
        "3. Build the one-paragraph Responsible Officer certification for Alexander Melikian (CFO),\n"
        "   cloned from 'NB Monthly Financials Officer Certification July 2026', named\n"
        "   'NB Monthly Financials Officer Certification " + me.strftime('%B %Y') + "'. No\n"
        "   placeholders except the signature block. Upload the PDF to Box.\n"
        "4. Try box create_sign_request ONCE (signer alexander@tryjeeves.com). If it errors (the\n"
        "   app lacks the Sign scope), do not retry -- the ask to Brian is to start Box Sign for\n"
        "   Alex from the Box UI on that file.\n"
        "5. Append a 'pending' watch to .deer-flow/_nb_cert_watch.json: label, name_fragment (the\n"
        "   cert name), unsigned_size (bytes of the uploaded PDF), subject '" + fin_subject(me) + "',\n"
        "   recipients = To list in nb_bb_distribution.json, companion = the step-2 workbook. The\n"
        "   hourly watcher then drafts the lender email once the signed copy lands. Build NO\n"
        "   lender draft yourself now.\n"
        "6. DM Brian ONE message: Drive link, Box file link, sign-request status, and the single\n"
        "   ask if any. No narration.\n"
    )


def _build_escalation_prompt(me):
    fin_due, _ = financials_due(me)
    return (
        "NB 6.01(c) ESCALATION -- " + me.strftime('%B %Y') + " financials still not received,\n"
        "due to NB " + fin_due.strftime('%A %B %d, %Y') + " (" + str((fin_due - date.today()).days)
        + " days). Confirm on live Gmail (from Kacper/Shalom/Alex, the period named). If truly not\n"
        "received, create a Gmail DRAFT (never send) replying on the latest monthly-financials\n"
        "thread to " + ', '.join(FINANCE_RECIPIENTS) + ": 1-3 sentences, 'Hi Kacper-', ask for the\n"
        + me.strftime('%B %Y') + " package and Alex's sign-off by a date 5 business days before the\n"
        "NB deadline. DM Brian the verbatim body + draft ID. If it HAS arrived, instead run the\n"
        "6.01(c) package build (financials workbook, officer certification, Box, cert watch).\n"
    )


def run_fin_milestones(me, now):
    state = load_state()
    ps = _pstate(state, me)
    fin_due, _ = financials_due(me)
    if ps.get('fin_package') or now.date() > fin_due:
        return
    if now.date() < me + timedelta(days=5):
        return
    # daily probe for arrival
    if ps.get('fin_probe_date') != now.date().isoformat():
        ps['fin_probe_date'] = now.date().isoformat()
        save_state(state)
        try:
            msg_id = financials_received(me)
        except Exception as e:
            log.error("financials probe failed: %s", e)
            msg_id = None
        if msg_id:
            try:
                from autonomous_dispatch import dispatch
                ok = dispatch(_build_fin_package_prompt(me, msg_id), notification=None,
                              report_result=False, category="NB 6.01(c) Package",
                              source_id="nb-fin-pkg-" + me.strftime('%Y%m'),
                              source_metadata={'period': me.isoformat(), 'msg': msg_id})
            except Exception as e:
                log.error("package dispatch failed: %s", e)
                ok = False
            if ok:
                state = load_state()
                _pstate(state, me)['fin_package'] = {'msg': msg_id,
                                                     'dispatched_at': now.isoformat()}
                save_state(state)
            return
    # escalation at due - 10 days
    if (not ps.get('fin_escalated')) and now.date() >= fin_due - timedelta(days=ESCALATE_DAYS_BEFORE) \
            and now.hour >= FIRE_HOUR:
        try:
            from autonomous_dispatch import dispatch
            ok = dispatch(_build_escalation_prompt(me), notification=None, report_result=False,
                          category="NB 6.01(c) Escalation",
                          source_id="nb-fin-esc-" + me.strftime('%Y%m'),
                          source_metadata={'period': me.isoformat()})
        except Exception as e:
            log.error("escalation dispatch failed: %s", e)
            ok = False
        if ok:
            state = load_state()
            _pstate(state, me)['fin_escalated'] = now.isoformat()
            save_state(state)


# ------------------------------------------------ deadline guard

def run_deadline_guard(me, now):
    if now.hour < FIRE_HOUR:
        return
    state = load_state()
    ps = _pstate(state, me)
    if ps.get('guard_date') == now.date().isoformat():
        return
    items = [('6.02(b)(y) month-end BBC + 6.02(f) cash report', add_business_days(me, 15),
              'Monthly Borrowing Base Certificate - As of ' + _long_date(me)),
             ('6.01(c) monthly financials', financials_due(me)[0],
              'Monthly Financial Statements - ' + me.strftime('%B %Y'))]
    lines = []
    for label, due, frag in items:
        warn_from = due
        for _ in range(GUARD_BD):
            warn_from -= timedelta(days=1)
            while warn_from.weekday() >= 5:
                warn_from -= timedelta(days=1)
        if now.date() < warn_from:
            continue
        if now.date() > due + timedelta(days=30):
            continue
        try:
            if sent_to_lenders(frag):
                continue
        except Exception as e:
            lines.append(label + ': could not check Sent (' + str(e)[:80] + ')')
            continue
        tag = 'OVERDUE' if now.date() > due else 'due'
        lines.append(label + ' -- ' + tag + ' ' + due.strftime('%a %b %d') + ', not in Sent')
    ps['guard_date'] = now.date().isoformat()
    save_state(state)
    if lines:
        _dm(":alarm_clock: *NB " + me.strftime('%B %Y') + " reporting*\n" + "\n".join(lines))


def run_schedule(now=None):
    now = now or datetime.now()
    me = prior_month_end(now.date())
    # 6.02(b)(y) month-end BBC at BD+3
    state = load_state()
    ps = _pstate(state, me).get('me_bbc', {})
    if ps.get('status') not in ('handed_off', 'withheld', 'failed') \
            and now.date() >= add_business_days(me, ME_BBC_BD) and now.hour >= ME_BBC_HOUR \
            and now.date() <= add_business_days(me, 15) \
            and ps.get('attempts', 0) < MAX_ATTEMPTS \
            and ps.get('last_attempt', '')[:13] != now.isoformat()[:13]:
        run_month_end_bbc(me)
    # 6.01(c): chase #1 on the 14th stays in run_check(); package + escalation here.
    # The financials window reaches back two periods (40-day due date spans months).
    for m in (me, prior_month_end(me)):
        run_fin_milestones(m, now)
        run_deadline_guard(m, now)


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
        try:
            run_schedule()
        except Exception as e:
            log.error("schedule error: %s", e)
            traceback.print_exc()
        time.sleep(CHECK_INTERVAL_SECS)


if __name__ == '__main__':
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    if hasattr(sys.stderr, 'reconfigure'):
        sys.stderr.reconfigure(encoding='utf-8', errors='replace')

    import argparse
    parser = argparse.ArgumentParser(description='NB Monthly Reporting Cron')
    parser.add_argument('mode', nargs='?', choices=['once', 'show-prompt', 'dates', 'month-end-bbc', 'guard', 'status'])
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
        print('BBC target       :', add_business_days(m, ME_BBC_BD).strftime('%a %Y-%m-%d'), '(BD+%d)' % ME_BBC_BD)
        print('fin escalation   :', (fd - timedelta(days=ESCALATE_DAYS_BEFORE)).strftime('%a %Y-%m-%d'))
    elif args.mode == 'month-end-bbc':
        run_month_end_bbc(pme or prior_month_end(datetime.now().date()), force=True)
    elif args.mode == 'guard':
        m = pme or prior_month_end(datetime.now().date())
        st = load_state(); _pstate(st, m).pop('guard_date', None); save_state(st)
        run_deadline_guard(m, datetime.now())
    elif args.mode == 'status':
        print(json.dumps(load_state(), indent=2))
    elif args.mode == 'once':
        run_check(pme)
    else:
        run_loop()
