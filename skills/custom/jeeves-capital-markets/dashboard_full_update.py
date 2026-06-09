#!/usr/bin/env python3
"""
dashboard_full_update.py
========================
Pulls real data from every available source and injects it into the
Capital Markets Dashboard HTML.

Sources:
  1. Latest US Bridge BB (Drive)            → KPIs, US BB waterfall, concentrations
  2. Latest MX SOFOM BB (Drive)             → MX BB waterfall
  3. Latest CICO email (Gmail)              → CICO cash section   [delegates to cico_dashboard_update.py]
  4. Redshift loc_tape                      → DPD buckets, geo, originations, product mix
  5. .deer-flow state files                 → Ops pulse
"""

import os, sys, json, re, subprocess, traceback
from datetime import date, datetime
from pathlib import Path

sys.stdout.reconfigure(encoding='utf-8', errors='replace')
sys.stderr.reconfigure(encoding='utf-8', errors='replace')

SKILLS = os.environ.get('SKILLS_PATH', '/mnt/skills')
OUTPUTS = os.environ.get('OUTPUTS_PATH', '/mnt/user-data/outputs')
WORKSPACE = os.environ.get('WORKSPACE_PATH', '/mnt/user-data/workspace')

sys.path.insert(0, os.path.join(SKILLS, 'custom', 'google-drive'))
sys.path.insert(0, os.path.join(SKILLS, 'custom', 'jeeves-borrowing-base'))

from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from googleapiclient.discovery import build

TODAY_STR = date.today().strftime('%b %-d, %Y') if sys.platform != 'win32' else date.today().strftime('%b %d, %Y').replace(' 0', ' ')
TODAY_ISO = date.today().isoformat()

# ─────────────────────────────────────────────────────────────
# 1. DRIVE AUTH
# ─────────────────────────────────────────────────────────────
def get_drive_service():
    creds = Credentials(
        token=None,
        refresh_token=os.environ['GOOGLE_REFRESH_TOKEN'],
        client_id=os.environ['GOOGLE_CLIENT_ID'],
        client_secret=os.environ['GOOGLE_CLIENT_SECRET'],
        token_uri='https://oauth2.googleapis.com/token',
    )
    creds.refresh(Request())
    return build('drive', 'v3', credentials=creds, cache_discovery=False)


def search_latest_bb(svc, name_fragment):
    res = svc.files().list(
        q=f"name contains '{name_fragment}' and mimeType='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet' and trashed=false",
        orderBy='modifiedTime desc',
        pageSize=1,
        fields='files(id,name,modifiedTime)'
    ).execute()
    files = res.get('files', [])
    return files[0] if files else None

# ─────────────────────────────────────────────────────────────
# 2. PARSE BB FILES
# ─────────────────────────────────────────────────────────────
def fetch_bb_text(file_id):
    """Fetch BB sheet text via fetch_doc.py subprocess."""
    result = subprocess.run(
        [sys.executable, os.path.join(SKILLS, 'custom', 'google-drive', 'fetch_doc.py'), file_id],
        capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=120
    )
    return result.stdout

def extract_num(text, label, default=None):
    """Extract first numeric value after a label in BB text."""
    pattern = re.escape(label) + r'[^0-9\-]*(-?[\d,]+\.?\d*)'
    m = re.search(pattern, text)
    if m:
        val = m.group(1).replace(',', '').strip()
        if val in ('', '-'):
            return default
        try:
            return float(val)
        except ValueError:
            return default
    return default

def parse_us_bridge_bb(text):
    """Parse key figures from Bridge BB Summary sheet."""
    data = {}
    data['gross_receivables'] = extract_num(text, ',Total Receivables,', 0)
    data['ineligibles'] = extract_num(text, ',Ineligible Receivables,', 0)
    data['eligible'] = extract_num(text, ',Total Eligible Receivables,', 0)
    data['receivables_counted'] = extract_num(text, ',Total Receivables Counted Towards Borrowing Base,', 0)
    data['us_cash'] = extract_num(text, ',Total US Cash Counted Towards Borrowing Base,', 0)
    data['ex_us_cash'] = extract_num(text, ',Total ex-US Cash Counted Towards Borrowing Base,', 0)
    data['total_collateral'] = extract_num(text, ',Total Collateral Counted Towards Borrowing Base,', 0)
    data['mastercard'] = extract_num(text, ',Mastercard Liability Amount,', 0)
    data['adv_rate_receivables'] = extract_num(text, ',Receivable Advance Rate,', 0.85)
    data['adv_rate_us_cash'] = extract_num(text, ',US Cash Advance Rate,', 1.0)
    data['adv_rate_ex_cash'] = extract_num(text, ',ex-US Cash Advance Rate,', 0.95)
    data['borrowing_base'] = extract_num(text, ',Borrowing Base,', 0)
    data['total_drawn'] = extract_num(text, ',Total Drawn,', 0)
    data['availability'] = extract_num(text, ',Borrowing Base Excess / Deficit,', 0)
    data['facility_size'] = extract_num(text, ',Facility Size,', 50_000_000)

    # Concentration tests — parse the table rows
    concentrations = []
    lines = text.split('\n')
    for line in lines:
        m = re.search(r',([\d,]*\.?\d*),([\d.]+),([\d.]+),([\d,]*\.?\d*),([\d,]*\.?\d*)', line)
        if m:
            try:
                actual_usd = float(m.group(1).replace(',','')) if m.group(1) else 0
                actual_pct = float(m.group(2))
                limit_pct = float(m.group(3))
                excess = float(m.group(5).replace(',','')) if m.group(5) else 0
            except ValueError:
                continue
            if 0 < actual_pct < 1 and 0 < limit_pct <= 1:
                label = line.split(',')[1].strip() if line.startswith(',') else ''
                label = re.sub(r'^[0-9]+\.\s*', '', label)[:80]
                if label and len(label) > 5:
                    concentrations.append({
                        'label': label,
                        'actual_pct': round(actual_pct * 100, 2),
                        'limit_pct': round(limit_pct * 100, 1),
                        'excess': excess,
                        'pass': excess == 0
                    })
    data['concentrations'] = concentrations[:11]  # up to 11 tests
    return data

