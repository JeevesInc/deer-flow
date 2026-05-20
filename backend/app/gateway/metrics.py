"""Prometheus metrics for the DeerFlow gateway.

Exposes lightweight gauges/counters over /metrics so the local Grafana stack
(monitoring/docker-compose.yml at repo root) can render an at-a-glance
dashboard.  All collectors here are pull-based: the metric is computed on
each scrape so there are no background timers competing with cron jobs.
"""

from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx
from prometheus_client import CollectorRegistry, Gauge

registry = CollectorRegistry()

g_langgraph_up = Gauge(
    "deerflow_langgraph_up",
    "Whether the LangGraph dev server responds 200 on /ok (1=yes, 0=no).",
    registry=registry,
)
g_checkpoints_db_bytes = Gauge(
    "deerflow_checkpoints_db_bytes",
    "Size of LangGraph checkpoints.db in bytes.",
    registry=registry,
)
g_active_runs = Gauge(
    "deerflow_active_autonomous_runs",
    "Currently in-flight autonomous_dispatch runs.",
    registry=registry,
)
g_dispatch_total = Gauge(
    "deerflow_dispatch_events_total",
    "Cumulative count of dispatch audit events by event type.",
    labelnames=("event",),
    registry=registry,
)
g_dispatch_last_ts = Gauge(
    "deerflow_dispatch_last_event_timestamp",
    "Unix timestamp of most recent dispatch audit event by event type.",
    labelnames=("event",),
    registry=registry,
)
g_thread_map_size = Gauge(
    "deerflow_channel_thread_mappings",
    "Number of Slack/Telegram chat-to-thread mappings in store.json.",
    registry=registry,
)
g_threads_by_status = Gauge(
    "deerflow_langgraph_threads",
    "Count of LangGraph threads by status (busy/idle/error/interrupted).",
    labelnames=("status",),
    registry=registry,
)
g_busy_thread_age = Gauge(
    "deerflow_langgraph_busy_thread_age_seconds",
    "Age in seconds of the oldest currently-busy LangGraph thread. "
    "A high value means a run has been in flight for a long time and may be stuck.",
    registry=registry,
)
g_scrape_seconds = Gauge(
    "deerflow_metrics_scrape_seconds",
    "Wall-clock seconds spent computing this scrape.",
    registry=registry,
)


def _state_dir() -> Path:
    # backend/app/gateway/metrics.py → backend/
    return Path(__file__).resolve().parent.parent.parent / ".deer-flow"


def _refresh_db_size() -> None:
    db = _state_dir() / "checkpoints.db"
    try:
        g_checkpoints_db_bytes.set(db.stat().st_size if db.exists() else 0)
    except OSError:
        g_checkpoints_db_bytes.set(0)


def _refresh_langgraph_up(langgraph_url: str) -> None:
    try:
        with httpx.Client(timeout=2.0) as client:
            resp = client.get(f"{langgraph_url.rstrip('/')}/ok")
            g_langgraph_up.set(1 if resp.status_code == 200 else 0)
    except Exception:
        g_langgraph_up.set(0)


def _refresh_active_runs() -> None:
    # autonomous_dispatch is loaded by file-path from the cron supervisor,
    # so it doesn't live on sys.modules under a stable name. Import lazily
    # from the file path and read the module-level counter.
    try:
        path = (
            Path(__file__).resolve().parent.parent.parent.parent
            / "skills"
            / "custom"
            / "_shared"
            / "autonomous_dispatch.py"
        )
        if not path.exists():
            g_active_runs.set(0)
            return
        import importlib.util

        spec = importlib.util.spec_from_file_location("_dispatch_for_metrics", str(path))
        if spec and spec.loader:
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            g_active_runs.set(getattr(mod, "_active_runs", 0))
    except Exception:
        g_active_runs.set(0)


