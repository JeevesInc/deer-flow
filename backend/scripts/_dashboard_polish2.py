"""Second polish pass — finish the remaining items:
1. SOFOM Eligibility bargauge: +1 row height so 'Availability' isn't truncated
2. Shorten 'Borrowing Base (canonical L81)' to just 'Borrowing Base'
3. Collection Cash panel: add 'as of' subtitle showing the BB file's source_mtime
4. SOFOM BB Component Stack: same +1 row height
5. Add USDMXN refresh date subtitle
6. Move row titles so they're consistent
"""
import json
from pathlib import Path

p = Path("C:/Jeeves/redshift-bot/monitoring/grafana/dashboards/capital-markets.json")
d = json.loads(p.read_text(encoding="utf-8"))

# 1. SOFOM Eligibility & BB Detail (id 205) — bump height from 8 to 10
sofom_elig = next((x for x in d["panels"] if x.get("id") == 205), None)
if sofom_elig:
    sofom_elig["gridPos"]["h"] = 10
    # Shorten the BB legend
    for t in sofom_elig.get("targets", []):
        if t.get("expr") == "jeeves_cm_mx_sofom_borrowing_base_usd":
            t["legendFormat"] = "Borrowing Base"

# 2. SOFOM BB Component Stack (id 206) — same +2 height for parity
sofom_stack = next((x for x in d["panels"] if x.get("id") == 206), None)
if sofom_stack:
    sofom_stack["gridPos"]["h"] = 10

# Bridge Eligibility (id 105) — match SOFOM height for consistency
bridge_elig = next((x for x in d["panels"] if x.get("id") == 105), None)
if bridge_elig:
    bridge_elig["gridPos"]["h"] = 10

# Bridge Receivables Composition (id 106) — match
bridge_comp = next((x for x in d["panels"] if x.get("id") == 106), None)
if bridge_comp:
    bridge_comp["gridPos"]["h"] = 10

# Shift everything below US Bridge section (y starting around 10) down by 2
# to make room. The MX SOFOM row starts at y=18 per current layout; bump it.
# Actually find the y of the next row after BRIDGE and shift below.
us_row = next((x for x in d["panels"] if x.get("id") == 100 and x.get("type") == "row"), None)
us_y = us_row["gridPos"]["y"] if us_row else 5
bridge_end_y = us_y + 1 + 4 + 10  # row + KPI row (h=4) + new tall detail panels (h=10)
# Shift any panel below this if its y < bridge_end_y
# Actually simpler: find SOFOM row y; if it's < bridge_end_y, shift everything from there down
sofom_row = next((x for x in d["panels"] if x.get("id") == 200 and x.get("type") == "row"), None)
if sofom_row and sofom_row["gridPos"]["y"] < bridge_end_y:
    shift = bridge_end_y - sofom_row["gridPos"]["y"]
    for panel in d["panels"]:
        if panel.get("gridPos", {}).get("y", 0) >= sofom_row["gridPos"]["y"]:
            panel["gridPos"]["y"] += shift

# Also shift everything below MX SOFOM if it now overlaps
sofom_end_y = sofom_row["gridPos"]["y"] + 1 + 4 + 10  # row + KPI + detail (10) — but plus Collection Cash too
# Find Covenants row (id 600) — make sure it's above sofom_end_y + collection cash row
cov_row = next((x for x in d["panels"] if x.get("id") == 600 and x.get("type") == "row"), None)
# Collection Cash (207) and USDMXN (208) are within SOFOM section at h=4 each
# Recompute SOFOM end: row(1) + KPI row(4) + detail panels(10) + cash/usdmxn(4) = y + 19
sofom_full_end = sofom_row["gridPos"]["y"] + 19
if cov_row and cov_row["gridPos"]["y"] < sofom_full_end:
    shift2 = sofom_full_end - cov_row["gridPos"]["y"]
    for panel in d["panels"]:
        if panel.get("gridPos", {}).get("y", 0) >= cov_row["gridPos"]["y"]:
            panel["gridPos"]["y"] += shift2

# 3. Collection Cash (id 207) — load BB file's source_mtime as subtitle if available
# Grafana stat panels don't have subtitles, but we can put it in displayName.
# Better: load state file to inject the date right now (static label).
state_path = Path("C:/Jeeves/redshift-bot/deer-flow/backend/.deer-flow/_bb_metrics_state.json")
sofom_as_of = None
if state_path.exists():
    try:
        state = json.loads(state_path.read_text(encoding="utf-8"))
        mtime = (state.get("mx_sofom") or {}).get("source_mtime", "")
        if mtime:
            sofom_as_of = mtime[:10]
    except Exception:
        pass

coll = next((x for x in d["panels"] if x.get("id") == 207), None)
if coll:
    suffix = f"  ·  as of {sofom_as_of}" if sofom_as_of else ""
    coll["title"] = f"Collection Cash (Prerecycling){suffix}"
    coll["fieldConfig"]["defaults"]["displayName"] = "M MXN"
    coll["fieldConfig"]["defaults"]["noValue"] = "—"
    coll["options"]["textMode"] = "value_and_name"

usdmxn = next((x for x in d["panels"] if x.get("id") == 208), None)
if usdmxn:
    suffix = f"  ·  as of {sofom_as_of}" if sofom_as_of else ""
    usdmxn["title"] = f"USDMXN Spot{suffix}"
    usdmxn["fieldConfig"]["defaults"]["displayName"] = "USDMXN"

d["panels"].sort(key=lambda p_: (p_["gridPos"]["y"], p_["gridPos"]["x"]))
d["version"] = 16
p.write_text(json.dumps(d, indent=2, ensure_ascii=False), encoding="utf-8")
print(f"Dashboard polish v2 applied. Version {d['version']}.")