def parse_sofom_bb(text):
    """Parse MX SOFOM BB — mostly template, grab what's populated."""
    data = {}
    data['facility_size'] = extract_num(text, ',Facility Size,', 100_000_000)
    data['total_receivables'] = extract_num(text, ',Total Receivables,', 0)
    # Collection account cash (Prerecycling)
    m = re.search(r'Prerecycling,,([\d,]+\.?\d*)', text)
    data['collection_cash_mxn'] = float(m.group(1).replace(',','')) if m else 0
    # Swap contract
    m = re.search(r'Recycling -1,(-?[\d,]+\.?\d*)', text)
    data['swap_mxn'] = float(m.group(1).replace(',','')) if m else 0
    data['usdmxn'] = extract_num(text, ',Spot Exchange Rate  USDMXN,', 17.31)
    return data

# ─────────────────────────────────────────────────────────────
# 3. REDSHIFT — portfolio strats
# ─────────────────────────────────────────────────────────────
def pull_redshift_data():
    """Pull DPD, geo, originations from Redshift. Returns None if offline."""
    try:
        from redshift_util import connect
        conn = connect()
        cur = conn.cursor()

        # Max date
        cur.execute("SELECT MAX(dt) FROM capital_markets_dm.loc_tape WHERE balance_usd > 0")
        max_dt = str(cur.fetchone()[0])

        # DPD buckets
        cur.execute(f"""
        SELECT COUNT(*) as accts,
               SUM(balance_usd) as total,
               SUM(CASE WHEN days_past_due = 0 THEN balance_usd ELSE 0 END) as curr,
               SUM(CASE WHEN days_past_due BETWEEN 1 AND 30 THEN balance_usd ELSE 0 END) as d1_30,
               SUM(CASE WHEN days_past_due BETWEEN 31 AND 60 THEN balance_usd ELSE 0 END) as d31_60,
               SUM(CASE WHEN days_past_due BETWEEN 61 AND 90 THEN balance_usd ELSE 0 END) as d61_90,
               SUM(CASE WHEN days_past_due > 90 THEN balance_usd ELSE 0 END) as d90p
        FROM capital_markets_dm.loc_tape
        WHERE dt = '{max_dt}' AND balance_usd > 0
        """)
        r = cur.fetchone()
        total = float(r[1]) if r[1] else 1
        dpd = {
            'max_dt': max_dt, 'accounts': int(r[0]), 'total': float(r[1] or 0),
            'current': float(r[2] or 0), 'd1_30': float(r[3] or 0),
            'd31_60': float(r[4] or 0), 'd61_90': float(r[5] or 0), 'd90p': float(r[6] or 0),
            'total_pct': 100,
            'current_pct': round(float(r[2] or 0)/total*100, 1),
            'd1_30_pct': round(float(r[3] or 0)/total*100, 1),
            'd31_60_pct': round(float(r[4] or 0)/total*100, 1),
            'd61_90_pct': round(float(r[5] or 0)/total*100, 1),
            'd90p_pct': round(float(r[6] or 0)/total*100, 1),
        }
        dpd['dq30_pct'] = round((dpd['d1_30'] + dpd['d31_60'] + dpd['d61_90'] + dpd['d90p'])/total*100, 1)

        # Geo
        COUNTRY_NAMES = {484: 'Mexico', 170: 'Colombia', 76: 'Brazil',
                         840: 'United States', 124: 'Canada', 32: 'Argentina'}
        cur.execute(f"""
        SELECT country_id, COUNT(DISTINCT company_id), SUM(balance_usd)
        FROM capital_markets_dm.loc_tape
        WHERE dt = '{max_dt}' AND balance_usd > 0
        GROUP BY 1 ORDER BY 3 DESC
        """)
        rows = cur.fetchall()
        geo_total = sum(float(r[2] or 0) for r in rows)
        geo = [{'name': COUNTRY_NAMES.get(int(r[0] or 0), f'Other'), 
                'accounts': int(r[1]), 'balance': float(r[2] or 0),
                'pct': round(float(r[2] or 0)/geo_total*100, 1)} for r in rows]

        # Monthly originations (last 6 months)
        cur.execute(f"""
        SELECT DATE_TRUNC('month', dt) as mo, SUM(disbursement_amount_usd) as orig
        FROM capital_markets_dm.loc_tape
        WHERE dt >= DATEADD('month', -6, '{max_dt}') AND disbursement_amount_usd > 0
        GROUP BY 1 ORDER BY 1
        """)
        monthly_orig = [{'month': str(r[0])[:7], 'orig': float(r[1] or 0)} for r in cur.fetchall()]

        # Product mix
        cur.execute(f"""
        SELECT SUM(balance_usd) as total,
               SUM(CASE WHEN jeeves_pay_balance_usd > 0 THEN balance_usd ELSE 0 END) as jp
        FROM capital_markets_dm.loc_tape
        WHERE dt = '{max_dt}' AND balance_usd > 0
        """)
        r = cur.fetchone()
        bal_total = float(r[0] or 0)
        jp_bal = float(r[1] or 0)
        loc_bal = bal_total - jp_bal
        product_mix = {
            'total': bal_total, 'loc': loc_bal, 'jp': jp_bal,
            'loc_pct': round(loc_bal/bal_total*100, 1) if bal_total else 0,
            'jp_pct': round(jp_bal/bal_total*100, 1) if bal_total else 0,
        }

        conn.close()
        return {'dpd': dpd, 'geo': geo, 'monthly_orig': monthly_orig, 'product_mix': product_mix, 'max_dt': max_dt}
    except Exception as e:
        print(f"Redshift offline: {e}", file=sys.stderr)
        return None

