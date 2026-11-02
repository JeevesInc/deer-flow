"""Replace the existing roll-rate matrix panels (id 700-702) and covenant
table panels (id 601, 602) with cleaner versions:
  - Roll rate: count matrix + % matrix (row-normalized), BOP/EOP in row title
  - Covenants: cleaner table transforms, no extra columns/whitespace, color-coded headroom"""
import json
from pathlib import Path

p = Path("C:/Jeeves/redshift-bot/monitoring/grafana/dashboards/capital-markets.json")
d = json.loads(p.read_text(encoding="utf-8"))

# Find the existing roll-rate row
roll_row = next((x for x in d["panels"] if x.get("id") == 700), None)
roll_y = roll_row["gridPos"]["y"] if roll_row else None

# Drop the old roll rate panels (700, 701, 702) — we'll re-emit
d["panels"] = [x for x in d["panels"] if x.get("id") not in (700, 701, 702)]

if roll_y is not None:
    roll_panels = [
        {
            "type": "row",
            "title": "Roll Rate Matrix (BOP -> EOP DPD)",
            "id": 700,
            "gridPos": {"h": 1, "w": 24, "x": 0, "y": roll_y},
            "collapsed": False
        },
        {
            "type": "stat", "title": "Period — BOP -> EOP", "id": 703,
            "gridPos": {"h": 3, "w": 24, "x": 0, "y": roll_y + 1},
            "targets": [{
                "expr": "jeeves_cm_roll_period_info",
                "instant": True,
                "legendFormat": "BOP: {{bop_dt}}    ->    EOP: {{eop_dt}}",
                "refId": "A"
            }],
            "options": {
                "reduceOptions": {"calcs": ["lastNotNull"], "fields": ""},
                "colorMode": "background", "graphMode": "none", "textMode": "name",
                "justifyMode": "center", "orientation": "auto",
                "noValue": "Period unknown"
            },
            "fieldConfig": {"defaults": {
                "unit": "none",
                "color": {"mode": "fixed", "fixedColor": "blue"},
                "thresholds": {"mode": "absolute", "steps": [{"color": "blue", "value": None}]}
            }}
        },
        {
            "type": "table", "title": "Roll Rate — units (loan counts)", "id": 701,
            "gridPos": {"h": 9, "w": 12, "x": 0, "y": roll_y + 4},
            "targets": [{
                "expr": "jeeves_cm_roll_count",
                "instant": True, "legendFormat": "", "refId": "A", "format": "table"
            }],
            "transformations": [
                {"id": "organize", "options": {
                    "excludeByName": {"Time": True, "__name__": True, "instance": True, "job": True}
                }},
                {"id": "groupingToMatrix", "options": {
                    "columnField": "to_bucket", "rowField": "from_bucket", "valueField": "Value",
                    "emptyValue": "zero"
                }},
                {"id": "organize", "options": {
                    "indexByName": {
                        "from_bucket\\to_bucket": 0,
                        "Current": 1, "1-30 DPD": 2, "31-60 DPD": 3,
                        "61-90 DPD": 4, "90+ DPD": 5
                    }
                }}
            ],
            "fieldConfig": {
                "defaults": {
                    "unit": "none", "decimals": 0,
                    "custom": {"cellOptions": {"type": "color-background"}, "align": "center"},
                    "thresholds": {"mode": "absolute", "steps": [
                        {"color": "green", "value": None},
                        {"color": "yellow", "value": 5},
                        {"color": "orange", "value": 30},
                        {"color": "red", "value": 100}
                    ]},
                    "color": {"mode": "thresholds"}
                },
                "overrides": [
                    {"matcher": {"id": "byName", "options": "from_bucket\\to_bucket"},
                     "properties": [
                         {"id": "displayName", "value": "BOP \\ EOP"},
                         {"id": "custom.cellOptions", "value": {"type": "auto"}},
                         {"id": "custom.align", "value": "left"},
                         {"id": "color", "value": {"mode": "fixed", "fixedColor": "text"}}
                     ]}
                ]
            }
        },
        {
            "type": "table", "title": "Roll Rate — % (row-normalized)", "id": 702,
            "gridPos": {"h": 9, "w": 12, "x": 12, "y": roll_y + 4},
            "targets": [{
                "expr": "jeeves_cm_roll_pct",
                "instant": True, "legendFormat": "", "refId": "A", "format": "table"
            }],
            "transformations": [
                {"id": "organize", "options": {
                    "excludeByName": {"Time": True, "__name__": True, "instance": True, "job": True}
                }},
                {"id": "groupingToMatrix", "options": {
                    "columnField": "to_bucket", "rowField": "from_bucket", "valueField": "Value",
                    "emptyValue": "zero"
                }},
                {"id": "organize", "options": {
                    "indexByName": {
                        "from_bucket\\to_bucket": 0,
                        "Current": 1, "1-30 DPD": 2, "31-60 DPD": 3,
                        "61-90 DPD": 4, "90+ DPD": 5
                    }
                }}
            ],
            "fieldConfig": {
                "defaults": {
                    "unit": "percent", "decimals": 1,
                    "custom": {"cellOptions": {"type": "color-background"}, "align": "center"},
                    "thresholds": {"mode": "absolute", "steps": [
                        {"color": "green", "value": None},
                        {"color": "yellow", "value": 5},
                        {"color": "orange", "value": 20},
                        {"color": "red", "value": 50}
                    ]},
                    "color": {"mode": "thresholds"}
                },
                "overrides": [
                    {"matcher": {"id": "byName", "options": "from_bucket\\to_bucket"},
                     "properties": [
                         {"id": "displayName", "value": "BOP \\ EOP"},
                         {"id": "unit", "value": "none"},
                         {"id": "custom.cellOptions", "value": {"type": "auto"}},
                         {"id": "custom.align", "value": "left"},
                         {"id": "color", "value": {"mode": "fixed", "fixedColor": "text"}}
                     ]}
                ]
            }
        }
    ]
    d["panels"].extend(roll_panels)

