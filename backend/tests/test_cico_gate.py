"""Tests for the CICO balance extraction gate (P2-8 / Skills-H2).

Bank balances used to zero-fill silently into the lender-facing borrowing
base when the vision extraction missed an account, and the fuzzy prefix
match picked the FIRST hit even when several keys matched.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

MODULE = (Path(__file__).resolve().parent.parent.parent
          / "skills" / "custom" / "jeeves-borrowing-base" / "cico_fetch_balances.py")


@pytest.fixture(scope="module")
def cico():
    spec = importlib.util.spec_from_file_location("cico_fetch_balances_under_test", MODULE)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _full_balances(cico, **overrides):
    """A balances dict covering every mapped CICO key."""
    data = {key: 100.0 for key in cico.BANK_ACCTS_TO_CICO.values()}
    data.update(overrides)
    return data


def test_all_matched_no_manifest(cico):
    records, manifest = cico.match_bank_accounts(_full_balances(cico))
    assert manifest["unmatched"] == []
    assert len(records) == len(cico._ROWS)
    assert all(r["bal"] == 100.0 for r in records)


def test_missing_account_lands_in_manifest(cico):
    balances = _full_balances(cico)
    del balances["8051"]  # Brazil / BS2
    records, manifest = cico.match_bank_accounts(balances)
    assert any(u["cico_key"] == "8051" and u["reason"] == "not extracted"
               for u in manifest["unmatched"])


def test_strict_build_raises_on_unmatched(cico):
    balances = _full_balances(cico)
    del balances["8051"]
    with pytest.raises(RuntimeError, match="zero-fill"):
        cico.build_bank_accts_df(balances)


def test_non_strict_build_returns_zero_filled_df(cico, capsys):
    balances = _full_balances(cico)
    del balances["8051"]
    df = cico.build_bank_accts_df(balances, strict=False)
    row = df[(df["country"] == "Brazil") & (df["acct"] == "BS2")]
    assert row.iloc[0]["bal"] == 0.0
    # Manifest is still printed even when not strict
    assert "unmatched-account manifest" in capsys.readouterr().err


def test_single_fuzzy_candidate_matches(cico):
    balances = _full_balances(cico)
    del balances["6540-1"]
    balances["6540"] = 250.0  # unique prefix match
    records, manifest = cico.match_bank_accounts(balances)
    assert manifest["unmatched"] == []
    row = next(r for r in records if r["acct"] == "BTG")
    assert row["bal"] == 250.0


def test_ambiguous_fuzzy_is_unmatched_not_first_hit(cico):
    balances = _full_balances(cico)
    del balances["0007"]  # Mexico
    balances["0007-1"] = 111.0
    balances["0007-2"] = 222.0
    records, manifest = cico.match_bank_accounts(balances)
    bad = [u for u in manifest["unmatched"] if u["cico_key"] == "0007"]
    assert bad and bad[0]["reason"].startswith("ambiguous")
    row = next(r for r in records if r["acct"] == "0007" and r["country"] == "Mexico")
    assert row["bal"] == 0.0  # not silently joined to either candidate


def test_unconsumed_keys_reported(cico):
    balances = _full_balances(cico, ZZZ999=42.0)
    _, manifest = cico.match_bank_accounts(balances)
    assert "ZZZ999" in manifest["unconsumed_keys"]


def test_dunder_keys_ignored(cico):
    balances = _full_balances(cico)
    balances["__email_date__"] = "Mon, 1 Jul 2026"
    _, manifest = cico.match_bank_accounts(balances)
    assert "__email_date__" not in manifest["unconsumed_keys"]


def test_extraction_gate_constant_env_overridable(cico, monkeypatch):
    assert cico.MIN_EXPECTED_ACCOUNTS >= 1


def test_vision_text_extraction_iterates_blocks(cico):
    """Sonnet 5 can lead with a ThinkingBlock; content[0].text raises on it."""
    src = MODULE.read_text(encoding="utf-8")
    assert "raw = resp.content[0].text" not in src
    assert "== 'text'" in src
