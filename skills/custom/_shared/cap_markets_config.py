"""Hot-reloading loader for cap_markets_config.json (P3-4).

Business config (active deals for the classifier, FX fallbacks, facility
sizes) used to be baked into code — editing the deal list required a process
restart, and FX rates disagreed across files. Now it lives in
`deer-flow/cap_markets_config.json`; this loader mtime-caches and reloads on
edit, and falls back to baked defaults if the file is missing or corrupt so
consumers (the webhook classifier above all) never break on a bad edit.
"""

import json
import logging
import os
from pathlib import Path

log = logging.getLogger("cap_markets_config")

# skills/custom/_shared/ -> deer-flow root
_DEFAULT_PATH = Path(__file__).resolve().parents[3] / "cap_markets_config.json"

# Baked defaults — mirror the values that used to live in code, so behavior
# is unchanged when the JSON is absent.
DEFAULTS = {
    "analyst_context": {
        "as_of": "May 2026",
        "active_deals_md": "",
        "counterparty_domains_md": "",
    },
    "fx_fallback": {"MXN": 17.35, "BRL": 5.5, "COP": 4100.0, "EUR": 0.926,
                    "GBP": 0.787, "CAD": 1.37, "ARS": 1000.0, "CLP": 950.0,
                    "PEN": 3.75},
    "facilities": {"us_bridge": {"facility_size_usd": 75_000_000}},
}

_cache: dict = {}
_cache_mtime: float | None = None
_cache_path: str | None = None


def _config_path() -> Path:
    override = os.environ.get("CAP_MARKETS_CONFIG_PATH", "")
    return Path(override) if override else _DEFAULT_PATH


def _merge(base: dict, override: dict) -> dict:
    out = dict(base)
    for k, v in override.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        else:
            out[k] = v
    return out


def get_cm_config() -> dict:
    """Config dict (defaults deep-merged with the JSON). Hot-reloads on
    mtime change; serves the last-good copy if a live edit is corrupt."""
    global _cache, _cache_mtime, _cache_path
    path = _config_path()
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return _cache if _cache else dict(DEFAULTS)
    if _cache and _cache_mtime == mtime and _cache_path == str(path):
        return _cache
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
        _cache = _merge(DEFAULTS, loaded)
        _cache_mtime = mtime
        _cache_path = str(path)
    except Exception as e:
        log.error("cap_markets_config.json unreadable (%s) — %s", e,
                  "serving last-good copy" if _cache else "using baked defaults")
        if not _cache:
            _cache = dict(DEFAULTS)
    return _cache


def fx_local_per_usd(currency: str) -> float | None:
    """Fallback LOCAL-per-USD rate for a currency, or None if unknown/USD."""
    cur = (currency or "").upper()
    if cur == "USD":
        return 1.0
    rate = get_cm_config().get("fx_fallback", {}).get(cur)
    return float(rate) if isinstance(rate, (int, float)) and rate > 0 else None


def to_usd(amount: float, currency: str) -> float | None:
    """Convert a local amount to USD using the fallback table. None if the
    currency is unknown — callers decide whether to pass through or skip."""
    rate = fx_local_per_usd(currency)
    if rate is None:
        return None
    return float(amount) / rate


def facility_size_usd(facility: str) -> float | None:
    val = get_cm_config().get("facilities", {}).get(facility, {}).get("facility_size_usd")
    return float(val) if isinstance(val, (int, float)) and val > 0 else None


def analyst_deals_section() -> str:
    """The volatile business-state block for the classifier/drafter prompt."""
    ctx = get_cm_config().get("analyst_context", {})
    parts = []
    deals = (ctx.get("active_deals_md") or "").strip()
    if deals:
        parts.append(f"## Active deals and workstreams (as of {ctx.get('as_of', 'recently')})\n{deals}")
    domains = (ctx.get("counterparty_domains_md") or "").strip()
    if domains:
        parts.append(f"## Key counterparties (emails from these domains are higher priority)\n{domains}")
    return "\n\n".join(parts)
