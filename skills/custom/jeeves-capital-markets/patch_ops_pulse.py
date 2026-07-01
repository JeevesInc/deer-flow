#!/usr/bin/env python3
"""Patch ops pulse section in dashboard with real state file data."""
import os, sys, json, re
from datetime import date, datetime
from pathlib import Path

sys.stdout.reconfigure(encoding='utf-8', errors='replace')

TODAY_ISO = date.today().isoformat()
TODAY_STR = date.today().strftime('%b %d, %Y').replace(' 0', ' ')

OUTPUTS = os.environ.get('OUTPUTS_PATH', '/mnt/user-data/outputs')
dash_path = os.path.join(OUTPUTS, 'Dashboard - Capital Markets Wireframe - 20260603.html')

def load_states():
    base = Path('C:/Jeeves/redshift-bot/deer-flow/backend/.deer-flow')
    states = {}
    for f in ['_analytics_cron_state', '_revenue_comp_state', '_report_scheduler_state',
              '_dreams_state', '_eod_review_state']:
        p = base / f'{f}.json'
        if p.exists():
            try: states[f.strip('_')] = json.loads(p.read_text())
            except: pass
    return states

def fmt_date(val):
    if not val: return 'Never'
    val = str(val)
    if val.endswith('_running'): return 'Running...'
    try:
        dt = datetime.fromisoformat(val[:19])
        if dt.date().isoformat() == TODAY_ISO:
            h = dt.hour % 12 or 12
            ampm = 'AM' if dt.hour < 12 else 'PM'
            return f'Today {h}:{dt.minute:02d} {ampm}'
        return dt.strftime('%b %d').replace(' 0', ' ')
    except:
        return val[:10]

def op_card(name, last_val, schedule, extra=''):
    is_today = str(last_val or '')[:10] == TODAY_ISO
    is_running = str(last_val or '').endswith('_running')
    if is_running:
        sc, st = 'ops-warn', 'Running...'
    elif is_today:
        sc, st = 'ops-ok', '&#10003; OK'
    else:
        sc, st = 'ops-warn', '&#9888; Stale'
    extra_html = f'\n        <div class="ops-time" style="margin-top:2px;">{extra}</div>' if extra else ''
    return (
        f'<div class="ops-row" style="flex-direction:column;align-items:flex-start;'
        f'background:var(--surface);border:1px solid var(--border);border-radius:10px;padding:14px 16px;">'
        f'<div style="display:flex;justify-content:space-between;width:100%;align-items:center;margin-bottom:6px;">'
        f'<div class="ops-name">{name}</div>'
        f'<div class="ops-status {sc}">{st}</div></div>'
        f'<div class="ops-time">Last: {fmt_date(last_val)}</div>'
        f'<div class="ops-time" style="margin-top:2px;">{schedule}</div>{extra_html}</div>'
    )

states = load_states()
sched  = states.get('report_scheduler_state', {})
analy  = states.get('analytics_cron_state', {})
rev    = states.get('revenue_comp_state', {})
drm    = states.get('dreams_state', {})
eod    = states.get('eod_review_state', {})

dream_extra = f'Count: {drm.get("dream_count","?")} | Auto: {"on" if drm.get("auto_approve") else "off"}'
eod_last = eod.get('last_eod', '')

inner_cards = ' '.join([
    op_card('Analytics Cron',     analy.get('last_daily'),              'Daily 8am'),
    op_card('Revenue Comp',       rev.get('last_run'),                  'Daily 8am'),
    op_card('SOFOM Distribution', sched.get('last_sofom_distribution'), 'Weekdays on email'),
    op_card('MX BB Cron',         sched.get('last_mx_bb'),              'Daily 8:30am'),
    op_card('US BB Cron',         sched.get('last_bb'),                 'Mondays 8am'),
    op_card('Portfolio Report',   sched.get('portfolio_202606'),        '2nd-5th monthly'),
    op_card('Dreams',             drm.get('last_dream'),                'Nightly', dream_extra),
    op_card('EOD Review',         eod_last[:10] if eod_last else None,  'Weekdays ~5pm',
            f'Reviews: {eod.get("review_count","?")}'),
])

MARKER = '\u2464 OPS PULSE (CRONS ONLY)'  # ⑤

new_ops = (
    f'<!-- {MARKER} -->\n'
    f'  <div>\n'
    f'    <div class="section-label">Automation Status '
    f'<span style="color:var(--muted);font-weight:400;font-size:11px">as of {TODAY_STR}</span></div>\n'
    f'    <div style="display:grid;grid-template-columns:repeat(4,1fr);gap:10px;">\n'
    f'      {inner_cards}\n'
    f'    </div>\n'
    f'  </div>'
)

html = Path(dash_path).read_text(encoding='utf-8')

# Fix double main div if present
html = html.replace('</div><!-- /main -->\n  </div><!-- /main -->', '</div><!-- /main -->')

# Find ops marker
marker_full = f'<!-- {MARKER} -->'
s = html.find(marker_full)
if s == -1:
    print(f"ERROR: could not find ops marker in dashboard")
    sys.exit(1)

# Find closing structure: the section's outer </div>\n\n</div><!-- /main
# Work from the marker forward and find the second-to-last </div> before the document end
end_tag = '</div><!-- /main -->'
e = html.find(end_tag, s)
if e == -1:
    print("ERROR: could not find end of ops section")
    sys.exit(1)

# Walk backwards from e to find the closing </div> that belongs to the ops section outer div
# The ops section ends with: </div>\n  </div>\n\n</div><!-- /main -->
# We want to replace from s to (just before </div><!-- /main -->)
section_close = '\n\n' + end_tag
ec = html.rfind('\n\n', s, e)  # find the \n\n before </div><!-- /main -->
if ec == -1:
    ec = e

html = html[:s] + new_ops + '\n\n' + html[e:]

Path(dash_path).write_text(html, encoding='utf-8')
print(f"Ops pulse updated. Analytics last_daily={analy.get('last_daily')} revenue={rev.get('last_run')}")
print(f"Dashboard saved: {dash_path}")
