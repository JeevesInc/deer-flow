import json, subprocess, sys, urllib.request, time
from pathlib import Path

STATE_DIR = Path('C:/Jeeves/redshift-bot/deer-flow/backend/.deer-flow')

bb = json.loads((STATE_DIR / '_bb_metrics_state.json').read_text())
bb['us_bridge']['facility_size']  = 75_000_000
bb['us_bridge']['borrowing_base'] = 75_000_000
bb['us_bridge']['availability']   = 75_000_000 - 71_500_000
(STATE_DIR / '_bb_metrics_state.json').write_text(json.dumps(bb, indent=2))

drawn = bb['us_bridge']['total_drawn']
avail = bb['us_bridge']['availability']
bb_cap = bb['us_bridge']['borrowing_base']
print(f"Fixed: cap=$75M  drawn=${drawn/1e6:.1f}M  avail=${avail/1e6:.1f}M  util={drawn/bb_cap*100:.1f}%")

subprocess.run([sys.executable, 'C:/Jeeves/redshift-bot/deer-flow/backend/scripts/cap_markets_metrics_writer.py'],
               check=True, capture_output=True)
print("State updated")

time.sleep(2)
with urllib.request.urlopen('http://localhost:8001/metrics', timeout=15) as r:
    body = r.read().decode()
for l in body.split('\n'):
    if 'jeeves_cm_us_bridge' in l and not l.startswith('#'):
        print(l)