# ─────────────────────────────────────────────────────────────
# 4. STATE FILES → Ops pulse
# ─────────────────────────────────────────────────────────────
def load_state_files():
    base = Path('C:/Jeeves/redshift-bot/deer-flow/backend/.deer-flow')
    states = {}
    for f in ['_analytics_cron_state', '_revenue_comp_state', '_report_scheduler_state',
              '_dreams_state', '_eod_review_state']:
        p = base / f'{f}.json'
        if p.exists():
            try:
                states[f.lstrip('_')] = json.loads(p.read_text())
            except Exception:
                pass
    return states

def fmt_state_date(val):
    """Format a state date string for display."""
    if not val:
        return 'Never'
    val = str(val)
    if val.endswith('_running'):
        return 'Running...'
    try:
        dt = datetime.fromisoformat(val)
        today = date.today()
        has_time = 'T' in val or (' ' in val and ':' in val.split(' ', 1)[-1])
        if dt.date() == today:
            if has_time:
                h = dt.strftime('%I:%M %p').lstrip('0') or '12:00 AM'
                return f'Today {h}'
            return 'Today'
        if has_time:
            return dt.strftime('%b %d %I:%M %p').replace(' 0', ' ').lstrip('0') or val[:10]
        return dt.strftime('%b %d').replace(' 0', ' ')
    except Exception:
        if val[:10] == TODAY_ISO:
            return 'Today'
        return val[:10]

# ─────────────────────────────────────────────────────────────
# 5. HTML BUILDERS
# ─────────────────────────────────────────────────────────────
def fmt_m(val, decimals=1):
    """Format a dollar value as $Xm."""
    if val is None: return 'N/A'
    m = abs(val) / 1_000_000
    sign = '-' if val < 0 else ''
    return f'{sign}${m:.{decimals}f}M'

def fmt_pct(val):
    if val is None: return 'N/A'
    return f'{val:.1f}%'

def build_kpi_section(us_bb, rs):
    """Build KPI row HTML with real data."""
    drawn = us_bb.get('total_drawn', 0)
    avail = us_bb.get('availability', 0)
    eligible = us_bb.get('eligible', 0)
    avail_class = 'green' if avail > 5_000_000 else ('yellow' if avail > 0 else 'red')

    dq30_html = ''
    if rs:
        dq30 = rs['dpd']['dq30_pct']
        dq_class = 'green' if dq30 < 3 else ('yellow' if dq30 < 6 else 'red')
        dq30_html = f'''
      <div class="kpi {dq_class}">
        <div class="kpi-label">DPD 30+ Rate</div>
        <div class="kpi-value">{fmt_pct(dq30)}</div>
        <div class="kpi-sub">Bridge portfolio · {rs["dpd"]["max_dt"]}</div>
        <div class="kpi-delta">by balance</div>
      </div>'''
    else:
        dq30_html = '''
      <div class="kpi" style="opacity:0.5">
        <div class="kpi-label">DPD 30+ Rate</div>
        <div class="kpi-value" style="font-size:1rem;color:var(--muted)">Redshift offline</div>
        <div class="kpi-sub">Bridge portfolio</div>
        <div class="kpi-delta">Reconnecting...</div>
      </div>'''

    return f'''<!-- ② KPIs -->
  <div>
    <div class="section-label">Portfolio Overview · <span style="color:var(--muted);font-weight:400;font-size:11px">Bridge BB as of May 31, 2026</span></div>
    <div class="kpi-row">
      <div class="kpi">
        <div class="kpi-label">Bridge Drawn</div>
        <div class="kpi-value">{fmt_m(drawn)}</div>
        <div class="kpi-sub">CIM facility · vs {fmt_m(us_bb.get("borrowing_base",0))} BB cap</div>
        <div class="kpi-delta">{fmt_m(avail)} headroom</div>
      </div>
      <div class="kpi {avail_class}">
        <div class="kpi-label">Bridge Availability</div>
        <div class="kpi-value">{fmt_m(avail)}</div>
        <div class="kpi-sub">Borrowing base excess</div>
        <div class="kpi-delta">May 31 snapshot</div>
      </div>
      <div class="kpi">
        <div class="kpi-label">Eligible Receivables</div>
        <div class="kpi-value">{fmt_m(eligible)}</div>
        <div class="kpi-sub">Bridge pool · {fmt_m(us_bb.get("gross_receivables",0))} gross</div>
        <div class="kpi-delta">{fmt_m(us_bb.get("ineligibles",0))} ineligible</div>
      </div>
      <div class="kpi">
        <div class="kpi-label">Advance Rate</div>
        <div class="kpi-value">{fmt_pct((us_bb.get("adv_rate_receivables",0.85))*100)}</div>
        <div class="kpi-sub">Receivables · Bridge</div>
        <div class="kpi-delta">ex-US cash: {fmt_pct(us_bb.get("adv_rate_ex_cash",0.95)*100)}</div>
      </div>
      {dq30_html}
    </div>
  </div>'''