# Replace covenant tables with cleaner transforms
for cov_id, facility in ((601, "us_bridge"), (602, "mx_sofom")):
    old = next((x for x in d["panels"] if x.get("id") == cov_id), None)
    if not old:
        continue
    gp = old["gridPos"]
    excess_unit = "currencyUSD" if facility == "us_bridge" else "none"
    excess_label = "Excess $" if facility == "us_bridge" else "Excess MXN"
    new = {
        "type": "table",
        "title": ("US Bridge — CIM Concentration Compliance" if facility == "us_bridge"
                  else "MX SOFOM — BBVA Concentration Compliance"),
        "id": cov_id,
        "gridPos": gp,
        "targets": [
            {"expr": f"jeeves_cm_covenant_actual_pct{{facility=\"{facility}\"}}",
             "instant": True, "legendFormat": "", "refId": "actual", "format": "table"},
            {"expr": f"jeeves_cm_covenant_limit_pct{{facility=\"{facility}\"}}",
             "instant": True, "legendFormat": "", "refId": "limit", "format": "table"},
            {"expr": f"jeeves_cm_covenant_headroom_pct{{facility=\"{facility}\"}}",
             "instant": True, "legendFormat": "", "refId": "head", "format": "table"},
            {"expr": f"jeeves_cm_covenant_excess{{facility=\"{facility}\"}}",
             "instant": True, "legendFormat": "", "refId": "excess", "format": "table"},
        ],
        "transformations": [
            {"id": "merge"},
            {"id": "organize", "options": {
                "excludeByName": {
                    "Time": True, "__name__": True, "instance": True,
                    "job": True, "facility": True
                },
                "renameByName": {
                    "test": "Test",
                    "Value #actual": "Actual %",
                    "Value #limit": "Limit %",
                    "Value #head": "Headroom %",
                    "Value #excess": excess_label
                },
                "indexByName": {
                    "test": 0,
                    "Value #actual": 1,
                    "Value #limit": 2,
                    "Value #head": 3,
                    "Value #excess": 4
                }
            }},
        ],
        "fieldConfig": {
            "defaults": {"unit": "none", "custom": {"align": "left"}},
            "overrides": [
                {"matcher": {"id": "byName", "options": "Test"},
                 "properties": [{"id": "custom.minWidth", "value": 220}]},
                {"matcher": {"id": "byName", "options": "Actual %"},
                 "properties": [
                     {"id": "unit", "value": "percent"},
                     {"id": "decimals", "value": 2},
                     {"id": "custom.align", "value": "right"}
                 ]},
                {"matcher": {"id": "byName", "options": "Limit %"},
                 "properties": [
                     {"id": "unit", "value": "percent"},
                     {"id": "decimals", "value": 2},
                     {"id": "custom.align", "value": "right"}
                 ]},
                {"matcher": {"id": "byName", "options": "Headroom %"},
                 "properties": [
                     {"id": "unit", "value": "percent"},
                     {"id": "decimals", "value": 2},
                     {"id": "custom.align", "value": "right"},
                     {"id": "custom.cellOptions", "value": {"type": "color-background"}},
                     {"id": "thresholds", "value": {"mode": "absolute", "steps": [
                         {"color": "red", "value": None},
                         {"color": "yellow", "value": 1},
                         {"color": "green", "value": 5}
                     ]}}
                 ]},
                {"matcher": {"id": "byName", "options": excess_label},
                 "properties": [
                     {"id": "unit", "value": excess_unit},
                     {"id": "decimals", "value": 0},
                     {"id": "custom.align", "value": "right"}
                 ]},
            ]
        }
    }
    idx = d["panels"].index(old)
    d["panels"][idx] = new

# The new roll-rate has h=4 (period bar) + 9 (matrix) = 13 vs previous 9.
# Shift everything below roll_y + 1 + 9 = old end by an extra 4.
if roll_y is not None:
    shift_threshold = roll_y + 10  # original old end
    for panel in d["panels"]:
        if panel.get("id") in (700, 701, 702, 703):
            continue
        if panel.get("gridPos", {}).get("y", 0) >= shift_threshold:
            panel["gridPos"]["y"] += 4

d["version"] = 13
d["panels"].sort(key=lambda p_: (p_["gridPos"]["y"], p_["gridPos"]["x"]))
p.write_text(json.dumps(d, indent=2, ensure_ascii=False), encoding="utf-8")
print(f"Rewrote roll-rate panels (count + %) with BOP/EOP header, fixed covenant tables.")
print(f"Total panels: {len(d['panels'])}")
