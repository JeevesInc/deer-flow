"""Polish the Capital Markets dashboard:
1. Fix em-dash mojibake across every string (UTF-8 -> latin-1 corruption)
2. Replace broken Eligibility & BB Detail tables (id 105, 205) with bargauges
3. Bigger alert chips
4. Sort Cash by Country (desc) so big countries don't dwarf the bars
5. Headroom % column readable on solid color background
6. Consistent unit/display formatting across hero + facility KPIs
"""
import json
import re
from pathlib import Path

p = Path("C:/Jeeves/redshift-bot/monitoring/grafana/dashboards/capital-markets.json")
raw = p.read_text(encoding="utf-8")

# ── 1. Em-dash mojibake fix ────────────────────────────────────
# Various forms that the em-dash can take after a bad encoding roundtrip:
MOJIBAKE_MAP = {
    "â€”": "—",   # standard latin-1-viewing-of-UTF-8 em-dash
    "â€“": "–",   # en-dash variant
    "â€œ": "“",
    "â€": "”",  # right curly quote (some variants)
    "Ã©": "é",
    "Ã±": "ñ",
}
for bad, good in MOJIBAKE_MAP.items():
    raw = raw.replace(bad, good)

d = json.loads(raw)

# ── 2. Replace Eligibility & BB Detail tables with bargauges ───
def _bridge_eligibility_bargauge(gridpos):
    return {
        "type": "bargauge",
        "title": "US Bridge — Eligibility & BB Detail",
        "id": 105,
        "gridPos": gridpos,
        "targets": [
            {"expr": "jeeves_cm_us_bridge_total_receivables_usd", "instant": True, "legendFormat": "Total Receivables", "refId": "A"},
            {"expr": "jeeves_cm_us_bridge_eligible_usd",          "instant": True, "legendFormat": "Eligible (post-concentration)", "refId": "B"},
            {"expr": "jeeves_cm_us_bridge_ineligible_usd",        "instant": True, "legendFormat": "Ineligible", "refId": "C"},
            {"expr": "jeeves_cm_us_bridge_concentration_breaches_total_usd", "instant": True, "legendFormat": "Concentration Excess", "refId": "D"},
            {"expr": "jeeves_cm_us_bridge_borrowing_base_usd",    "instant": True, "legendFormat": "Borrowing Base", "refId": "E"},
            {"expr": "jeeves_cm_us_bridge_facility_size_usd",     "instant": True, "legendFormat": "Facility Size", "refId": "F"},
            {"expr": "jeeves_cm_us_bridge_binding_cap_usd",       "instant": True, "legendFormat": "Binding Cap", "refId": "G"},
            {"expr": "jeeves_cm_us_bridge_drawn_usd",             "instant": True, "legendFormat": "Drawn", "refId": "H"},
            {"expr": "jeeves_cm_us_bridge_available_usd",         "instant": True, "legendFormat": "Availability", "refId": "I"}
        ],
        "options": {
            "reduceOptions": {"calcs": ["lastNotNull"]},
            "orientation": "horizontal",
            "displayMode": "gradient",
            "showUnfilled": True,
            "valueMode": "color"
        },
        "fieldConfig": {
            "defaults": {
                "unit": "currencyUSD", "decimals": 0,
                "color": {"mode": "thresholds"},
                "thresholds": {"mode": "absolute", "steps": [{"color": "blue", "value": None}]}
            },
            "overrides": [
                {"matcher": {"id": "byName", "options": "Availability"}, "properties": [
                    {"id": "color", "value": {"mode": "thresholds"}},
                    {"id": "thresholds", "value": {"mode": "absolute", "steps": [
                        {"color": "red", "value": None}, {"color": "yellow", "value": 2_000_000}, {"color": "green", "value": 10_000_000}
                    ]}}
                ]},
                {"matcher": {"id": "byName", "options": "Drawn"}, "properties": [{"id": "color", "value": {"mode": "fixed", "fixedColor": "purple"}}]},
                {"matcher": {"id": "byName", "options": "Facility Size"}, "properties": [{"id": "color", "value": {"mode": "fixed", "fixedColor": "semi-dark-purple"}}]},
                {"matcher": {"id": "byName", "options": "Ineligible"}, "properties": [{"id": "color", "value": {"mode": "fixed", "fixedColor": "semi-dark-red"}}]},
                {"matcher": {"id": "byName", "options": "Concentration Excess"}, "properties": [{"id": "color", "value": {"mode": "fixed", "fixedColor": "orange"}}]}
            ]
        }
    }


