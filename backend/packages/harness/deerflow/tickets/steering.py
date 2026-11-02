"""Per-thread steering inbox and Source heartbeats (Super-Agent proposal §3).

Steering: when Brian writes into a thread whose run is still working, the
gateway drops the message here instead of queuing a second run;
``SteeringMiddleware`` pulls it into the running conversation before the next
model call. One file per message so a writer (gateway) and a reader (LangGraph)
never share a file; a reader claims a message with an atomic rename.

Heartbeats: each Source (slack, gmail) stamps its last event so the watchdog
can tell when one goes quiet (the Gmail watch was dead 33 days unnoticed).

Stdlib only.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path


def _base() -> Path:
    env = os.environ.get("DEERFLOW_STATE_DIR")
    if env:
        return Path(env)
    return Path(__file__).resolve().parents[4] / ".deer-flow"


_SAFE = re.compile(r"[^A-Za-z0-9_.-]")


def _inbox(thread_id: str, base: Path | None = None) -> Path:
    return (base or _base()) / "steering" / _SAFE.sub("_", thread_id)


def push(thread_id: str, text: str, *, source: str = "slack", base: Path | None = None) -> Path:
    d = _inbox(thread_id, base)
    d.mkdir(parents=True, exist_ok=True)
    name = f"{time.time_ns()}-{uuid.uuid4().hex[:6]}"
    tmp = d / (name + ".tmp")
    tmp.write_text(json.dumps({"ts": datetime.now(UTC).isoformat(timespec="seconds"), "source": source, "text": text}), encoding="utf-8")
    final = d / (name + ".json")
    os.replace(tmp, final)
    return final


def pending(thread_id: str, base: Path | None = None) -> list[Path]:
    d = _inbox(thread_id, base)
    if not d.exists():
        return []
    return sorted(d.glob("*.json"))


def pop_all(thread_id: str, base: Path | None = None) -> list[dict]:
    """Claim and return every pending message, oldest first."""
    out = []
    for f in pending(thread_id, base):
        claimed = f.with_suffix(".consumed")
        try:
            os.replace(f, claimed)
        except OSError:
            continue  # another reader took it
        try:
            out.append(json.loads(claimed.read_text(encoding="utf-8")))
        except Exception:
            continue
    return out


# -- Source heartbeats -------------------------------------------------------


def _hb_path(base: Path | None = None) -> Path:
    return (base or _base()) / "source_heartbeats.json"


def beat(source: str, base: Path | None = None) -> None:
    """Best-effort; never raises."""
    try:
        p = _hb_path(base)
        p.parent.mkdir(parents=True, exist_ok=True)
        data = read_beats(base)
        data[source] = datetime.now(UTC).isoformat(timespec="seconds")
        fd, tmp = tempfile.mkstemp(dir=p.parent, suffix=".tmp")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f)
        os.replace(tmp, p)
    except Exception:
        pass


def read_beats(base: Path | None = None) -> dict[str, str]:
    try:
        return json.loads(_hb_path(base).read_text(encoding="utf-8"))
    except Exception:
        return {}
