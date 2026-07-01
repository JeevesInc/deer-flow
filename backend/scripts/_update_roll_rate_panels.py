"""Add the 'Paid/Charged-off' column to the existing roll-rate matrix panels."""
import json
from pathlib import Path

p = Path("C:/Jeeves/redshift-bot/monitoring/grafana/dashboards/capital-markets.json")
d = json.loads(p.read_text(encoding="utf-8"))

bucket_order = {
    "from_bucket\\to_bucket": 0,
    "Current": 1,
    "1-30 DPD": 2,
    "31-60 DPD": 3,
    "61-90 DPD": 4,
    "90+ DPD": 5,
    "Paid/Charged-off": 6,
}

for panel_id in (701, 702):
    panel = next((x for x in d["panels"] if x.get("id") == panel_id), None)
    if not panel:
        continue
    for t in panel.get("transformations", []):
        if t.get("id") == "organize" and "indexByName" in t.get("options", {}):
            t["options"]["indexByName"] = dict(bucket_order)

d["version"] = 14
p.write_text(json.dumps(d, indent=2, ensure_ascii=False), encoding="utf-8")
print("Roll-rate panels updated with Paid/Charged-off column.")