def build_us_bb_html(us_bb):
    gross = us_bb.get('gross_receivables', 0)
    inelig = us_bb.get('ineligibles', 0)
    eligible = us_bb.get('eligible', 0)
    counted = us_bb.get('receivables_counted', 0)
    us_cash = us_bb.get('us_cash', 0)
    ex_cash = us_bb.get('ex_us_cash', 0)
    mc = us_bb.get('mastercard', 0)
    ar = us_bb.get('adv_rate_receivables', 0.85)
    bb = us_bb.get('borrowing_base', 0)
    drawn = us_bb.get('total_drawn', 0)
    avail = us_bb.get('availability', 0)
    avail_class = 'bb-avail' if avail >= 0 else 'bb-avail tight'
    avail_lbl = 'Borrowing base excess' if avail >= 0 else 'Borrowing base deficit'

    # Receivable BB = counted * adv_rate
    rec_bb = counted * ar
    us_cash_bb = us_cash * 1.0
    ex_cash_bb = ex_cash * us_bb.get('adv_rate_ex_cash', 0.95)
    mc_bb = mc * us_bb.get('mastercard_adv_rate', 0.40)

    return f'''<!-- US BB -->
      <div class="card">
        <div class="card-header">
          <div class="card-title">US Borrowing Base</div>
          <div class="card-badge live">CIM Bridge · May 31</div>
        </div>
        <div class="bb-inner">
          <div class="bb-title">Pool Waterfall <span class="tag">May 31, 2026</span></div>
          <div class="bb-line"><span class="lbl">Gross Receivables</span><span>{fmt_m(gross)}</span></div>
          <div class="bb-line"><span class="lbl" style="color:var(--red)">(&#8722;) Ineligibles</span><span style="color:var(--red)">({fmt_m(inelig)})</span></div>
          <div class="bb-line"><span class="lbl">Eligible Pool</span><span>{fmt_m(eligible)}</span></div>
          <div class="bb-line"><span class="lbl">Receivables Counted</span><span>{fmt_m(counted)}</span></div>
          <div class="bb-line" style="border-top:1px solid var(--border);margin-top:4px;padding-top:4px"><span class="lbl">Rec BB @ {fmt_pct(ar*100)}</span><span>{fmt_m(rec_bb)}</span></div>
          <div class="bb-line"><span class="lbl">US Cash BB</span><span>{fmt_m(us_cash_bb)}</span></div>
          <div class="bb-line"><span class="lbl">ex-US Cash BB @ {fmt_pct(us_bb.get("adv_rate_ex_cash",0.95)*100)}</span><span>{fmt_m(ex_cash_bb)}</span></div>
          <div class="bb-line"><span class="lbl" style="color:var(--red)">MC Liability @ 40%</span><span style="color:var(--red)">({fmt_m(abs(mc_bb))})</span></div>
          <div class="bb-line" style="font-weight:600;border-top:1px solid var(--border);margin-top:4px;padding-top:4px"><span class="lbl">Borrowing Base</span><span>{fmt_m(bb)}</span></div>
          <div class="bb-line"><span class="lbl">Total Drawn</span><span>({fmt_m(drawn)})</span></div>
          <div class="{avail_class}">{fmt_m(avail)} available</div>
          <div class="bb-avail-lbl">{avail_lbl}</div>
        </div>
      </div>'''