def _sofom_eligibility_bargauge(gridpos):
    return {
        "type": "bargauge",
        "title": "MX SOFOM — Eligibility & BB Detail",
        "id": 205,
        "gridPos": gridpos,
        "targets": [
            {"expr": "jeeves_cm_mx_sofom_total_receivables_usd", "instant": True, "legendFormat": "Total Receivables (Transferred)", "refId": "A"},
            {"expr": "jeeves_cm_mx_sofom_eligible_usd",          "instant": True, "legendFormat": "Eligible", "refId": "B"},
            {"expr": "jeeves_cm_mx_sofom_ineligible_usd",        "instant": True, "legendFormat": "Ineligible", "refId": "C"},
            {"expr": "jeeves_cm_mx_sofom_receivable_bb_usd",     "instant": True, "legendFormat": "Receivable BB", "refId": "D"},
            {"expr": "jeeves_cm_mx_sofom_cash_bb_usd",           "instant": True, "legendFormat": "Cash BB (offset)", "refId": "E"},
            {"expr": "jeeves_cm_mx_sofom_swap_bb_usd",           "instant": True, "legendFormat": "Swap BB", "refId": "F"},
            {"expr": "jeeves_cm_mx_sofom_borrowing_base_usd",    "instant": True, "legendFormat": "Borrowing Base (canonical L81)", "refId": "G"},
            {"expr": "jeeves_cm_mx_sofom_facility_size_usd",     "instant": True, "legendFormat": "Facility Size", "refId": "H"},
            {"expr": "jeeves_cm_mx_sofom_drawn_usd",             "instant": True, "legendFormat": "Drawn", "refId": "I"},
            {"expr": "jeeves_cm_mx_sofom_available_usd",         "instant": True, "legendFormat": "Availability", "refId": "J"}
        ],
        "options": {
            "reduceOptions": {"calcs": ["lastNotNull"]},
            "orientation": "horizontal",
            "displayMode": "gradient",
            "showUnfilled": True,
            "valueMode": "color"
        },
        "fieldConfig": {
            "defaults": {
                "unit": "currencyUSD", "decimals": 0,
                "color": {"mode": "thresholds"},
                "thresholds": {"mode": "absolute", "steps": [{"color": "blue", "value": None}]}
            },
            "overrides": [
                {"matcher": {"id": "byName", "options": "Availability"}, "properties": [
                    {"id": "color", "value": {"mode": "thresholds"}},
                    {"id": "thresholds", "value": {"mode": "absolute", "steps": [
                        {"color": "red", "value": None}, {"color": "yellow", "value": 2_000_000}, {"color": "green", "value": 10_000_000}
                    ]}}
                ]},
                {"matcher": {"id": "byName", "options": "Drawn"}, "properties": [{"id": "color", "value": {"mode": "fixed", "fixedColor": "purple"}}]},
                {"matcher": {"id": "byName", "options": "Facility Size"}, "properties": [{"id": "color", "value": {"mode": "fixed", "fixedColor": "semi-dark-purple"}}]},
                {"matcher": {"id": "byName", "options": "Ineligible"}, "properties": [{"id": "color", "value": {"mode": "fixed", "fixedColor": "semi-dark-red"}}]}
            ]
        }
    }


for old_id, builder in ((105, _bridge_eligibility_bargauge), (205, _sofom_eligibility_bargauge)):
    old = next((x for x in d["panels"] if x.get("id") == old_id), None)
    if not old:
        continue
    new = builder(old["gridPos"])
    idx = d["panels"].index(old)
    d["panels"][idx] = new

