"""Local run-trace sink.

Why local rather than LangSmith/Langfuse: there is no hosted-tracing credential
on this box, and the whole point of these traces is to become eval material for
work that already runs offline. A JSONL file on disk is enough to sample, grade
and replay from — and it keeps counterparty email content, loan tape numbers and
Slack DMs off a third-party service, which matters more here than a nice UI.

Layout:
    .deer-flow/traces/YYYY-MM-DD.jsonl     one JSON object per model call

Design constraints:
  * Never break a run. Every public entry point swallows its own errors — a
    tracing bug must not surface as an agent failure.
  * Never grow without bound. `.deer-flow` only ever grew until the retention
    janitor was added (P2-7); this sink ships with its own day-file rotation and
    a `prune()` the janitor can call.
  * Never block the model call meaningfully. Writes are small appends under a
    lock; payloads are truncated at capture time, not at read time.

Reading traces back:
    from deerflow.agents.tracing.trace_sink import read_traces
    for rec in read_traces(days=7):
        ...
"""

from __future__ import annotations

import json
import logging
import os
import threading
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# Tracing is on by default. It is cheap, and the failure mode this whole effort
# exists to fix is "we have no record of what the agent actually did".
_ENABLED_DEFAULT = "1"

# Per-field capture caps. Generous enough to reconstruct what happened, small
# enough that a busy day stays in the tens of MB rather than the hundreds.
MAX_TEXT_CHARS = 4000
MAX_PROMPT_CHARS = 2000
MAX_TOOL_ARGS_CHARS = 600
RETENTION_DAYS = int(os.environ.get("DEERFLOW_TRACE_RETENTION_DAYS", "30"))

_lock = threading.Lock()
_step_counters: dict[str, int] = {}


def enabled() -> bool:
    return os.environ.get("DEERFLOW_TRACING", _ENABLED_DEFAULT).lower() not in (
        "0",
        "false",
        "no",
        "off",
    )


def trace_dir() -> Path:
    """Resolve .deer-flow/traces/, honoring the same override the rest of the
    stack uses so tests and alternate deployments do not write into the live
    runtime directory."""
    override = os.environ.get("DEERFLOW_STATE_DIR")
    if override:
        base = Path(override)
    else:
        # packages/harness/deerflow/agents/tracing/ -> backend/
        base = Path(__file__).resolve().parents[5] / ".deer-flow"
    return base / "traces"


def _today_file() -> Path:
    d = trace_dir()
    d.mkdir(parents=True, exist_ok=True)
    return d / f"{datetime.now(UTC):%Y-%m-%d}.jsonl"


def truncate(value: Any, limit: int = MAX_TEXT_CHARS) -> str:
    """Stringify and clip, marking the clip so a reader never mistakes a
    truncated payload for the model's real output."""
    if value is None:
        return ""
    s = value if isinstance(value, str) else str(value)
    if len(s) <= limit:
        return s
    return s[:limit] + f"...[truncated {len(s) - limit} chars]"


def next_step(run_id: str) -> int:
    """Monotonic per-run step number, so a trace can be replayed in order even
    though records from concurrent runs interleave in one file."""
    with _lock:
        n = _step_counters.get(run_id, 0) + 1
        _step_counters[run_id] = n
        # Bound the counter map; long-lived gateways see thousands of threads.
        if len(_step_counters) > 5000:
            for k in list(_step_counters)[:2500]:
                _step_counters.pop(k, None)
        return n


def write(record: dict) -> None:
    """Append one trace record. Silent on failure by design."""
    if not enabled():
        return
    try:
        record.setdefault("ts", datetime.now(UTC).isoformat())
        line = json.dumps(record, ensure_ascii=False, default=str)
        with _lock:
            with _today_file().open("a", encoding="utf-8") as f:
                f.write(line + "\n")
    except Exception:  # noqa: BLE001 — tracing must never fail a run
        logger.debug("trace write failed", exc_info=True)


def read_traces(days: int = 7, since: datetime | None = None) -> Iterator[dict]:
    """Yield trace records from the last `days` day-files, oldest first.

    Malformed lines are skipped rather than raising: the file is appended to
    concurrently, so a torn final line during a live run is expected.
    """
    d = trace_dir()
    if not d.exists():
        return
    cutoff = since or (datetime.now(UTC) - timedelta(days=days))
    for path in sorted(d.glob("*.jsonl")):
        try:
            file_day = datetime.strptime(path.stem, "%Y-%m-%d").replace(tzinfo=UTC)
        except ValueError:
            continue
        # +1 day: a file dated the cutoff day still holds records after it.
        if file_day < cutoff - timedelta(days=1):
            continue
        try:
            with path.open("r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rec = json.loads(line)
                    except ValueError:
                        continue
                    ts = rec.get("ts", "")
                    try:
                        if ts and datetime.fromisoformat(ts) < cutoff:
                            continue
                    except ValueError:
                        pass
                    yield rec
        except OSError:
            continue


def prune(retention_days: int | None = None) -> int:
    """Delete day-files older than the retention window. Returns files removed.

    Called by the retention-janitor cron; also safe to call ad hoc.
    """
    keep = retention_days if retention_days is not None else RETENTION_DAYS
    d = trace_dir()
    if not d.exists():
        return 0
    cutoff = (datetime.now(UTC) - timedelta(days=keep)).date()
    removed = 0
    for path in d.glob("*.jsonl"):
        try:
            day = datetime.strptime(path.stem, "%Y-%m-%d").date()
        except ValueError:
            continue
        if day < cutoff:
            try:
                path.unlink()
                removed += 1
            except OSError:
                logger.debug("could not remove trace file %s", path)
    return removed


def stats(days: int = 1) -> dict:
    """Summary counters over recent traces — feeds the Grafana gauges."""
    total = errors = tool_calls = 0
    in_tokens = out_tokens = 0
    latencies: list[float] = []
    for rec in read_traces(days=days):
        total += 1
        if rec.get("error"):
            errors += 1
        tool_calls += len(rec.get("output", {}).get("tool_calls", []) or [])
        usage = rec.get("usage") or {}
        in_tokens += int(usage.get("input_tokens") or 0)
        out_tokens += int(usage.get("output_tokens") or 0)
        lat = rec.get("latency_ms")
        if isinstance(lat, (int, float)):
            latencies.append(float(lat))
    latencies.sort()
    return {
        "model_calls": total,
        "errors": errors,
        "tool_calls": tool_calls,
        "input_tokens": in_tokens,
        "output_tokens": out_tokens,
        "p50_latency_ms": latencies[len(latencies) // 2] if latencies else 0.0,
        "p95_latency_ms": latencies[int(len(latencies) * 0.95)] if latencies else 0.0,
    }