def build_mx_bb_html(mx_bb):
    fac = mx_bb.get('facility_size', 100_000_000)
    cash_mxn = mx_bb.get('collection_cash_mxn', 0)
    swap_mxn = mx_bb.get('swap_mxn', 0)
    fx = mx_bb.get('usdmxn', 17.31)
    cash_usd = cash_mxn / fx
    swap_usd = swap_mxn / fx
    n_rec = mx_bb.get('total_receivables', 0)

    return f'''<!-- MX BB -->
      <div class="card">
        <div class="card-header">
          <div class="card-title">MX Borrowing Base</div>
          <div class="card-badge live">BBVA SOFOM · Jun 2</div>
        </div>
        <div class="bb-inner">
          <div class="bb-title">Collateral Summary <span class="tag">Jun 2, 2026</span></div>
          <div class="bb-line"><span class="lbl">Facility Size</span><span>{fmt_m(fac)}</span></div>
          <div class="bb-line"><span class="lbl">Receivables in Pool</span><span>{int(n_rec):,} accts</span></div>
          <div class="bb-line" style="border-top:1px solid var(--border);margin-top:4px;padding-top:4px">
            <span class="lbl">Collection Acct Cash</span><span>MXN {cash_mxn/1e6:.1f}M</span>
          </div>
          <div class="bb-line"><span class="lbl">&nbsp; USD equiv @ {fx:.2f}</span><span>{fmt_m(cash_usd)}</span></div>
          <div class="bb-line"><span class="lbl" style="color:var(--red)">Swap Contract</span><span style="color:var(--red)">MXN {swap_mxn/1e6:.1f}M</span></div>
          <div class="bb-line" style="color:var(--muted);font-size:11px;margin-top:8px">
            <span>Receivable BB values pending draw request submission</span>
          </div>
        </div>
        <div class="inelig-section">
          <div class="inelig-title">FX Rate</div>
          <div class="inelig-row"><span>USD/MXN (spot)</span><span>{fx:.4f}</span></div>
          <div class="inelig-row"><span>Net cash USD equiv</span><span>{fmt_m(cash_usd + swap_usd)}</span></div>
        </div>
      </div>'''

def build_covenant_html(us_bb):
    concs = us_bb.get('concentrations', [])
    # Build known covenant rows from real data
    rows_html = ''

    # Map known labels to short names
    LABEL_MAP = {
        'the outstanding principal balance of the Eligible Receivables included in the Borrowing Base that are obligations of any single Account Debtor': 'Single Obligor Concentration',
        'the outstanding principal balance of the Eligible Receivables included in the Borrowing Base that are obligations of the largest three Account Debtors': 'Top 3 Obligors',
        'not more than thirty percent': '>$2M Credit Limit',
        'not more than ten percent (10%) of the aggregate Receivable Balance of Eligible Receivables in the Financed Portfolio consists of Account Debtors with a Jeeves Unified Risk Score of D': 'Risk Score D',
        'the outstanding principal balance of the Eligible Receivables included in the Borrowing Base that are obligations of Account Debtors in a single industry': 'Single Industry',
        'not more than fifteen percent': 'Non-Core Jurisdictions',
        'the outstanding principal balance of the Eligible Receivables included in the Borrowing Base that are obligations of Account Debtors that are Start-Ups': 'Start-Up Obligors',
        'the outstanding principal balance of the Eligible Receivables included in the Borrowing Base that are obligations of Account Debtors in a high risk industry': 'High Risk Industry',
        'the outstanding principal balance of the Eligible Receivables included in the Borrowing Base that are obligations of Account Debtors in high risk provinces': 'High Risk Provinces',
        'the outstanding principal balance of the Eligible Receivables included in the Borrowing Base that are obligations of Account Debtors that have been onboarded by Jeeves in the last six': 'New Clients <6mo',
    }

    def short_label(lbl):
        for k, v in LABEL_MAP.items():
            if k.lower()[:40] in lbl.lower():
                return v
        return lbl[:50]

    for c in concs:
        actual = c['actual_pct']
        limit = c['limit_pct']
        passing = c['pass']
        excess = c['excess']
        row_class = 'cov-row' if passing else 'cov-row warn'
        actual_class = 'cov-actual pass' if passing else 'cov-actual warn-c'
        label = short_label(c['label'])
        note = '' if passing else f'  +${excess/1000:.0f}K excess'
        rows_html += f'''
          <div class="{row_class}">
            <div>
              <div class="cov-name">{label}</div>
              <div class="cov-facility">CIM Bridge{note}</div>
            </div>
            <div style="text-align:right">
              <div class="{actual_class}">{fmt_pct(actual)}</div>
              <div class="cov-threshold">Max {fmt_pct(limit)}</div>
            </div>
          </div>'''

    # Add MX SOFOM facility size note
    rows_html += '''
          <div class="cov-row">
            <div>
              <div class="cov-name">SOFOM Facility</div>
              <div class="cov-facility">BBVA SOFOM — $100M committed</div>
            </div>
            <div style="text-align:right">
              <div class="cov-actual pass">Active</div>
              <div class="cov-threshold">Jun 2, 2026</div>
            </div>
          </div>'''

    return f'''<!-- COVENANTS -->
      <div class="card">
        <div class="card-header">
          <div class="card-title">Covenant Tracker</div>
          <div class="card-badge">CIM Bridge · May 31</div>
        </div>
        <div class="cov-list">
          {rows_html}
        </div>
      </div>'''

