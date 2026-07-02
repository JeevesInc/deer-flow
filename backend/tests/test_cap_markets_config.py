"""Tests for the cap-markets business-config extraction (P3-4, 2026-07-02).

Deal lists, FX fallbacks, and facility sizes moved from code into
cap_markets_config.json with mtime hot-reload. The loader must fail open to
baked defaults — a bad live edit must never take the webhook classifier down.
"""
from __future__ import annotations

import importlib.util
import json
import os
import time
from pathlib import Path

import pytest

_SHARED = Path(__file__).resolve().parent.parent.parent / "skills" / "custom" / "_shared"


@pytest.fixture()
def cmc(monkeypatch, tmp_path):
    """Fresh loader module pointed at a tmp config path (cache isolated)."""
    monkeypatch.setenv("CAP_MARKETS_CONFIG_PATH", str(tmp_path / "cm.json"))
    spec = importlib.util.spec_from_file_location("cmc_under_test",
                                                  _SHARED / "cap_markets_config.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _write(cmc, data, bump=1):
    p = Path(os.environ["CAP_MARKETS_CONFIG_PATH"])
    p.write_text(json.dumps(data), encoding="utf-8")
    # mtime resolution can be coarse; force a strictly-increasing mtime so
    # the reload check fires deterministically.
    os.utime(p, (time.time(), time.time() + bump))
    return p


def test_defaults_when_file_missing(cmc):
    cfg = cmc.get_cm_config()
    assert cfg["fx_fallback"]["MXN"] == 17.35
    assert cmc.facility_size_usd("us_bridge") == 75_000_000


def test_json_overrides_and_merges(cmc):
    _write(cmc, {"fx_fallback": {"MXN": 18.2}})
    cfg = cmc.get_cm_config()
    assert cfg["fx_fallback"]["MXN"] == 18.2
    assert cfg["fx_fallback"]["BRL"] == 5.5  # default preserved by deep-merge


def test_hot_reload_on_mtime_change(cmc):
    _write(cmc, {"facilities": {"us_bridge": {"facility_size_usd": 75_000_000}}}, bump=1)
    assert cmc.facility_size_usd("us_bridge") == 75_000_000
    _write(cmc, {"facilities": {"us_bridge": {"facility_size_usd": 100_000_000}}}, bump=5)
    assert cmc.facility_size_usd("us_bridge") == 100_000_000  # no restart needed


def test_corrupt_edit_serves_last_good(cmc):
    _write(cmc, {"fx_fallback": {"MXN": 18.0}})
    assert cmc.fx_local_per_usd("MXN") == 18.0
    p = Path(os.environ["CAP_MARKETS_CONFIG_PATH"])
    p.write_text("{not json", encoding="utf-8")
    os.utime(p, (time.time(), time.time() + 2))
    assert cmc.fx_local_per_usd("MXN") == 18.0  # last-good, not a crash


def test_fx_helpers(cmc):
    assert cmc.fx_local_per_usd("USD") == 1.0
    assert cmc.fx_local_per_usd("mxn") == 17.35  # case-insensitive
    assert cmc.fx_local_per_usd("XYZ") is None
    assert cmc.to_usd(173.5, "MXN") == pytest.approx(10.0)
    assert cmc.to_usd(5, "XYZ") is None


def test_analyst_deals_section_formats(cmc):
    _write(cmc, {"analyst_context": {
        "as_of": "July 2026",
        "active_deals_md": "- **NB** — docs in progress",
        "counterparty_domains_md": "- nb.com — Neuberger Berman",
    }})
    section = cmc.analyst_deals_section()
    assert "as of July 2026" in section
    assert "- **NB** — docs in progress" in section
    assert "nb.com" in section


def test_analyst_deals_section_empty_when_unset(cmc):
    assert cmc.analyst_deals_section() == ""  # defaults carry no deal text


def test_webhook_context_includes_config_deals(monkeypatch):
    import webhook_receiver as wr

    class FakeCmc:
        @staticmethod
        def analyst_deals_section():
            return "## Active deals\n- **TESTDEAL** — closing Friday"
    import sys
    monkeypatch.setitem(sys.modules, "cap_markets_config", FakeCmc)
    ctx = wr._analyst_context()
    assert "TESTDEAL" in ctx
    assert "Who Brian is" in ctx                     # framework head intact
    assert "What IS actionable" in ctx               # framework tail intact


def test_webhook_context_survives_missing_config(monkeypatch):
    import sys
    import webhook_receiver as wr

    class Boom:
        @staticmethod
        def analyst_deals_section():
            raise RuntimeError("config exploded")
    monkeypatch.setitem(sys.modules, "cap_markets_config", Boom)
    ctx = wr._analyst_context()
    assert "Who Brian is" in ctx
    assert "What IS actionable" in ctx
