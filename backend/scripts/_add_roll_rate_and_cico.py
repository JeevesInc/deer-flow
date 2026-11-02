"""Insert Roll Rate matrix panel + placeholder for CICO Cash into the
Grafana dashboard. CICO panels start blank; data wiring happens once we
extract from Gmail."""
import json
from pathlib import Path

p = Path("C:/Jeeves/redshift-bot/monitoring/grafana/dashboards/capital-markets.json")
d = json.loads(p.read_text(encoding="utf-8"))

# Insert below Covenants (id=600) — find Portfolio row (id=300) which
# currently follows Covenants and shift it down.
SHIFT = 10  # 1 row + 9 height for the new section
# Find Portfolio row y to use as insert_y
portfolio_row = next(p_ for p_ in d["panels"] if p_.get("id") == 300 and p_.get("type") == "row")
insert_y = portfolio_row["gridPos"]["y"]

# Shift everything at insert_y and below
for panel in d["panels"]:
    if panel.get("gridPos", {}).get("y", 0) >= insert_y:
        panel["gridPos"]["y"] += SHIFT

roll_rate_panels = [
    {
        "type": "row", "title": "Roll Rate Matrix (BOP → EOP DPD)", "id": 700,
        "gridPos": {"h": 1, "w": 24, "x": 0, "y": insert_y}, "collapsed": False
    },
    {
        "type": "table", "title": "Roll Rate — counts", "id": 701,
        "gridPos": {"h": 9, "w": 12, "x": 0, "y": insert_y + 1},
        "targets": [
            {"expr": "jeeves_cm_roll_count", "instant": True,
             "legendFormat": "", "refId": "A", "format": "table"}
        ],
        "transformations": [
            {"id": "organize", "options": {
                "excludeByName": {"Time": True, "__name__": True, "instance": True, "job": True}
            }},
            {"id": "groupingToMatrix", "options": {
                "columnField": "to_bucket",
                "rowField": "from_bucket",
                "valueField": "Value"
            }},
        ],
        "fieldConfig": {
            "defaults": {
                "unit": "none", "decimals": 0,
                "custom": {"cellOptions": {"type": "color-background"}},
                "thresholds": {"mode": "absolute", "steps": [
                    {"color": "green", "value": None},
                    {"color": "yellow", "value": 5},
                    {"color": "red", "value": 50}
                ]},
                "color": {"mode": "thresholds"}
            },
            "overrides": [
                {"matcher": {"id": "byName", "options": "from_bucket\\to_bucket"},
                 "properties": [{"id": "custom.cellOptions", "value": {"type": "auto"}}]}
            ]
        }
    },
    {
        "type": "table", "title": "Roll Rate — BOP balance ($)", "id": 702,
        "gridPos": {"h": 9, "w": 12, "x": 12, "y": insert_y + 1},
        "targets": [
            {"expr": "jeeves_cm_roll_balance_usd", "instant": True,
             "legendFormat": "", "refId": "A", "format": "table"}
        ],
        "transformations": [
            {"id": "organize", "options": {
                "excludeByName": {"Time": True, "__name__": True, "instance": True, "job": True}
            }},
            {"id": "groupingToMatrix", "options": {
                "columnField": "to_bucket",
                "rowField": "from_bucket",
                "valueField": "Value"
            }},
        ],
        "fieldConfig": {
            "defaults": {
                "unit": "currencyUSD", "decimals": 0,
                "custom": {"cellOptions": {"type": "color-background"}},
                "color": {"mode": "fixed", "fixedColor": "purple"}
            },
            "overrides": [
                {"matcher": {"id": "byName", "options": "from_bucket\\to_bucket"},
                 "properties": [{"id": "custom.cellOptions", "value": {"type": "auto"}}]}
            ]
        }
    },
]

d["panels"].extend(roll_rate_panels)
d["version"] = 11
d["panels"].sort(key=lambda p_: (p_["gridPos"]["y"], p_["gridPos"]["x"]))
p.write_text(json.dumps(d, indent=2, ensure_ascii=False), encoding="utf-8")
print(f"Inserted Roll Rate Matrix at y={insert_y}.")
print(f"Total panels: {len(d['panels'])}")