def build_ops_pulse_html(states):
    sched = states.get('report_scheduler_state', {})
    analytics = states.get('analytics_cron_state', {})
    rev = states.get('revenue_comp_state', {})
    dreams = states.get('dreams_state', {})
    eod = states.get('eod_review_state', {})

    def op_card(name, last_val, schedule, extra=''):
        is_today = str(last_val or '')[:10] == TODAY_ISO or str(last_val or '').startswith(TODAY_ISO)
        is_running = str(last_val or '').endswith('_running')
        status_class = 'ops-ok' if is_today else ('ops-warn' if last_val else 'ops-err')
        status_text = 'Running...' if is_running else ('OK' if is_today else 'Stale')
        status_icon = '&#8635;' if is_running else ('&#10003;' if is_today else '!')
        return f'''
      <div class="ops-row" style="flex-direction:column;align-items:flex-start;background:var(--surface);border:1px solid var(--border);border-radius:10px;padding:14px 16px;">
        <div style="display:flex;justify-content:space-between;width:100%;align-items:center;margin-bottom:6px;">
          <div class="ops-name">{name}</div>
          <div class="ops-status {status_class}">{status_icon} {status_text}</div>
        </div>
        <div class="ops-time">Last: {fmt_state_date(last_val)}</div>
        <div class="ops-time" style="margin-top:2px;">{schedule}</div>
        {f'<div class="ops-time" style="margin-top:2px;">{extra}</div>' if extra else ''}
      </div>'''

    cards = ''
    cards += op_card('Analytics Cron', analytics.get('last_daily'), 'Daily 8am')
    cards += op_card('Revenue Comp', rev.get('last_run'), 'Daily 8am')
    cards += op_card('SOFOM Distribution', sched.get('last_sofom_distribution'), 'Weekdays · on email')
    cards += op_card('MX BB Cron', sched.get('last_mx_bb'), 'Daily 8:30am')
    cards += op_card('US BB Cron', sched.get('last_bb'), 'Mondays 8am')
    cards += op_card('Portfolio Report', sched.get('portfolio_202606'), '2nd–5th monthly')

    dream_extra = f'Count: {dreams.get("dream_count","?")} | Auto-approve: {"on" if dreams.get("auto_approve") else "off"}'
    cards += op_card('Dreams (Memory)', dreams.get('last_dream'), 'Nightly', dream_extra)

    eod_val = eod.get('last_eod', '')
    cards += op_card('EOD Review', eod_val[:10] if eod_val else None, 'Weekdays ~5pm',
                     f'Reviews: {eod.get("review_count","?")}')

    return f'''<!-- ⑤ OPS PULSE (CRONS ONLY) -->
  <div>
    <div class="section-label">Automation Status · <span style="color:var(--muted);font-weight:400;font-size:11px">as of {TODAY_STR}</span></div>
    <div style="display:grid;grid-template-columns:repeat(4,1fr);gap:10px;">
      {cards}
    </div>
  </div>'''