# ── 3. Bigger, more readable alerts row ─────────────────────────
alert_panel = next((x for x in d["panels"] if x.get("id") == 9001), None)
if alert_panel:
    alert_panel["gridPos"]["h"] = 6  # taller
    alert_panel["options"].update({
        "textMode": "name",
        "graphMode": "none",
        "colorMode": "background",
        "justifyMode": "center",
        "orientation": "vertical",  # stack chips vertically — easier to read
        "noValue": "✓ All clear — no active alerts"
    })

# ── 4. Sort Bank Accounts by country (desc) and improve bargauge legibility
bank_panel = next((x for x in d["panels"] if x.get("id") == 401), None)
if bank_panel:
    bank_panel["targets"][0]["expr"] = "sort_desc(jeeves_cm_bank_balance_usd)"
    bank_panel["options"]["displayMode"] = "lcd"  # higher contrast on small values

# Top 10 debtors — sort descending too
debtor_panel = next((x for x in d["panels"] if x.get("id") == 304), None)
if debtor_panel:
    debtor_panel["targets"][0]["expr"] = "topk(10, jeeves_cm_top_debtor_balance_usd)"
    debtor_panel["options"]["displayMode"] = "lcd"

# ── 5. Headroom column — keep cell background but make text white & bold
for cov_id in (601, 602):
    panel = next((x for x in d["panels"] if x.get("id") == cov_id), None)
    if not panel:
        continue
    for ov in panel["fieldConfig"]["overrides"]:
        if ov.get("matcher", {}).get("options") == "Headroom %":
            ov["properties"] = [
                {"id": "unit", "value": "percent"},
                {"id": "decimals", "value": 2},
                {"id": "custom.align", "value": "right"},
                {"id": "custom.cellOptions", "value": {"type": "color-background", "mode": "gradient"}},
                {"id": "color", "value": {"mode": "thresholds"}},
                {"id": "thresholds", "value": {"mode": "absolute", "steps": [
                    {"color": "red", "value": None},
                    {"color": "orange", "value": 1},
                    {"color": "yellow", "value": 5},
                    {"color": "green", "value": 10}
                ]}}
            ]

# ── 6. Consistent KPI formatting — display "$X.XM" in value, drop redundant title units
# Hero + facility stat panels: standardize displayName to remove "($M)" suffix since value text shows the unit
def _standardize_stat(panel, label):
    fc = panel.setdefault("fieldConfig", {}).setdefault("defaults", {})
    fc["displayName"] = label
    fc.setdefault("unit", "short")
    fc["decimals"] = 2
    # Show value AND name; center-justify so the value reads big and clean
    panel["options"].update({
        "colorMode": "value",
        "graphMode": "none",
        "textMode": "value_and_name",
        "justifyMode": "center",
        "reduceOptions": {"calcs": ["lastNotNull"]}
    })


_stat_relabel = {
    2: "Total Drawn ($M)",
    3: "Total Available ($M)",
    4: "Total Facility ($M)",
    101: "Drawn ($M)",
    102: "Availability ($M)",
    103: "Borrowing Base ($M)",
    201: "Drawn ($M)",
    202: "Availability ($M)",
    203: "Borrowing Base ($M)"
}
for pid, label in _stat_relabel.items():
    panel = next((x for x in d["panels"] if x.get("id") == pid), None)
    if not panel or panel.get("type") != "stat":
        continue
    _standardize_stat(panel, label)

# ── 7. Collection Cash — show $0 cleaner with a note
coll = next((x for x in d["panels"] if x.get("id") == 207), None)
if coll:
    coll["options"]["textMode"] = "value_and_name"
    coll["fieldConfig"]["defaults"]["displayName"] = "Collection Cash (M MXN)"
    coll["fieldConfig"]["defaults"]["noValue"] = "—"

d["version"] = 15
p.write_text(json.dumps(d, indent=2, ensure_ascii=False), encoding="utf-8")
print(f"Dashboard polished. Version {d['version']}, panels: {len(d['panels'])}")
