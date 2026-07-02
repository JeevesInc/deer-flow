"""Prometheus metrics for the DeerFlow gateway.

Exposes lightweight gauges/counters over /metrics so the local Grafana stack
(monitoring/docker-compose.yml at repo root) can render an at-a-glance
dashboard.  All collectors here are pull-based: metrics are computed on each
scrape so there are no background timers competing with cron jobs.  The one
exception is the paginated LangGraph thread sweep, which is memoized for
_THREAD_STATUS_TTL_SEC to stay under the Prometheus scrape timeout — still
pull-driven, just rate-limited.
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

# ── Cron supervisor gauges (fed by app/gateway/cron_supervisor._supervisors) ──
g_cron_running = Gauge(
    "deerflow_cron_running",
    "1 if the cron's supervised thread is alive and has not exited; else 0.",
    labelnames=("cron",),
    registry=registry,
)
g_cron_crash_total = Gauge(
    "deerflow_cron_crash_total",
    "Cumulative crash count for the cron since gateway start.",
    labelnames=("cron",),
    registry=registry,
)
g_cron_exited = Gauge(
    "deerflow_cron_exited",
    "1 if the cron's run_loop returned (abnormal — the cron is not running).",
    labelnames=("cron",),
    registry=registry,
)
g_cron_heartbeat_ts = Gauge(
    "deerflow_cron_last_heartbeat_ts",
    "Unix ts of the cron's last heartbeat (target (re)start, or record_heartbeat() "
    "if the cron opted in). Alert on staleness > 2x the cron's expected interval.",
    labelnames=("cron",),
    registry=registry,
)

# ── Capital Markets gauges (fed by .deer-flow/_cap_markets_state.json) ──
# State JSON is written by backend/scripts/cap_markets_metrics_writer.py,
# which dashboard_full_update.py calls at end of each refresh.
g_cm_us_drawn = Gauge("jeeves_cm_us_bridge_drawn_usd", "US Bridge — total drawn (USD).", registry=registry)
g_cm_us_avail = Gauge("jeeves_cm_us_bridge_available_usd", "US Bridge — availability (USD).", registry=registry)
g_cm_us_elig = Gauge("jeeves_cm_us_bridge_eligible_usd", "US Bridge — eligible receivables (USD).", registry=registry)
g_cm_us_bb = Gauge("jeeves_cm_us_bridge_borrowing_base_usd", "US Bridge — borrowing base cap (USD).", registry=registry)
g_cm_us_facility = Gauge("jeeves_cm_us_bridge_facility_size_usd", "US Bridge — facility size (USD).", registry=registry)
g_cm_us_port_total = Gauge("jeeves_cm_us_bridge_portfolio_total_usd", "US Bridge — portfolio balance (USD).", registry=registry)
g_cm_us_port_accts = Gauge("jeeves_cm_us_bridge_portfolio_accounts", "US Bridge — active accounts count.", registry=registry)
g_cm_us_port_dq30 = Gauge("jeeves_cm_us_bridge_portfolio_dq30_pct", "US Bridge — DPD 30+ delinquency rate (%).", registry=registry)
g_cm_mx_drawn = Gauge("jeeves_cm_mx_sofom_drawn_usd", "MX SOFOM — total drawn (USD-equivalent).", registry=registry)
g_cm_mx_avail = Gauge("jeeves_cm_mx_sofom_available_usd", "MX SOFOM — availability (USD-equivalent).", registry=registry)
g_cm_mx_elig = Gauge("jeeves_cm_mx_sofom_eligible_usd", "MX SOFOM — eligible receivables (USD-equivalent).", registry=registry)
g_cm_mx_bb = Gauge("jeeves_cm_mx_sofom_borrowing_base_usd", "MX SOFOM — borrowing base cap (USD-equivalent).", registry=registry)
g_cm_mx_facility = Gauge("jeeves_cm_mx_sofom_facility_size_usd", "MX SOFOM — facility size (USD-equivalent).", registry=registry)
g_cm_mx_coll = Gauge("jeeves_cm_mx_sofom_collection_cash_mxn", "MX SOFOM — prerecycling collection cash (MXN).", registry=registry)
g_cm_mx_port_total = Gauge("jeeves_cm_mx_sofom_portfolio_total_usd", "MX SOFOM — portfolio balance (USD-equivalent).", registry=registry)
g_cm_mx_port_accts = Gauge("jeeves_cm_mx_sofom_portfolio_accounts", "MX SOFOM — active accounts count.", registry=registry)
g_cm_mx_port_dq30 = Gauge("jeeves_cm_mx_sofom_portfolio_dq30_pct", "MX SOFOM — DPD 30+ delinquency rate (%).", registry=registry)

# Eligibility breakdown
g_cm_us_total_recv = Gauge("jeeves_cm_us_bridge_total_receivables_usd", "US Bridge — total receivables (USD).", registry=registry)
g_cm_us_ineligible = Gauge("jeeves_cm_us_bridge_ineligible_usd", "US Bridge — ineligible receivables (USD).", registry=registry)
g_cm_us_counted = Gauge("jeeves_cm_us_bridge_receivables_counted_usd", "US Bridge — receivables counted towards BB after concentration (USD).", registry=registry)
g_cm_us_binding_cap = Gauge("jeeves_cm_us_bridge_binding_cap_usd", "US Bridge — min(BB, facility) — the binding constraint (USD).", registry=registry)
g_cm_us_utilization = Gauge("jeeves_cm_us_bridge_utilization_pct", "US Bridge — drawn / binding cap (%).", registry=registry)
g_cm_us_conc_total = Gauge("jeeves_cm_us_bridge_concentration_breaches_total_usd", "US Bridge — sum of concentration test breaches (USD).", registry=registry)

g_cm_mx_total_recv = Gauge("jeeves_cm_mx_sofom_total_receivables_usd", "MX SOFOM — Total Receivables (Transferred Receivables, USD).", registry=registry)
g_cm_mx_ineligible = Gauge("jeeves_cm_mx_sofom_ineligible_usd", "MX SOFOM — ineligible receivables (USD).", registry=registry)
g_cm_mx_counted = Gauge("jeeves_cm_mx_sofom_receivables_counted_usd", "MX SOFOM — receivables counted towards BB after concentration (USD).", registry=registry)
g_cm_mx_binding_cap = Gauge("jeeves_cm_mx_sofom_binding_cap_usd", "MX SOFOM — min(BB, facility) — the binding constraint (USD).", registry=registry)
g_cm_mx_utilization = Gauge("jeeves_cm_mx_sofom_utilization_pct", "MX SOFOM — drawn / binding cap (%).", registry=registry)
g_cm_mx_receivable_bb = Gauge("jeeves_cm_mx_sofom_receivable_bb_usd", "MX SOFOM — receivable component of BB (USD).", registry=registry)
g_cm_mx_cash_bb = Gauge("jeeves_cm_mx_sofom_cash_bb_usd", "MX SOFOM — cash component of BB (USD).", registry=registry)
g_cm_mx_swap_bb = Gauge("jeeves_cm_mx_sofom_swap_bb_usd", "MX SOFOM — swap value component of BB (USD).", registry=registry)
g_cm_mx_usdmxn = Gauge("jeeves_cm_mx_sofom_usdmxn_rate", "MX SOFOM — USDMXN spot exchange rate from Exhibit A L6.", registry=registry)

# Combined / cross-facility
g_cm_total_drawn = Gauge("jeeves_cm_total_drawn_usd", "Total drawn across all facilities (USD).", registry=registry)
g_cm_total_available = Gauge("jeeves_cm_total_available_usd", "Total availability across all facilities (USD).", registry=registry)
g_cm_total_facility = Gauge("jeeves_cm_total_facility_size_usd", "Total facility size across all facilities (USD).", registry=registry)

# Stratifications — global (combined across facilities)
g_cm_balance_by_country = Gauge(
    "jeeves_cm_balance_by_country_usd",
    "Combined portfolio balance by country (USD, Bridge + SOFOM).",
    labelnames=("country",), registry=registry,
)
g_cm_accounts_by_country = Gauge(
    "jeeves_cm_accounts_by_country",
    "Combined active accounts by country (Bridge + SOFOM).",
    labelnames=("country",), registry=registry,
)
g_cm_balance_by_dpd = Gauge(
    "jeeves_cm_balance_by_dpd_bucket_usd",
    "Combined portfolio balance by DPD bucket (USD).",
    labelnames=("bucket",), registry=registry,
)
g_cm_accounts_by_dpd = Gauge(
    "jeeves_cm_accounts_by_dpd_bucket",
    "Combined active accounts by DPD bucket.",
    labelnames=("bucket",), registry=registry,
)
g_cm_balance_by_product = Gauge(
    "jeeves_cm_balance_by_product_usd",
    "Combined portfolio balance by product (USD).",
    labelnames=("product",), registry=registry,
)
g_cm_top_debtor_balance = Gauge(
    "jeeves_cm_top_debtor_balance_usd",
    "Top combined debtor balance (USD) — labelled by rank, company_id, name.",
    labelnames=("rank", "company_id", "name"), registry=registry,
)
g_cm_originations_card = Gauge("jeeves_cm_originations_card_usd", "Card originations in the BB period (USD).", registry=registry)
g_cm_originations_jp = Gauge("jeeves_cm_originations_jp_usd", "Jeeves Pay originations in the BB period (USD).", registry=registry)
g_cm_originations_total = Gauge("jeeves_cm_originations_total_usd", "Total originations in the BB period (USD).", registry=registry)
g_cm_bank_balance = Gauge(
    "jeeves_cm_bank_balance_usd",
    "Combined bank account cash balance by country (USD).",
    labelnames=("country",), registry=registry,
)

# Concentration covenants — labelled per facility + test name
g_cm_covenant_actual_pct = Gauge(
    "jeeves_cm_covenant_actual_pct",
    "Concentration test actual percentage, per facility (%).",
    labelnames=("facility", "test"), registry=registry,
)
g_cm_covenant_limit_pct = Gauge(
    "jeeves_cm_covenant_limit_pct",
    "Concentration test limit percentage, per facility (%).",
    labelnames=("facility", "test"), registry=registry,
)
g_cm_covenant_excess = Gauge(
    "jeeves_cm_covenant_excess",
    "Concentration test excess (deduction) amount, per facility (USD or MXN as native).",
    labelnames=("facility", "test"), registry=registry,
)
g_cm_covenant_headroom_pct = Gauge(
    "jeeves_cm_covenant_headroom_pct",
    "Concentration test headroom (limit − actual), per facility (%).",
    labelnames=("facility", "test"), registry=registry,
)

# Alerts — boolean (0/1) signals computed from other metrics. Surfaced as a chip row.
g_cm_roll_count = Gauge(
    "jeeves_cm_roll_count",
    "Roll-rate transition count from BOP DPD bucket to EOP DPD bucket (units).",
    labelnames=("from_bucket", "to_bucket"), registry=registry,
)
g_cm_roll_pct = Gauge(
    "jeeves_cm_roll_pct",
    "Roll-rate row-normalized percentage: of loans in BOP bucket, % that ended in each EOP bucket.",
    labelnames=("from_bucket", "to_bucket"), registry=registry,
)
g_cm_roll_period = Gauge(
    "jeeves_cm_roll_period_info",
    "Roll-rate reporting period (always 1) — labels carry BOP and EOP dates.",
    labelnames=("bop_dt", "eop_dt"), registry=registry,
)

# CICO daily cash (extracted from Axel's email via OCR)
g_cm_cico_category = Gauge(
    "jeeves_cm_cico_category_usd",
    "CICO daily cash balance by category (USD-equivalent).",
    labelnames=("category",), registry=registry,
)
g_cm_cico_total_cash = Gauge("jeeves_cm_cico_total_cash_usd", "CICO total cash (USD, excluding restricted).", registry=registry)
g_cm_cico_restricted = Gauge("jeeves_cm_cico_restricted_usd", "CICO restricted deposits (USD).", registry=registry)
g_cm_cico_daca = Gauge("jeeves_cm_cico_daca_pledged_usd", "CICO DACA + pledged countries (feeds US BB).", registry=registry)
g_cm_cico_total_plus_restricted = Gauge("jeeves_cm_cico_total_cash_plus_restricted_usd", "CICO total cash + restricted (USD).", registry=registry)

g_cm_alert = Gauge(
    "jeeves_cm_alert",
    "Active alerts — 1 if condition holds, 0 if cleared. Labelled by id + severity + summary.",
    labelnames=("id", "severity", "summary"), registry=registry,
)
# Retained for backward compat — same as US Bridge today; will be removed once dashboard panels migrate.
g_cm_port_dq30 = Gauge("jeeves_cm_portfolio_dq30_pct", "[deprecated] use jeeves_cm_us_bridge_portfolio_dq30_pct", registry=registry)
g_cm_port_total = Gauge("jeeves_cm_portfolio_total_usd", "[deprecated] use jeeves_cm_us_bridge_portfolio_total_usd", registry=registry)
g_cm_port_accts = Gauge("jeeves_cm_portfolio_accounts", "[deprecated] use jeeves_cm_us_bridge_portfolio_accounts", registry=registry)
g_cm_mx_recv = Gauge("jeeves_cm_mx_sofom_receivables_mxn", "[deprecated] use jeeves_cm_mx_sofom_eligible_usd", registry=registry)
g_cm_cron_days = Gauge(
    "jeeves_cm_cron_days_ago",
    "Days since each capital-markets cron last completed (lower = healthier).",
    labelnames=("cron",),
    registry=registry,
)
g_cm_cron_last_ts = Gauge(
    "jeeves_cm_cron_last_ts",
    "Unix timestamp of each capital-markets cron's last completion.",
    labelnames=("cron",),
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


# /threads/search returns at most one page per call. Paginate to get the true
# count, but cap pages and total wall-clock so a slow LangGraph can't make the
# /metrics scrape exceed Prometheus' scrape_timeout (default 10s).
_THREAD_PAGE_SIZE = 200
_THREAD_MAX_PAGES = 50  # safety cap: counts up to 10k threads per status
_THREAD_COLLECT_BUDGET_SEC = 6.0
_THREAD_REQUEST_TIMEOUT_SEC = 4.0


def _collect_thread_status(langgraph_url: str) -> dict[str, Any]:
    """Returns {counts, collected, oldest_busy_age_seconds, busy_threads}.

    ``counts[status]`` is the true thread count (paginated, not capped at a
    single 200-row page). ``collected[status]`` is True only when that status
    was counted end-to-end; on a non-200/timeout/budget-exhausted sub-request
    the count is left at 0 and ``collected[status]`` stays False, so callers can
    skip the gauge update rather than publish a spurious zero.

    Pure-sync via httpx so it works from both the FastAPI async /metrics
    handler (which already runs inside a loop and can't call asyncio.run())
    and from sync admin scripts. A wall-clock budget keeps the whole sweep
    under the Prometheus scrape timeout even when LangGraph is slow.
    """
    statuses = ("busy", "idle", "error", "interrupted")
    out: dict[str, Any] = {
        "counts": {s: 0 for s in statuses},
        "collected": {s: False for s in statuses},
        "oldest_busy_age_seconds": None,
        "busy_threads": [],
    }
    now = datetime.now(timezone.utc)
    url = f"{langgraph_url.rstrip('/')}/threads/search"
    deadline = time.monotonic() + _THREAD_COLLECT_BUDGET_SEC
    try:
        with httpx.Client(timeout=_THREAD_REQUEST_TIMEOUT_SEC) as client:
            for status in statuses:
                total = 0
                offset = 0
                ok = True
                busy_threads: list[dict[str, Any]] = []  # busy is small; keep full set for age
                while True:
                    if time.monotonic() >= deadline:
                        ok = False
                        break
                    resp = client.post(url, json={"status": status, "limit": _THREAD_PAGE_SIZE, "offset": offset})
                    if resp.status_code != 200:
                        ok = False
                        break
                    batch = resp.json()
                    if not isinstance(batch, list):
                        ok = False
                        break
                    total += len(batch)
                    if status == "busy":
                        busy_threads.extend(batch)
                    if len(batch) < _THREAD_PAGE_SIZE:
                        break
                    offset += _THREAD_PAGE_SIZE
                    if offset // _THREAD_PAGE_SIZE >= _THREAD_MAX_PAGES:
                        break
                if not ok:
                    continue
                out["counts"][status] = total
                out["collected"][status] = True
                if status == "busy":
                    ages: list[tuple[float, dict[str, Any]]] = []
                    for t in busy_threads:
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


# Memoize the (multi-call, paginated) thread sweep so a 15s scrape interval
# doesn't hammer LangGraph 4× every 15s. Still pull-based — no background timer.
_THREAD_STATUS_TTL_SEC = 60.0
_thread_status_cache: dict[str, Any] | None = None
_thread_status_cache_at: float = 0.0


def _refresh_langgraph_threads(langgraph_url: str) -> None:
    global _thread_status_cache, _thread_status_cache_at
    nowm = time.monotonic()
    if _thread_status_cache is not None and (nowm - _thread_status_cache_at) < _THREAD_STATUS_TTL_SEC:
        info = _thread_status_cache
    else:
        try:
            info = _collect_thread_status(langgraph_url)
        except Exception:
            return
        _thread_status_cache = info
        _thread_status_cache_at = nowm
    collected = info.get("collected", {})
    for status, count in info["counts"].items():
        if collected.get(status, True):  # only publish statuses we actually counted
            g_threads_by_status.labels(status=status).set(count)
    if collected.get("busy", True):
        oldest = info["oldest_busy_age_seconds"]
        g_busy_thread_age.set(oldest if oldest is not None else 0)


def _refresh_cap_markets() -> None:
    """Load _cap_markets_state.json (written by cap_markets_metrics_writer.py)
    and project the values onto the jeeves_cm_* gauges. Values that are
    explicitly None in the state file are written as NaN so Grafana renders
    'no data' rather than 0."""
    import math
    state_file = _state_dir() / "_cap_markets_state.json"
    if not state_file.exists():
        return
    try:
        data = json.loads(state_file.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return

    def _set_or_nan(gauge, value):
        if value is None:
            gauge.set(math.nan)
        else:
            try:
                gauge.set(float(value))
            except (TypeError, ValueError):
                gauge.set(math.nan)

    us = data.get("us_bridge") or {}
    if "total_drawn" in us:        _set_or_nan(g_cm_us_drawn, us["total_drawn"])
    if "availability" in us:       _set_or_nan(g_cm_us_avail, us["availability"])
    if "eligible" in us:           _set_or_nan(g_cm_us_elig, us["eligible"])
    if "borrowing_base" in us:     _set_or_nan(g_cm_us_bb, us["borrowing_base"])
    if "facility_size" in us:      _set_or_nan(g_cm_us_facility, us["facility_size"])
    if "binding_cap" in us:        _set_or_nan(g_cm_us_binding_cap, us["binding_cap"])
    if "total_receivables" in us:  _set_or_nan(g_cm_us_total_recv, us["total_receivables"])
    if "concentration_breaches_total" in us: _set_or_nan(g_cm_us_conc_total, us["concentration_breaches_total"])
    if "portfolio_total" in us:    _set_or_nan(g_cm_us_port_total, us["portfolio_total"])
    if "portfolio_accounts" in us: _set_or_nan(g_cm_us_port_accts, us["portfolio_accounts"])
    if "portfolio_dq30_pct" in us: _set_or_nan(g_cm_us_port_dq30, us["portfolio_dq30_pct"])
    # derived
    if us.get("total_receivables") and us.get("eligible") is not None:
        _set_or_nan(g_cm_us_ineligible, us["total_receivables"] - us["eligible"])
    if us.get("binding_cap") and us.get("total_drawn") is not None:
        _set_or_nan(g_cm_us_utilization, us["total_drawn"] / us["binding_cap"] * 100 if us["binding_cap"] else None)

    mx = data.get("mx_sofom") or {}
    if "total_drawn" in mx:        _set_or_nan(g_cm_mx_drawn, mx["total_drawn"])
    if "availability" in mx:       _set_or_nan(g_cm_mx_avail, mx["availability"])
    if "eligible" in mx:           _set_or_nan(g_cm_mx_elig, mx["eligible"])
    if "borrowing_base" in mx:     _set_or_nan(g_cm_mx_bb, mx["borrowing_base"])
    if "facility_size" in mx:      _set_or_nan(g_cm_mx_facility, mx["facility_size"])
    if "binding_cap" in mx:        _set_or_nan(g_cm_mx_binding_cap, mx["binding_cap"])
    if "collection_cash_mxn" in mx: _set_or_nan(g_cm_mx_coll, mx["collection_cash_mxn"])
    if "total_receivables" in mx:  _set_or_nan(g_cm_mx_total_recv, mx["total_receivables"])
    if "receivable_bb_usd" in mx:  _set_or_nan(g_cm_mx_receivable_bb, mx["receivable_bb_usd"])
    if "cash_bb_usd" in mx:        _set_or_nan(g_cm_mx_cash_bb, mx["cash_bb_usd"])
    if "swap_bb_usd" in mx:        _set_or_nan(g_cm_mx_swap_bb, mx["swap_bb_usd"])
    if "usdmxn_rate" in mx:        _set_or_nan(g_cm_mx_usdmxn, mx["usdmxn_rate"])
    if "portfolio_total" in mx:    _set_or_nan(g_cm_mx_port_total, mx["portfolio_total"])
    if "portfolio_accounts" in mx: _set_or_nan(g_cm_mx_port_accts, mx["portfolio_accounts"])
    if "portfolio_dq30_pct" in mx: _set_or_nan(g_cm_mx_port_dq30, mx["portfolio_dq30_pct"])
    if mx.get("total_receivables") and mx.get("eligible") is not None:
        _set_or_nan(g_cm_mx_ineligible, mx["total_receivables"] - mx["eligible"])
    if mx.get("binding_cap") and mx.get("total_drawn") is not None:
        _set_or_nan(g_cm_mx_utilization, mx["total_drawn"] / mx["binding_cap"] * 100 if mx["binding_cap"] else None)

    # Totals
    drawn_total = sum(filter(None, (us.get("total_drawn"), mx.get("total_drawn"))))
    avail_total = sum(filter(None, (us.get("availability"), mx.get("availability"))))
    facility_total = sum(filter(None, (us.get("facility_size"), mx.get("facility_size"))))
    g_cm_total_drawn.set(drawn_total)
    g_cm_total_available.set(avail_total)
    g_cm_total_facility.set(facility_total)

    # Global stratifications (combined across facilities).
    # Clear labelled gauges first so stale country/bucket entries don't linger.
    for g in (g_cm_balance_by_country, g_cm_accounts_by_country,
              g_cm_balance_by_dpd, g_cm_accounts_by_dpd,
              g_cm_balance_by_product, g_cm_top_debtor_balance,
              g_cm_bank_balance, g_cm_covenant_actual_pct,
              g_cm_covenant_limit_pct, g_cm_covenant_excess,
              g_cm_covenant_headroom_pct, g_cm_alert,
              g_cm_roll_count, g_cm_roll_pct, g_cm_roll_period):
        g.clear()

    # Covenants per facility
    for label, block in (("us_bridge", us), ("mx_sofom", mx)):
        for cov in (block.get("covenants") or []):
            if not isinstance(cov, dict):
                continue
            test = str(cov.get("test", ""))
            _set_or_nan(g_cm_covenant_actual_pct.labels(facility=label, test=test), cov.get("actual_pct"))
            _set_or_nan(g_cm_covenant_limit_pct.labels(facility=label, test=test), cov.get("limit_pct"))
            _set_or_nan(g_cm_covenant_excess.labels(facility=label, test=test),
                        cov.get("excess_usd") if cov.get("excess_usd") is not None else cov.get("excess_mxn"))
            if isinstance(cov.get("actual_pct"), (int, float)) and isinstance(cov.get("limit_pct"), (int, float)):
                g_cm_covenant_headroom_pct.labels(facility=label, test=test).set(
                    cov["limit_pct"] - cov["actual_pct"]
                )

    # Alerts — compute from current state
    alerts = []
    def _add_alert(aid, sev, summary, cond):
        if cond:
            alerts.append((aid, sev, summary))
    _add_alert("bridge_bb_deficit", "high",
               f"Bridge in BB deficit: drawn ${us.get('total_drawn',0)/1e6:.1f}M > BB ${us.get('borrowing_base',0)/1e6:.1f}M",
               us.get("bb_excess_deficit") is not None and us["bb_excess_deficit"] < 0)
    _add_alert("bridge_high_util", "med",
               f"Bridge utilization {(us.get('total_drawn') or 0) / (us.get('binding_cap') or 1) * 100:.0f}%",
               us.get("binding_cap") and us.get("total_drawn") and (us["total_drawn"] / us["binding_cap"]) > 0.95)
    _add_alert("sofom_high_util", "med",
               f"SOFOM utilization {(mx.get('total_drawn') or 0) / (mx.get('binding_cap') or 1) * 100:.0f}%",
               mx.get("binding_cap") and mx.get("total_drawn") and (mx["total_drawn"] / mx["binding_cap"]) > 0.95)
    _add_alert("bridge_concentration_breach", "med",
               f"Bridge concentration excess ${us.get('concentration_excess_total',0)/1e6:.1f}M (L40−L60)",
               us.get("concentration_excess_total") and us["concentration_excess_total"] > 100_000)
    _add_alert("low_total_availability", "high",
               f"Total availability below $5M (drawn against tight headroom)",
               isinstance(avail_total, (int, float)) and avail_total < 5_000_000)
    for aid, sev, summary in alerts:
        g_cm_alert.labels(id=aid, severity=sev, summary=summary).set(1.0)

    port = data.get("portfolio") or {}
    for country, stats in (port.get("by_country") or {}).items():
        if isinstance(stats, dict):
            _set_or_nan(g_cm_balance_by_country.labels(country=country), stats.get("balance"))
            _set_or_nan(g_cm_accounts_by_country.labels(country=country), stats.get("accounts"))
    for bucket, stats in (port.get("by_dpd_bucket") or {}).items():
        if isinstance(stats, dict):
            _set_or_nan(g_cm_balance_by_dpd.labels(bucket=bucket), stats.get("balance"))
            _set_or_nan(g_cm_accounts_by_dpd.labels(bucket=bucket), stats.get("accounts"))
    for product, bal in (port.get("by_product") or {}).items():
        _set_or_nan(g_cm_balance_by_product.labels(product=product), bal)
    for i, td in enumerate(port.get("top_debtors") or [], start=1):
        if isinstance(td, dict) and td.get("balance") is not None:
            g_cm_top_debtor_balance.labels(
                rank=str(i),
                company_id=str(td.get("company_id") or ""),
                name=str(td.get("name") or ""),
            ).set(float(td["balance"]))
    orig = port.get("originations_period") or {}
    if orig:
        _set_or_nan(g_cm_originations_card, orig.get("card"))
        _set_or_nan(g_cm_originations_jp, orig.get("jeeves_pay"))
        _set_or_nan(g_cm_originations_total, orig.get("total"))
    for country, bal in (port.get("bank_balances_by_country") or {}).items():
        _set_or_nan(g_cm_bank_balance.labels(country=country), bal)

    # Roll-rate matrix — counts + row-normalized percentages.
    for key, stats in (port.get("roll_rate") or {}).items():
        if "|" not in key or not isinstance(stats, dict):
            continue
        fb, tb = key.split("|", 1)
        if stats.get("count") is not None:
            g_cm_roll_count.labels(from_bucket=fb, to_bucket=tb).set(float(stats["count"]))
        if stats.get("pct") is not None:
            g_cm_roll_pct.labels(from_bucket=fb, to_bucket=tb).set(float(stats["pct"]))

    # Roll-rate period (BOP / EOP) — surface dates as gauge labels so the
    # dashboard panel can show them in titles.
    bop_dt = port.get("bop_dt")
    eop_dt = port.get("eop_dt")
    if bop_dt or eop_dt:
        g_cm_roll_period.labels(bop_dt=str(bop_dt or ""), eop_dt=str(eop_dt or "")).set(1.0)

    # CICO daily cash — loaded from sibling state file
    g_cm_cico_category.clear()
    cico_file = _state_dir() / "_cico_state.json"
    if cico_file.exists():
        try:
            cico = json.loads(cico_file.read_text(encoding="utf-8"))
            for cat, bal in (cico.get("by_category") or {}).items():
                if isinstance(bal, (int, float)):
                    g_cm_cico_category.labels(category=cat).set(float(bal))
            _set_or_nan(g_cm_cico_total_cash, cico.get("total_cash_usd"))
            _set_or_nan(g_cm_cico_restricted, cico.get("restricted_deposits_usd"))
            _set_or_nan(g_cm_cico_daca, cico.get("daca_pledged_usd"))
            _set_or_nan(g_cm_cico_total_plus_restricted, cico.get("total_cash_plus_restricted_usd"))
        except (OSError, ValueError):
            pass

    # Backward-compat: mirror US Bridge portfolio into legacy generic gauges
    # for any pre-existing dashboard panels still using them. Remove after
    # all panels migrate to facility-scoped gauges.
    if "portfolio_total" in us:    _set_or_nan(g_cm_port_total, us["portfolio_total"])
    if "portfolio_accounts" in us: _set_or_nan(g_cm_port_accts, us["portfolio_accounts"])
    if "portfolio_dq30_pct" in us: _set_or_nan(g_cm_port_dq30, us["portfolio_dq30_pct"])

    # Legacy "portfolio" top-level block in older state files
    port = data.get("portfolio") or {}
    if "dq30_pct" in port: _set_or_nan(g_cm_port_dq30, port["dq30_pct"])
    if "total" in port:    _set_or_nan(g_cm_port_total, port["total"])
    if "accounts" in port: _set_or_nan(g_cm_port_accts, port["accounts"])

    health = data.get("cron_health") or {}
    last_ts = data.get("cron_last_ts") or {}
    cron_map = (
        ("us_bb", "us_bb_days_ago", "us_bb_ts"),
        ("mx_bb", "mx_bb_days_ago", "mx_bb_ts"),
        ("sofom_dist", "sofom_dist_days_ago", "sofom_dist_ts"),
        ("cico", "cico_days_ago", None),
        ("analytics", "analytics_days_ago", "analytics_ts"),
        ("revenue_comp", "revenue_comp_days_ago", "revenue_comp_ts"),
        ("dreams", "dreams_days_ago", "dreams_ts"),
        ("eod_review", "eod_review_days_ago", None),
    )
    for label, days_key, ts_key in cron_map:
        d = health.get(days_key)
        if d is not None:
            try:
                g_cm_cron_days.labels(cron=label).set(float(d))
            except (TypeError, ValueError):
                pass
        if ts_key:
            t = last_ts.get(ts_key)
            if t is not None:
                try:
                    g_cm_cron_last_ts.labels(cron=label).set(float(t))
                except (TypeError, ValueError):
                    pass


def _refresh_crons() -> None:
    """Per-cron liveness gauges, read from the supervisor registry.

    Lets a Grafana alert catch the silent-failure class this whole review is
    about: a cron that died, exited (run_loop returned), or crash-loops. Clears
    stale label sets first so a removed cron doesn't linger.
    """
    try:
        from app.gateway.cron_supervisor import _supervisors

        g_cron_running.clear()
        g_cron_crash_total.clear()
        g_cron_exited.clear()
        g_cron_heartbeat_ts.clear()
        for sv in _supervisors:
            g_cron_running.labels(cron=sv.name).set(1 if sv.running else 0)
            g_cron_crash_total.labels(cron=sv.name).set(getattr(sv, "crash_count", 0))
            g_cron_exited.labels(cron=sv.name).set(1 if getattr(sv, "exited", False) else 0)
            g_cron_heartbeat_ts.labels(cron=sv.name).set(getattr(sv, "last_heartbeat", 0.0))
    except Exception:
        pass


def refresh_all(langgraph_url: str = "http://localhost:2024") -> None:
    """Recompute all metrics. Called from the /metrics handler."""
    started = time.monotonic()
    _refresh_db_size()
    _refresh_langgraph_up(langgraph_url)
    _refresh_active_runs()
    _refresh_dispatch_audit()
    _refresh_thread_map()
    _refresh_langgraph_threads(langgraph_url)
    _refresh_cap_markets()
    _refresh_crons()
    g_scrape_seconds.set(time.monotonic() - started)
