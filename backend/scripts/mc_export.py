"""Publish Mission Control to the hosted Streamlit app (brian-tryjeeves/cm-mission-control - private, personal; Streamlit Community Cloud).

Redshift sits behind Zscaler ZPA (resolves to 100.64.x), so the hosted app cannot query it. This script
runs every card from Redshift on the bot host, writes the results to data/*.parquet in a local clone of
the repo together with the app code, and pushes. Community Cloud redeploys on push.

Run after mission_control_snapshot.py (report_scheduler_cron run_mission_control).
"""
import datetime as dt
import importlib.util
import json
import os
import shutil
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
SK = os.environ.get('SKILLS_PATH') or os.path.join('C:' + os.sep, 'Jeeves', 'redshift-bot', 'deer-flow', 'skills')
BACKEND = os.path.join(SK, '..', 'backend')
REPO_URL = 'https://github.com/brian-tryjeeves/cm-mission-control.git'
CLONE = os.path.join(BACKEND, '.deer-flow', '_mission_control', 'cm-mission-control')
for p in (os.path.join(SK, 'custom', 'jeeves-borrowing-base'), os.path.join(SK, 'custom', 'cfo-org-kb', 'scripts', 'mission_control'), HERE):
    sys.path.insert(0, p)

import pandas as pd  # noqa: E402
import redshift_util as ru  # noqa: E402
import mc_cards  # noqa: E402
import mc_sql  # noqa: E402

APP_SRC = os.path.join(HERE, 'cm_credit_health_app.py')
spec = importlib.util.spec_from_file_location('mc_app', APP_SRC)
app = importlib.util.module_from_spec(spec)
spec.loader.exec_module(app)  # import-safe: body runs only under __main__

KEYS = {'facility_req': ['NB', 'CIM MX SOFOM'], 'pool': ['NB', 'MX', 'All']}
REQS = 'streamlit>=1.50\npandas>=2.0\nplotly>=5.20\npyarrow>=15,<26\n'
CONFIG = '[theme]\nbase = "dark"\nprimaryColor = "#4c9bff"\n[browser]\ngatherUsageStats = false\n'


def git(*a, cwd=None):
    r = subprocess.run(['git'] + list(a), cwd=cwd or CLONE, capture_output=True, text=True)
    if r.returncode != 0 and 'nothing to commit' not in (r.stdout + r.stderr):
        raise RuntimeError('git {} failed: {}'.format(' '.join(a), (r.stdout + r.stderr)[-400:]))
    return r.stdout


def main():
    if not os.path.isdir(os.path.join(CLONE, '.git')):
        os.makedirs(os.path.dirname(CLONE), exist_ok=True)
        git('clone', REPO_URL, CLONE, cwd=os.path.dirname(CLONE))
    try:
        git('pull', '--rebase')
    except RuntimeError:
        pass  # empty remote on first run
    data = os.path.join(CLONE, 'data')
    shutil.rmtree(data, ignore_errors=True)
    os.makedirs(data)
    conn = ru.connect()
    counts = {}
    try:
        file_keys = {
            'facility_table': None, 'kpi_avail': None, 'heatmap': None, 'obligors': None, 'recycling': None,
            'waterfall_steps': 'facility_req', 'reasons': 'facility_req', 'bb_trend': 'facility_req',
            'loans': 'facility_req', 'test_trend': 'facility_req',
            'dq_mix': 'pool', 'roll_rates': 'pool', 'roll_matrix': 'pool', 'epd': 'pool', 'vintage': 'pool', 'risk_migration': 'pool',
            'collections': 'pool'}
        for card, key in file_keys.items():
            for v in (KEYS[key] if key else [None]):
                params = {key: v} if key else {}
                df = pd.read_sql(mc_sql.render(mc_cards.CARDS[card]['sql'], params), conn)
                df.to_parquet(os.path.join(data, app.mc_export_name(card, v)), index=False)
                counts[app.mc_export_name(card, v)] = len(df)
        pd.read_sql(app.FWD_BASE_SQL, conn).to_parquet(os.path.join(data, 'forward_base.parquet'), index=False)
        pd.read_sql(app.FWD_FC_SQL, conn).to_parquet(os.path.join(data, 'forecast.parquet'), index=False)
        app.actuals_long(pd.read_sql(app.ACTUALS_SQL, conn)).to_parquet(os.path.join(data, 'actuals_series.parquet'), index=False)
        app.forecast_series().to_parquet(os.path.join(data, 'forecast_series.parquet'), index=False)
        bq, bs = app.forecast_bands()
        if len(bq):
            bq.to_parquet(os.path.join(data, 'forecast_bands.parquet'), index=False)
            bs.to_parquet(os.path.join(data, 'forecast_band_summary.parquet'), index=False)
        snap = pd.read_sql("select max(as_of) from analytics_sandbox.cm_mc_facility_daily where facility = 'NB'", conn).iloc[0, 0]
    finally:
        conn.close()
    json.dump({'published_at': dt.datetime.now().isoformat(timespec='seconds'), 'snapshot_as_of': str(snap),
               'files': counts}, open(os.path.join(data, 'meta.json'), 'w'), indent=1)
    shutil.copy2(APP_SRC, os.path.join(CLONE, 'streamlit_app.py'))
    for f in ('mc_cards.py', 'mc_sql.py'):
        shutil.copy2(os.path.join(SK, 'custom', 'cfo-org-kb', 'scripts', 'mission_control', f), os.path.join(CLONE, f))
    open(os.path.join(CLONE, 'requirements.txt'), 'w').write(REQS)
    os.makedirs(os.path.join(CLONE, '.streamlit'), exist_ok=True)
    open(os.path.join(CLONE, '.streamlit', 'config.toml'), 'w').write(CONFIG)
    open(os.path.join(CLONE, '.gitignore'), 'w').write('__pycache__/\n*.pyc\n.streamlit/secrets.toml\n')
    git('add', '-A')
    git('-c', 'user.name=deerflow-analyst', '-c', 'user.email=brian.mauck@tryjeeves.com', 'commit', '-m',
        'Publish Mission Control data - snapshot {}'.format(snap))
    git('push', '-u', 'origin', 'HEAD:main')
    print('published snapshot', snap, len(counts), 'files')


if __name__ == '__main__':
    main()
