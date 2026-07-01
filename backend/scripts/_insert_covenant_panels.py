"""One-off: insert a Covenants section between MX SOFOM and Portfolio rows,
shift later panels down to make room, and re-emit the dashboard JSON with
proper UTF-8 (no \\uXXXX escape sequences)."""
import json
from pathlib import Path

p = Path("C:/Jeeves/redshift-bot/monitoring/grafana/dashboards/capital-markets.json")
d = json.loads(p.read_text(encoding="utf-8"))

# Find where MX SOFOM section ends (find the next row after MX SOFOM)
# Row IDs: alerts=9000, hero=1, bridge=100, sofom=200, portfolio=300, bank=400, ops=900
# Use the y of the Portfolio row (id=300) as the insertion point.
portfolio_row = next(p_ for p_ in d["panels"] if p_.get("id") == 300 and p_.get("type") == "row")
insert_y = portfolio_row["gridPos"]["y"]

# Shift everything from y=insert_y onward down by 9 (1 row + 8 height for covenant panels)
SHIFT = 9
for panel in d["panels"]:
    if panel.get("gridPos", {}).get("y", 0) >= insert_y:
        panel["gridPos"]["y"] += SHIFT

# Build the new Covenants section
covenant_panels = [
    {
        "type": "row", "title": "Covenants — Concentration Tests", "id": 600,
        "gridPos": {"h": 1, "w": 24, "x": 0, "y": insert_y}, "collapsed": False
    },
    {
        "type": "table", "title": "US Bridge — CIM Concentration Compliance", "id": 601,
        "gridPos": {"h": 8, "w": 12, "x": 0, "y": insert_y + 1},
        "targets": [
            {"expr": "jeeves_cm_covenant_actual_pct{facility=\"us_bridge\"}",
             "instant": True, "legendFormat": "{{test}}", "refId": "actual", "format": "table"},
            {"expr": "jeeves_cm_covenant_limit_pct{facility=\"us_bridge\"}",
             "instant": True, "legendFormat": "{{test}}", "refId": "limit", "format": "table"},
            {"expr": "jeeves_cm_covenant_headroom_pct{facility=\"us_bridge\"}",
             "instant": True, "legendFormat": "{{test}}", "refId": "head", "format": "table"},
            {"expr": "jeeves_cm_covenant_excess{facility=\"us_bridge\"}",
             "instant": True, "legendFormat": "{{test}}", "refId": "excess", "format": "table"},
        ],
        "transformations": [
            {"id": "merge"},
            {"id": "organize", "options": {
                "excludeByName": {"Time": True, "__name__": True, "instance": True, "job": True, "facility": True},
                "renameByName": {
                    "test": "Test",
                    "Value #actual": "Actual %",
                    "Value #limit": "Limit %",
                    "Value #head": "Headroom %",
                    "Value #excess": "Excess $",
                }
            }},
        ],
        "fieldConfig": {
            "defaults": {"unit": "none"},
            "overrides": [
                {"matcher": {"id": "byName", "options": "Actual %"},
                 "properties": [{"id": "unit", "value": "percent"}, {"id": "decimals", "value": 2}]},
                {"matcher": {"id": "byName", "options": "Limit %"},
                 "properties": [{"id": "unit", "value": "percent"}, {"id": "decimals", "value": 2}]},
                {"matcher": {"id": "byName", "options": "Headroom %"},
                 "properties": [
                     {"id": "unit", "value": "percent"}, {"id": "decimals", "value": 2},
                     {"id": "custom.cellOptions", "value": {"type": "color-background"}},
                     {"id": "thresholds", "value": {"mode": "absolute", "steps": [
                         {"color": "red", "value": None},
                         {"color": "yellow", "value": 1},
                         {"color": "green", "value": 5},
                     ]}},
                 ]},
                {"matcher": {"id": "byName", "options": "Excess $"},
                 "properties": [{"id": "unit", "value": "currencyUSD"}, {"id": "decimals", "value": 0}]},
            ],
        },
    },
    {
        "type": "table", "title": "MX SOFOM — BBVA Concentration Compliance", "id": 602,
        "gridPos": {"h": 8, "w": 12, "x": 12, "y": insert_y + 1},
        "targets": [
            {"expr": "jeeves_cm_covenant_actual_pct{facility=\"mx_sofom\"}",
             "instant": True, "legendFormat": "{{test}}", "refId": "actual", "format": "table"},
            {"expr": "jeeves_cm_covenant_limit_pct{facility=\"mx_sofom\"}",
             "instant": True, "legendFormat": "{{test}}", "refId": "limit", "format": "table"},
            {"expr": "jeeves_cm_covenant_headroom_pct{facility=\"mx_sofom\"}",
             "instant": True, "legendFormat": "{{test}}", "refId": "head", "format": "table"},
            {"expr": "jeeves_cm_covenant_excess{facility=\"mx_sofom\"}",
             "instant": True, "legendFormat": "{{test}}", "refId": "excess", "format": "table"},
        ],
        "transformations": [
            {"id": "merge"},
            {"id": "organize", "options": {
                "excludeByName": {"Time": True, "__name__": True, "instance": True, "job": True, "facility": True},
                "renameByName": {
                    "test": "Test",
                    "Value #actual": "Actual %",
                    "Value #limit": "Limit %",
                    "Value #head": "Headroom %",
                    "Value #excess": "Excess MXN",
                }
            }},
        ],
        "fieldConfig": {
            "defaults": {"unit": "none"},
            "overrides": [
                {"matcher": {"id": "byName", "options": "Actual %"},
                 "properties": [{"id": "unit", "value": "percent"}, {"id": "decimals", "value": 2}]},
                {"matcher": {"id": "byName", "options": "Limit %"},
                 "properties": [{"id": "unit", "value": "percent"}, {"id": "decimals", "value": 2}]},
                {"matcher": {"id": "byName", "options": "Headroom %"},
                 "properties": [
                     {"id": "unit", "value": "percent"}, {"id": "decimals", "value": 2},
                     {"id": "custom.cellOptions", "value": {"type": "color-background"}},
                     {"id": "thresholds", "value": {"mode": "absolute", "steps": [
                         {"color": "red", "value": None},
                         {"color": "yellow", "value": 1},
                         {"color": "green", "value": 5},
                     ]}},
                 ]},
                {"matcher": {"id": "byName", "options": "Excess MXN"},
                 "properties": [{"id": "decimals", "value": 0}]},
            ],
        },
    },
]

d["panels"].extend(covenant_panels)
d["version"] = 10

# Sort panels by (y, x) for stable Grafana ordering
d["panels"].sort(key=lambda p_: (p_["gridPos"]["y"], p_["gridPos"]["x"]))

p.write_text(json.dumps(d, indent=2, ensure_ascii=False), encoding="utf-8")
print(f"Inserted Covenants section at y={insert_y}, shifted {sum(1 for _ in d['panels'])} total panels.")
print(f"New panel count: {len(d['panels'])}")