def build_strats_pending_html(rs):
    """Portfolio strats — real data if Redshift available, else pending badge."""
    if rs:
        dpd = rs['dpd']
        max_dt = dpd['max_dt']
        bars = ''
        for label, val, cls in [
            ('Current', dpd['current_pct'], 'green'),
            ('1–30 DPD', dpd['d1_30_pct'], 'yellow'),
            ('31–60 DPD', dpd['d31_60_pct'], 'orange'),
            ('61–90 DPD', dpd['d61_90_pct'], 'orange'),
            ('90+ DPD', dpd['d90p_pct'], 'red'),
        ]:
            bars += f'''
          <div style="display:flex;align-items:center;gap:8px;margin-bottom:6px;">
            <div style="width:80px;font-size:11px;color:var(--muted)">{label}</div>
            <div style="flex:1;background:var(--surface2);border-radius:4px;height:16px;overflow:hidden;">
              <div style="width:{val}%;height:100%;background:var(--{cls},var(--accent));"></div>
            </div>
            <div style="width:40px;text-align:right;font-size:12px;font-weight:600">{val}%</div>
          </div>'''

        # Geo bars
        geo_bars = ''
        for g in rs['geo'][:6]:
            geo_bars += f'''
          <div style="display:flex;align-items:center;gap:8px;margin-bottom:5px;">
            <div style="width:90px;font-size:11px;color:var(--muted);white-space:nowrap">{g["name"]}</div>
            <div style="flex:1;background:var(--surface2);border-radius:4px;height:14px;overflow:hidden;">
              <div style="width:{g["pct"]}%;height:100%;background:var(--accent);"></div>
            </div>
            <div style="width:44px;text-align:right;font-size:11px;">{g["pct"]}%</div>
          </div>'''

        # Originations bars
        orig_bars = ''
        if rs['monthly_orig']:
            max_orig = max(m['orig'] for m in rs['monthly_orig']) or 1
            for m in rs['monthly_orig']:
                pct = round(m['orig']/max_orig*100, 0)
                orig_bars += f'''
          <div style="display:flex;align-items:center;gap:8px;margin-bottom:5px;">
            <div style="width:55px;font-size:11px;color:var(--muted)">{m["month"]}</div>
            <div style="flex:1;background:var(--surface2);border-radius:4px;height:14px;overflow:hidden;">
              <div style="width:{pct}%;height:100%;background:var(--accent);"></div>
            </div>
            <div style="width:54px;text-align:right;font-size:11px;">${m["orig"]/1e6:.1f}M</div>
          </div>'''

        pm = rs['product_mix']
        strats_html = f'''
    <div style="display:grid;grid-template-columns:1fr 1fr 1fr 1fr;gap:16px;margin-top:12px;">
      <div class="card">
        <div class="card-header"><div class="card-title">DPD Buckets</div><div class="card-badge live">{max_dt}</div></div>
        <div style="padding:4px 0">{bars}</div>
        <div style="color:var(--muted);font-size:11px;margin-top:6px">DPD 30+: {dpd["dq30_pct"]}% by balance | {dpd["accounts"]:,} accts | {fmt_m(dpd["total"])} total</div>
      </div>
      <div class="card">
        <div class="card-header"><div class="card-title">Geographic Exposure</div><div class="card-badge live">{max_dt}</div></div>
        <div style="padding:4px 0">{geo_bars}</div>
        <div style="color:var(--muted);font-size:11px;margin-top:6px">Total: {fmt_m(sum(g["balance"] for g in rs["geo"]))}</div>
      </div>
      <div class="card">
        <div class="card-header"><div class="card-title">Monthly Originations</div><div class="card-badge live">6-mo trend</div></div>
        <div style="padding:4px 0">{orig_bars}</div>
      </div>
      <div class="card">
        <div class="card-header"><div class="card-title">Product Mix</div><div class="card-badge live">{max_dt}</div></div>
        <div style="padding:16px 0">
          <div style="display:flex;justify-content:space-between;margin-bottom:8px">
            <span style="font-size:12px">LOC</span><span style="font-weight:600">{pm["loc_pct"]}% · {fmt_m(pm["loc"])}</span>
          </div>
          <div style="display:flex;justify-content:space-between">
            <span style="font-size:12px">Jeeves Pay</span><span style="font-weight:600">{pm["jp_pct"]}% · {fmt_m(pm["jp"])}</span>
          </div>
          <div style="margin-top:12px;background:var(--surface2);border-radius:6px;height:20px;overflow:hidden;display:flex">
            <div style="width:{pm["loc_pct"]}%;background:var(--accent);height:100%"></div>
            <div style="width:{pm["jp_pct"]}%;background:var(--yellow,#f59e0b);height:100%"></div>
          </div>
          <div style="color:var(--muted);font-size:11px;margin-top:6px">Total: {fmt_m(pm["total"])}</div>
        </div>
      </div>
    </div>
'''
    else:
        strats_html = '''
    <div style="display:grid;grid-template-columns:repeat(4,1fr);gap:16px;margin-top:12px;">
      <div class="card" style="opacity:0.5"><div class="card-header"><div class="card-title">DPD Buckets</div><div class="card-badge" style="color:var(--yellow)">Redshift offline</div></div><div style="padding:16px;color:var(--muted);font-size:12px">Will refresh when VPN reconnects</div></div>
      <div class="card" style="opacity:0.5"><div class="card-header"><div class="card-title">Geographic Exposure</div><div class="card-badge" style="color:var(--yellow)">Redshift offline</div></div><div style="padding:16px;color:var(--muted);font-size:12px">Will refresh when VPN reconnects</div></div>
      <div class="card" style="opacity:0.5"><div class="card-header"><div class="card-title">Monthly Originations</div><div class="card-badge" style="color:var(--yellow)">Redshift offline</div></div><div style="padding:16px;color:var(--muted);font-size:12px">Will refresh when VPN reconnects</div></div>
      <div class="card" style="opacity:0.5"><div class="card-header"><div class="card-title">Product Mix</div><div class="card-badge" style="color:var(--yellow)">Redshift offline</div></div><div style="padding:16px;color:var(--muted);font-size:12px">Will refresh when VPN reconnects</div></div>
    </div>'''

    return f'''<!-- ④ PORTFOLIO STRATIFICATIONS -->
  <div>
    <div class="section-label">Portfolio Stratifications{"" if rs else " · <span style=\"color:var(--yellow);font-size:11px\">Redshift offline — strats will auto-refresh on reconnect</span>"}</div>
    {strats_html}
  </div>'''

# ─────────────────────────────────────────────────────────────
# 6. MAIN — inject into HTML
# ─────────────────────────────────────────────────────────────
def replace_section(html, start_marker, end_marker, new_content):
    s = html.find(start_marker)
    if s == -1:
        print(f"WARNING: marker not found: {start_marker}", file=sys.stderr)
        return html
    e = html.find(end_marker, s)
    if e == -1:
        print(f"WARNING: end marker not found after: {start_marker}", file=sys.stderr)
        return html
    return html[:s] + new_content + '\n  ' + html[e:]

