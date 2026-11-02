"""Restore column ordering via indexByName; use proper capture-group regex
for stripping the '0N ' prefix from column headers."""
import json
from pathlib import Path

p = Path("C:/Jeeves/redshift-bot/monitoring/grafana/dashboards/capital-markets.json")
d = json.loads(p.read_text(encoding="utf-8"))

column_order = {
    "from_bucket\\to_bucket": 0,
    "01 Current": 1,
    "02 1-30 DPD": 2,
    "03 31-60 DPD": 3,
    "04 61-90 DPD": 4,
    "05 90+ DPD": 5,
    "06 Paid Off": 6,
    "07 Charged Off": 7,
}

for panel_id in (701, 702):
    panel = next((x for x in d["panels"] if x.get("id") == panel_id), None)
    if not panel:
        continue
    panel["transformations"] = [
        {"id": "organize", "options": {
            "excludeByName": {"Time": True, "__name__": True, "instance": True, "job": True}
        }},
        # Sort by from_bucket alphabetically — the "0N " prefix ensures
        # DPD severity ordering after sort.
        {"id": "sortBy", "options": {
            "fields": {},
            "sort": [{"field": "from_bucket"}]
        }},
        {"id": "groupingToMatrix", "options": {
            "columnField": "to_bucket",
            "rowField": "from_bucket",
            "valueField": "Value",
            "emptyValue": "zero"
        }},
        # Order columns by DPD severity then Paid → Charged.
        {"id": "organize", "options": {"indexByName": dict(column_order)}},
        # Strip the "NN " prefix from column headers using a capture group.
        {"id": "renameByRegex", "options": {"regex": "^\\d{2}\\s+(.+)$", "renamePattern": "$1"}},
    ]

d["version"] = 19
p.write_text(json.dumps(d, indent=2, ensure_ascii=False), encoding="utf-8")
print("Roll rate panels restored with indexByName + capture-group renameByRegex.")
