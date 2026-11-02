"""Shared cron state-file helpers (P2-4 / GW-F11).

State writes across crons were non-atomic (a kill mid-write corrupts the
JSON) and corruption recovery was inconsistent — some crons raised forever,
slack_dm_monitor reset its watermark and re-dispatched a storm. Canonical
behavior here:

- atomic_write_json: temp file + os.replace (atomic on Windows + POSIX).
- load_json_state: on corrupt JSON, QUARANTINE the file to
  <name>.corrupt-<ts> (evidence preserved, no reparse loop) and return the
  default — callers get a clean, explicit fresh-start instead of a crash
  loop or a silent reset.
"""

import json
import logging
import os
import time
from pathlib import Path

log = logging.getLogger("shared.cron_state")


def atomic_write_json(path, data) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(p.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, default=str)
    os.replace(tmp, p)


def load_json_state(path, default=None):
    """Load a JSON state file; quarantine-and-default on corruption."""
    p = Path(path)
    if not p.exists():
        return {} if default is None else default
    try:
        with p.open(encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, UnicodeDecodeError) as e:
        quarantine = p.with_name(p.name + ".corrupt-" + time.strftime("%Y%m%d-%H%M%S"))
        try:
            os.replace(p, quarantine)
            log.error("Corrupt state file %s (%s) — quarantined to %s", p, e, quarantine)
        except OSError:
            log.error("Corrupt state file %s (%s) — could not quarantine", p, e)
        return {} if default is None else default
