"""Insert CICO Cash section between Roll Rate and Portfolio sections."""
import json
from pathlib import Path

p = Path("C:/Jeeves/redshift-bot/monitoring/grafana/dashboards/capital-markets.json")
d = json.loads(p.read_text(encoding="utf-8"))

# Find Portfolio row (id=300) y; insert above it (after Roll Rate)
portfolio_row = next(p_ for p_ in d["panels"] if p_.get("id") == 300 and p_.get("type") == "row")
insert_y = portfolio_row["gridPos"]["y"]
SHIFT = 9

for panel in d["panels"]:
    if panel.get("gridPos", {}).get("y", 0) >= insert_y:
        panel["gridPos"]["y"] += SHIFT

cico_panels = [
    {
        "type": "row", "title": "CICO Daily Cash (Axel — Operations)", "id": 800,
        "gridPos": {"h": 1, "w": 24, "x": 0, "y": insert_y}, "collapsed": False
    },
    {
        "type": "stat", "title": "Total Cash", "id": 801,
        "gridPos": {"h": 4, "w": 6, "x": 0, "y": insert_y + 1},
        "targets": [{"expr": "jeeves_cm_cico_total_cash_usd / 1e6", "instant": True, "refId": "A"}],
        "options": {"reduceOptions": {"calcs": ["lastNotNull"]}, "colorMode": "value", "graphMode": "none", "textMode": "value_and_name", "justifyMode": "center"},
        "fieldConfig": {"defaults": {"unit": "short", "decimals": 2, "displayName": "Total Cash ($M)",
            "color": {"mode": "fixed", "fixedColor": "blue"},
            "thresholds": {"mode": "absolute", "steps": [{"color":"blue","value":None}]}}}
    },
    {
        "type": "stat", "title": "DACA + Pledged (feeds US BB)", "id": 802,
        "gridPos": {"h": 4, "w": 6, "x": 6, "y": insert_y + 1},
        "targets": [{"expr": "jeeves_cm_cico_daca_pledged_usd / 1e6", "instant": True, "refId": "A"}],
        "options": {"reduceOptions": {"calcs": ["lastNotNull"]}, "colorMode": "value", "graphMode": "none", "textMode": "value_and_name", "justifyMode": "center"},
        "fieldConfig": {"defaults": {"unit": "short", "decimals": 2, "displayName": "DACA+Pledged ($M)",
            "color": {"mode": "fixed", "fixedColor": "green"},
            "thresholds": {"mode": "absolute", "steps": [{"color":"green","value":None}]}}}
    },
    {
        "type": "stat", "title": "Restricted Deposits", "id": 803,
        "gridPos": {"h": 4, "w": 6, "x": 12, "y": insert_y + 1},
        "targets": [{"expr": "jeeves_cm_cico_restricted_usd / 1e6", "instant": True, "refId": "A"}],
        "options": {"reduceOptions": {"calcs": ["lastNotNull"]}, "colorMode": "value", "graphMode": "none", "textMode": "value_and_name", "justifyMode": "center"},
        "fieldConfig": {"defaults": {"unit": "short", "decimals": 2, "displayName": "Restricted ($M)",
            "color": {"mode": "fixed", "fixedColor": "yellow"},
            "thresholds": {"mode": "absolute", "steps": [{"color":"yellow","value":None}]}}}
    },
    {
        "type": "stat", "title": "Total Cash + Restricted", "id": 804,
        "gridPos": {"h": 4, "w": 6, "x": 18, "y": insert_y + 1},
        "targets": [{"expr": "jeeves_cm_cico_total_cash_plus_restricted_usd / 1e6", "instant": True, "refId": "A"}],
        "options": {"reduceOptions": {"calcs": ["lastNotNull"]}, "colorMode": "value", "graphMode": "none", "textMode": "value_and_name", "justifyMode": "center"},
        "fieldConfig": {"defaults": {"unit": "short", "decimals": 2, "displayName": "Cash+Restricted ($M)",
            "color": {"mode": "fixed", "fixedColor": "purple"},
            "thresholds": {"mode": "absolute", "steps": [{"color":"purple","value":None}]}}}
    },
    {
        "type": "piechart", "title": "CICO Cash by Category", "id": 805,
        "gridPos": {"h": 8, "w": 12, "x": 0, "y": insert_y + 5},
        "targets": [{"expr": "jeeves_cm_cico_category_usd", "instant": True, "legendFormat": "{{category}}", "refId": "A"}],
        "options": {"reduceOptions": {"calcs": ["lastNotNull"]}, "legend": {"displayMode": "table", "placement": "right", "values": ["value", "percent"]}, "pieType": "donut", "displayLabels": ["name"]},
        "fieldConfig": {"defaults": {"unit": "currencyUSD", "decimals": 0}}
    },
    {
        "type": "bargauge", "title": "CICO Categories — magnitude", "id": 806,
        "gridPos": {"h": 8, "w": 12, "x": 12, "y": insert_y + 5},
        "targets": [{"expr": "jeeves_cm_cico_category_usd", "instant": True, "legendFormat": "{{category}}", "refId": "A"}],
        "options": {"reduceOptions": {"calcs": ["lastNotNull"]}, "orientation": "horizontal", "displayMode": "gradient", "showUnfilled": True},
        "fieldConfig": {"defaults": {"unit": "currencyUSD", "decimals": 0,
            "color": {"mode": "fixed", "fixedColor": "blue"}}}
    },
]

d["panels"].extend(cico_panels)
d["version"] = 12
d["panels"].sort(key=lambda p_: (p_["gridPos"]["y"], p_["gridPos"]["x"]))
p.write_text(json.dumps(d, indent=2, ensure_ascii=False), encoding="utf-8")
print(f"Inserted CICO at y={insert_y}. Panels: {len(d['panels'])}")
