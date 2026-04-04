"""Tests for custom skill utilities — SQL validation, memory dedup, date guards."""

import datetime as dt
import json
import os
import sys
import tempfile

import pytest

# ---------------------------------------------------------------------------
# SQL Runner validation tests
# ---------------------------------------------------------------------------

# Add skills path so we can import sql_runner
SKILLS_DIR = os.path.join(os.path.dirname(__file__), "..", "..", "skills", "custom", "jeeves-redshift")
sys.path.insert(0, SKILLS_DIR)

from sql_runner import validate_sql  # noqa: E402


class TestSQLValidation:
    """Tests for sql_runner.validate_sql()."""

    def test_select_allowed(self):
        issues = validate_sql("SELECT 1")
        errors = [i for i in issues if i.startswith("ERROR:")]
        assert not errors

    def test_delete_blocked(self):
        issues = validate_sql("DELETE FROM capital_markets_dm.loc_tape")
        errors = [i for i in issues if i.startswith("ERROR:")]
        assert any("Only SELECT" in e for e in errors)

    def test_drop_blocked(self):
        issues = validate_sql("DROP TABLE foo")
        errors = [i for i in issues if i.startswith("ERROR:")]
        assert any("Only SELECT" in e for e in errors)

    def test_insert_blocked(self):
        issues = validate_sql("INSERT INTO foo VALUES (1)")
        errors = [i for i in issues if i.startswith("ERROR:")]
        assert any("Only SELECT" in e for e in errors)

    def test_cte_allowed(self):
        sql = "WITH cte AS (SELECT 1 AS x) SELECT * FROM cte"
        issues = validate_sql(sql)
        errors = [i for i in issues if i.startswith("ERROR:")]
        assert not errors

    def test_warns_deprecated_column(self):
        sql = "SELECT v0_charge_off_amount_usd FROM capital_markets_dm.loc_tape WHERE dt = '2026-04-02'"
        issues = validate_sql(sql)
        warnings = [i for i in issues if not i.startswith("ERROR:")]
        assert any("Deprecated column" in w for w in warnings)

    def test_warns_date_trunc_on_dt(self):
        sql = "SELECT * FROM capital_markets_dm.loc_tape WHERE DATE_TRUNC('month', dt) = '2026-03-01'"
        issues = validate_sql(sql)
        warnings = [i for i in issues if not i.startswith("ERROR:")]
        assert any("DATE_TRUNC" in w for w in warnings)

    def test_warns_today_date(self):
        today = dt.date.today().isoformat()
        sql = f"SELECT * FROM capital_markets_dm.loc_tape WHERE dt = '{today}'"
        issues = validate_sql(sql)
        warnings = [i for i in issues if not i.startswith("ERROR:")]
        assert any("today" in w.lower() for w in warnings)

    def test_warns_missing_charge_off_filter(self):
        sql = "SELECT SUM(balance_usd) FROM capital_markets_dm.loc_tape WHERE dt = '2026-04-02'"
        issues = validate_sql(sql)
        warnings = [i for i in issues if not i.startswith("ERROR:")]
        assert any("charge_off_flag" in w for w in warnings)

    def test_warns_missing_repayment_filter(self):
        sql = "SELECT SUM(balance_usd) FROM capital_markets_dm.loc_tape WHERE dt = '2026-04-02' AND charge_off_flag = false"
        issues = validate_sql(sql)
        warnings = [i for i in issues if not i.startswith("ERROR:")]
        assert any("is_in_repayment" in w for w in warnings)

    def test_no_warning_with_all_filters(self):
        sql = "SELECT SUM(balance_usd) FROM capital_markets_dm.loc_tape WHERE dt = '2026-04-02' AND charge_off_flag = false AND is_in_repayment = false"
        issues = validate_sql(sql)
        warnings = [i for i in issues if not i.startswith("ERROR:")]
        # Should only have no loc_tape-related warnings
        loc_warnings = [w for w in warnings if "loc_tape" in w or "charge_off" in w or "repayment" in w]
        assert not loc_warnings

    def test_warns_missing_test_filter(self):
        sql = "SELECT SUM(revenue_usd) FROM master_transactions_dm.transactions_ssot WHERE posted_at > '2026-01-01'"
        issues = validate_sql(sql)
        warnings = [i for i in issues if not i.startswith("ERROR:")]
        assert any("is_company_test" in w for w in warnings)

    def test_charge_off_query_no_false_positive(self):
        """Charge-off lookup queries use charge_off_dt — shouldn't warn about missing charge_off_flag."""
        sql = "SELECT balance_usd FROM capital_markets_dm.loc_tape WHERE company_id = 456 AND dt = charge_off_dt"
        issues = validate_sql(sql)
        warnings = [i for i in issues if not i.startswith("ERROR:")]
        charge_off_warnings = [w for w in warnings if "charge_off_flag" in w]
        assert not charge_off_warnings


