"""Simplify the roll-rate panel transformations: drop the failing
findReplaceByRegex and the indexByName organize. Rely on natural sort of
prefixed labels, and use renameByRegex to strip prefixes from COLUMN headers.
Row labels retain the '0N ' prefix — acceptable for a glance read since
they're already in the right order."""
import json
from pathlib import Path

p = Path("C:/Jeeves/redshift-bot/monitoring/grafana/dashboards/capital-markets.json")
d = json.loads(p.read_text(encoding="utf-8"))

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
        # Strip the leading "NN " sort prefix from column headers AFTER the matrix.
        # Row label column is "from_bucket\to_bucket" — values keep the prefix
        # which matches the column order for visual symmetry.
        {"id": "renameByRegex", "options": {"regex": "^\\d{2} ", "renamePattern": ""}},
    ]

d["version"] = 18
p.write_text(json.dumps(d, indent=2, ensure_ascii=False), encoding="utf-8")
print("Roll rate transformations simplified.")