def _refresh_dispatch_audit() -> None:
    audit = _state_dir() / "dispatch_audit.jsonl"
    if not audit.exists():
        return
    counts: dict[str, int] = {}
    last_ts: dict[str, float] = {}
    try:
        with audit.open("r", encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec: dict[str, Any] = json.loads(line)
                except ValueError:
                    continue
                event = str(rec.get("event", "unknown"))
                counts[event] = counts.get(event, 0) + 1
                ts_iso = rec.get("ts")
                if isinstance(ts_iso, str):
                    try:
                        from datetime import datetime

                        ts = datetime.fromisoformat(ts_iso.replace("Z", "+00:00")).timestamp()
                        if ts > last_ts.get(event, 0.0):
                            last_ts[event] = ts
                    except ValueError:
                        pass
    except OSError:
        return
    for event, count in counts.items():
        g_dispatch_total.labels(event=event).set(count)
    for event, ts in last_ts.items():
        g_dispatch_last_ts.labels(event=event).set(ts)


def _refresh_thread_map() -> None:
    store = _state_dir() / "channels" / "store.json"
    if not store.exists():
        # store may live directly in .deer-flow/store.json on older layouts
        store = _state_dir() / "store.json"
    try:
        with store.open("r", encoding="utf-8") as f:
            data = json.load(f)
        # store schema: {channel:chat[:topic] -> thread_id}
        g_thread_map_size.set(len(data) if isinstance(data, dict) else 0)
    except (OSError, ValueError):
        g_thread_map_size.set(0)


def _parse_ts(ts: str | None) -> datetime | None:
    if not isinstance(ts, str):
        return None
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return None


def _collect_thread_status(langgraph_url: str) -> dict[str, Any]:
    """Returns {counts: {status: n}, oldest_busy_age_seconds: float|None, busy_threads: [...]}.

    Pure-sync via httpx so it works from both the FastAPI async /metrics
    handler (which already runs inside a loop and can't call asyncio.run())
    and from sync admin scripts.
    """
    out: dict[str, Any] = {
        "counts": {s: 0 for s in ("busy", "idle", "error", "interrupted")},
        "oldest_busy_age_seconds": None,
        "busy_threads": [],
    }
    now = datetime.now(timezone.utc)
    url = f"{langgraph_url.rstrip('/')}/threads/search"
    try:
        with httpx.Client(timeout=5.0) as client:
            for status in ("busy", "idle", "error", "interrupted"):
                resp = client.post(url, json={"status": status, "limit": 200})
                if resp.status_code != 200:
                    continue
                threads = resp.json()
                if not isinstance(threads, list):
                    continue
                out["counts"][status] = len(threads)
                if status == "busy":
                    ages: list[tuple[float, dict[str, Any]]] = []
                    for t in threads:
                        ts = _parse_ts(t.get("updated_at") or t.get("created_at"))
                        if ts is None:
                            continue
                        age = (now - ts).total_seconds()
                        ages.append((age, {
                            "thread_id": t.get("thread_id"),
                            "age_seconds": round(age, 1),
                            "updated_at": t.get("updated_at"),
                            "created_at": t.get("created_at"),
                        }))
                    ages.sort(key=lambda x: x[0], reverse=True)
                    if ages:
                        out["oldest_busy_age_seconds"] = ages[0][0]
                    out["busy_threads"] = [a[1] for a in ages]
    except Exception:
        return out
    return out


def _refresh_langgraph_threads(langgraph_url: str) -> None:
    try:
        info = _collect_thread_status(langgraph_url)
    except Exception:
        return
    for status, count in info["counts"].items():
        g_threads_by_status.labels(status=status).set(count)
    oldest = info["oldest_busy_age_seconds"]
    g_busy_thread_age.set(oldest if oldest is not None else 0)


def refresh_all(langgraph_url: str = "http://localhost:2024") -> None:
    """Recompute all metrics. Called from the /metrics handler."""
    started = time.monotonic()
    _refresh_db_size()
    _refresh_langgraph_up(langgraph_url)
    _refresh_active_runs()
    _refresh_dispatch_audit()
    _refresh_thread_map()
    _refresh_langgraph_threads(langgraph_url)
    g_scrape_seconds.set(time.monotonic() - started)