# ---------------------------------------------------------------------------
# Memory similarity dedup tests
# ---------------------------------------------------------------------------

# Import from the harness package
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "packages", "harness"))

from deerflow.agents.memory.updater import _is_similar_to_existing, _normalize_token, _tokenize  # noqa: E402


class TestFactSimilarity:
    """Tests for memory fact deduplication."""

    def test_exact_substring(self):
        assert _is_similar_to_existing("Data available through yesterday", ["Redshift data only available through yesterday"])

    def test_plural_normalization(self):
        assert _is_similar_to_existing("User prefers PDF export", ["User exports PDFs"])

    def test_different_content(self):
        assert not _is_similar_to_existing("User likes Python", ["User prefers JavaScript"])

    def test_unrelated(self):
        assert not _is_similar_to_existing("User prefers PDF export", ["Brian runs Capital Markets"])

    def test_high_overlap(self):
        assert _is_similar_to_existing("Brian works with CSV files daily", ["Brian works with CSV data files"])

    def test_empty_existing(self):
        assert not _is_similar_to_existing("Some fact", [])

    def test_empty_new(self):
        assert not _is_similar_to_existing("", ["Some existing fact"])

    def test_normalize_token_plurals(self):
        assert _normalize_token("files") == "file"
        assert _normalize_token("PDFs") == "pdf"

    def test_normalize_token_ing(self):
        assert _normalize_token("running") == "runn"
        assert _normalize_token("exporting") == "export"

    def test_normalize_token_ed(self):
        assert _normalize_token("exported") == "export"


# ---------------------------------------------------------------------------
# Pydantic memory response parsing tests
# ---------------------------------------------------------------------------

from deerflow.agents.memory.updater import _parse_memory_response, MemoryUpdateResponse  # noqa: E402


class TestMemoryResponseParsing:
    """Tests for LLM memory response parsing robustness."""

    def test_valid_json(self):
        data = {"user": {}, "history": {}, "newFacts": [], "factsToRemove": []}
        result = _parse_memory_response(json.dumps(data))
        assert isinstance(result, MemoryUpdateResponse)

    def test_markdown_code_block(self):
        data = {"user": {}, "history": {}, "newFacts": [{"content": "test", "confidence": 0.9}]}
        text = f"```json\n{json.dumps(data)}\n```"
        result = _parse_memory_response(text)
        assert len(result.newFacts) == 1

    def test_json_with_surrounding_text(self):
        data = {"user": {}, "history": {}, "newFacts": []}
        text = f"Here's the update:\n{json.dumps(data)}\nDone."
        result = _parse_memory_response(text)
        assert isinstance(result, MemoryUpdateResponse)

    def test_invalid_json_returns_noop(self):
        result = _parse_memory_response("This is not JSON at all")
        assert isinstance(result, MemoryUpdateResponse)
        assert len(result.newFacts) == 0
        assert len(result.factsToRemove) == 0

    def test_partial_fields_use_defaults(self):
        result = _parse_memory_response('{"newFacts": [{"content": "hello", "confidence": 0.8}]}')
        assert len(result.newFacts) == 1
        assert result.newFacts[0].content == "hello"
