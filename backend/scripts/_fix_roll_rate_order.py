"""Update roll rate matrix panels to:
- Sort rows in DPD-severity order via prefixed bucket names from SQL
- Strip the '01 ', '02 ', etc. prefix in display
- Use new column ordering including separate Paid Off and Charged Off
"""
import json
from pathlib import Path

p = Path("C:/Jeeves/redshift-bot/monitoring/grafana/dashboards/capital-markets.json")
d = json.loads(p.read_text(encoding="utf-8"))

# New column ordering (matches SQL prefixed labels — after groupingToMatrix
# the column headers are the to_bucket values like '01 Current', etc.)
bucket_order = {
    "from_bucket\\to_bucket": 0,
    "01 Current": 1,
    "02 1-30 DPD": 2,
    "03 31-60 DPD": 3,
    "04 61-90 DPD": 4,
    "05 90+ DPD": 5,
    "06 Paid Off": 6,
    "07 Charged Off": 7,
}

# Regex to strip "0N " prefix from both column names and the row-label values
strip_prefix_regex = "^\\d{2} "

for panel_id in (701, 702):
    panel = next((x for x in d["panels"] if x.get("id") == panel_id), None)
    if not panel:
        continue
    panel["transformations"] = [
        {"id": "organize", "options": {
            "excludeByName": {"Time": True, "__name__": True, "instance": True, "job": True}
        }},
        {"id": "groupingToMatrix", "options": {
            "columnField": "to_bucket",
            "rowField": "from_bucket",
            "valueField": "Value",
            "emptyValue": "zero"
        }},
        # Order columns by DPD severity then Paid Off then Charged Off
        {"id": "organize", "options": {"indexByName": dict(bucket_order)}},
        # Strip "0N " prefix from column headers
        {"id": "renameByRegex", "options": {"regex": strip_prefix_regex, "renamePattern": ""}},
        # Strip "0N " prefix from the row-label column VALUES
        {"id": "findReplaceByRegex", "options": {
            "fields": ["from_bucket\\to_bucket"],
            "regex": strip_prefix_regex,
            "replace": ""
        }},
    ]

d["version"] = 17
p.write_text(json.dumps(d, indent=2, ensure_ascii=False), encoding="utf-8")
print(f"Roll rate panels updated for ordered rows + Paid/Charged Off split. Version {d['version']}.")