def main():
    dashboard_path = os.path.join(OUTPUTS, 'Dashboard - Capital Markets Wireframe - 20260603.html')
    if not os.path.exists(dashboard_path):
        print(f"ERROR: Dashboard not found at {dashboard_path}", file=sys.stderr)
        sys.exit(1)

    html = Path(dashboard_path).read_text(encoding='utf-8')

    # ── BB files
    print("Fetching latest BB files from Drive...")
    svc = get_drive_service()
    bridge_file = search_latest_bb(svc, 'Bridge Borrowing Base')
    sofom_file = search_latest_bb(svc, 'SOFOM Borrowing Base')

    us_bb = {}
    if bridge_file:
        print(f"  Bridge BB: {bridge_file['name']}")
        bb_text = fetch_bb_text(bridge_file['id'])
        us_bb = parse_us_bridge_bb(bb_text)
        print(f"  Parsed: eligible={us_bb.get('eligible',0)/1e6:.1f}M drawn={us_bb.get('total_drawn',0)/1e6:.1f}M avail={us_bb.get('availability',0)/1e6:.1f}M")
    else:
        print("  No Bridge BB found in Drive")

    mx_bb = {}
    if sofom_file:
        print(f"  SOFOM BB: {sofom_file['name']}")
        sofom_text = fetch_bb_text(sofom_file['id'])
        mx_bb = parse_sofom_bb(sofom_text)
        print(f"  Parsed: facility={mx_bb.get('facility_size',0)/1e6:.0f}M cash_mxn={mx_bb.get('collection_cash_mxn',0)/1e6:.1f}M")
    else:
        print("  No SOFOM BB found")

    # ── Redshift
    print("Querying Redshift...")
    rs = pull_redshift_data()
    if rs:
        print(f"  Redshift OK: max_dt={rs['max_dt']} DPD30+={rs['dpd']['dq30_pct']}%")
    else:
        print("  Redshift offline — strats will show pending badge")

    # ── State files
    print("Reading ops state files...")
    states = load_state_files()
    print(f"  Loaded {len(states)} state files")

    # ── Inject sections
    print("Injecting real data into dashboard HTML...")

    # KPI row
    if us_bb:
        kpi_new = build_kpi_section(us_bb, rs)
        html = replace_section(html, '<!-- ② KPIs -->', '<!-- ③ CICO', kpi_new + '\n\n  ')

    # US BB section
    if us_bb:
        us_bb_new = build_us_bb_html(us_bb)
        html = replace_section(html, '<!-- US BB -->', '<!-- MX BB -->', us_bb_new + '\n\n      ')

    # MX BB section
    if mx_bb:
        mx_bb_new = build_mx_bb_html(mx_bb)
        html = replace_section(html, '<!-- MX BB -->', '<!-- COVENANTS -->', mx_bb_new + '\n\n      ')

    # Covenants
    if us_bb and us_bb.get('concentrations'):
        cov_new = build_covenant_html(us_bb)
        html = replace_section(html, '<!-- COVENANTS -->', '<!-- ④', cov_new + '\n\n  ')

    # Portfolio strats
    strats_new = build_strats_pending_html(rs)
    html = replace_section(html, '<!-- ④ PORTFOLIO STRATIFICATIONS -->', '<!-- ⑤ OPS PULSE', strats_new + '\n\n  ')

    # Ops pulse
    if states:
        ops_new = build_ops_pulse_html(states)
        html = replace_section(html, '<!-- ⑤ OPS PULSE (CRONS ONLY) -->', '</div><!-- /main -->', ops_new + '\n\n</div><!-- /main -->')

    # Update timestamp in header
    now_str = datetime.now().strftime('%b %-d, %Y %H:%M' if sys.platform != 'win32' else '%b %d, %Y %H:%M')
    html = re.sub(r'(Updated|Refreshed)[^<]{0,40}(?=<)', f'Updated {now_str}', html)

    # Write output
    out_path = os.path.join(OUTPUTS, 'Dashboard - Capital Markets Wireframe - 20260603.html')
    Path(out_path).write_text(html, encoding='utf-8')
    print(f"Dashboard written: {out_path}")


    # ── Write BB metrics state for Grafana Prometheus scrape ────────────────────
    try:
        bb_metrics = {
            'us_bridge': {
                'total_drawn':   us_bb.get('total_drawn', 0),
                'availability':  us_bb.get('availability', 0),
                'eligible':      us_bb.get('eligible', 0),
                'borrowing_base': us_bb.get('borrowing_base', 0),
            } if us_bb else {},
            'mx_sofom': {
                'collection_cash_mxn': mx_bb.get('collection_cash_mxn', 0),
                'total_receivables':   mx_bb.get('total_receivables', 0),
            } if mx_bb else {},
            'portfolio': {
                'dq30_pct': rs['dpd']['dq30_pct'] if rs else None,
                'total':    rs['dpd']['total']     if rs else None,
                'accounts': rs['dpd']['accounts']  if rs else None,
            },
        }
        bb_state_path = Path('C:/Jeeves/redshift-bot/deer-flow/backend/.deer-flow/_bb_metrics_state.json')
        bb_state_path.write_text(json.dumps(bb_metrics, indent=2), encoding='utf-8')
        print(f"BB metrics state written: {bb_state_path}")
        # Run cap markets metrics writer to build full _cap_markets_state.json
        subprocess.run(
            [sys.executable,
             'C:/Jeeves/redshift-bot/deer-flow/backend/scripts/cap_markets_metrics_writer.py'],
            capture_output=True, text=True, timeout=30
        )
        print("Cap markets metrics state updated")
    except Exception as e:
        print(f"Warning: could not write BB metrics state: {e}", file=sys.stderr)

    # Upload to Drive
    print("Uploading to Drive...")
    result = subprocess.run(
        [sys.executable,
         os.path.join(SKILLS, 'custom', 'google-drive', 'upload_to_drive.py'),
         out_path, '--folder-id', '1KbriPJYqDN0WIfaVTzc0dkH2J5Xnpb9J',
         '--file-id', '1dKMSGs82wZPX5pss-mCfWH_rQgdztnXi'],
        capture_output=True, text=True, encoding='utf-8', errors='replace'
    )
    output = (result.stdout + result.stderr).strip()
    print(output)
    m = re.search(r'https://drive\.google\.com/\S+', output)
    if m:
        print(f"Drive link: {m.group(0)}")
    return m.group(0) if m else None


if __name__ == '__main__':
    link = main()
    if link:
        print(f"\nDone. Dashboard: {link}")
