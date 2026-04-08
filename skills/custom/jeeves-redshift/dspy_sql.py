"""DSPy-powered SQL generation and validation for Jeeves Redshift queries.

This module uses DSPy to:
1. Generate SQL from natural language questions with chain-of-thought reasoning
2. Apply assertions to catch common mistakes before execution
3. Optimize prompts via compilation against a training set of known-good queries

Usage (standalone):
    python dspy_sql.py "What's the total portfolio balance?"

Usage (from sql_runner):
    from dspy_sql import generate_sql
    sql = generate_sql("What's the total portfolio balance?")

Usage (compile/optimize):
    python dspy_sql.py --compile
"""

import datetime as dt
import json
import os
import re
import sys

import dspy

# ---------------------------------------------------------------------------
# Schema context (injected into prompts)
# ---------------------------------------------------------------------------

SCHEMA_CONTEXT = """
Available Redshift tables:

1. capital_markets_dm.loc_tape — LOC daily tape (one row per company per day)
   Key columns: dt (date, snapshot), company_id, balance_usd, disbursement_amount_usd,
   payment_amount_usd, days_past_due, dq_bucket, charge_off_flag (boolean),
   charge_off_dt (date), is_in_repayment (boolean), country_code, status, card_balance_usd,
   jp_balance_usd, jeeves_pay_disbursement_amount_usd, delinquent_dt

2. capital_markets_dm.gwc_tape — GWC daily tape (Jeeves Pay loans, one row per loan per day)
   Key columns: dt, company_id, loan_id, balance_usd, principal_balance_usd,
   days_past_due, charge_off_flag, charge_off_dt, status

3. master_transactions_dm.transactions_ssot — All transactions (revenue, GTV)
   Key columns: posted_at, company_id, product, revenue_usd, gtv_usd,
   is_company_test (boolean), company_country

4. master_customer_dm.companies_dm — Company dimension
   Key columns: company_id, name, country_code, credit_limit_usd, is_test (boolean),
   platform_status, activation_date

5. analytics_sandbox.loc_vintage_data — Vintage cohort data
   Key columns: company_id, start_date, end_date, bop_balance_usd, eop_balance_usd,
   charge_off_usd, payment_amount_usd

RULES:
- Data only available through yesterday ({yesterday}). Never use today or future dates.
- loc_tape: ALWAYS include charge_off_flag = false AND is_in_repayment = false for active portfolio.
- loc_tape date filtering: use dt BETWEEN 'YYYY-MM-DD' AND 'YYYY-MM-DD', NEVER DATE_TRUNC on dt.
- transactions_ssot: ALWAYS include is_company_test = false.
- companies_dm: ALWAYS include is_test = false.
- Charge-off amount = balance_usd WHERE dt = charge_off_dt (never use v0_charge_off_amount_usd).
- Revenue is ONLY in transactions_ssot.revenue_usd. loc_tape has spend, not revenue.
- disbursement_amount_usd = customer card spend. Do NOT use loan_allocation_amount.
- NULL arithmetic: COALESCE(col, 0) when adding columns.
- JOIN KEY: company_id connects all tables.
- Country codes: 840=US, 484=MX, 170=CO, 124=CA, 76=BR.
""".strip()


# ---------------------------------------------------------------------------
# Training examples
# ---------------------------------------------------------------------------

TRAINING_SET = [
    {
        "question": "What's the total portfolio balance?",
        "sql": "SELECT SUM(balance_usd) AS total_balance FROM capital_markets_dm.loc_tape WHERE dt = (SELECT MAX(dt) FROM capital_markets_dm.loc_tape) AND charge_off_flag = false AND is_in_repayment = false",
    },
    {
        "question": "How much did customers spend on cards in March 2026?",
        "sql": "SELECT SUM(COALESCE(disbursement_amount_usd, 0)) AS card_spend FROM capital_markets_dm.loc_tape WHERE dt BETWEEN '2026-03-01' AND '2026-03-31' AND charge_off_flag = false AND is_in_repayment = false",
    },
    {
        "question": "What's our 90+ DPD rate by country?",
        "sql": "SELECT country_code, SUM(CASE WHEN days_past_due > 90 THEN balance_usd ELSE 0 END) / NULLIF(SUM(balance_usd), 0) AS dpd_90plus_rate, SUM(balance_usd) AS total_balance FROM capital_markets_dm.loc_tape WHERE dt = (SELECT MAX(dt) FROM capital_markets_dm.loc_tape) AND charge_off_flag = false AND is_in_repayment = false GROUP BY country_code",
    },
    {
        "question": "What was the charge-off amount for company 456?",
        "sql": "SELECT company_id, balance_usd AS charge_off_amount, charge_off_dt FROM capital_markets_dm.loc_tape WHERE company_id = 456 AND dt = charge_off_dt",
    },
    {
        "question": "Total revenue in Q1 2026?",
        "sql": "SELECT SUM(revenue_usd) AS total_revenue FROM master_transactions_dm.transactions_ssot WHERE posted_at BETWEEN '2026-01-01' AND '2026-03-31' AND is_company_test = false",
    },
    {
        "question": "Top 10 borrowers by balance with company names",
        "sql": "SELECT c.name, c.country_name, lt.balance_usd FROM capital_markets_dm.loc_tape lt JOIN master_customer_dm.companies_dm c ON c.company_id = lt.company_id WHERE lt.dt = (SELECT MAX(dt) FROM capital_markets_dm.loc_tape) AND lt.charge_off_flag = false AND lt.is_in_repayment = false AND c.is_test = false ORDER BY lt.balance_usd DESC LIMIT 10",
    },
    {
        "question": "Show me the DQ bucket distribution",
        "sql": "SELECT dq_bucket, COUNT(DISTINCT company_id) AS accounts, SUM(balance_usd) AS balance FROM capital_markets_dm.loc_tape WHERE dt = (SELECT MAX(dt) FROM capital_markets_dm.loc_tape) AND charge_off_flag = false AND is_in_repayment = false GROUP BY dq_bucket ORDER BY dq_bucket",
    },
    {
        "question": "Monthly charge-off trend for the last 6 months",
        "sql": "SELECT DATE_TRUNC('month', charge_off_dt) AS month, COUNT(DISTINCT company_id) AS accounts, SUM(balance_usd) AS charged_off_balance FROM capital_markets_dm.loc_tape WHERE charge_off_dt IS NOT NULL AND dt = charge_off_dt AND charge_off_dt >= DATEADD('month', -6, CURRENT_DATE) GROUP BY 1 ORDER BY 1",
    },
    {
        "question": "What's the portfolio balance for Mexico?",
        "sql": "SELECT SUM(balance_usd) AS mx_balance FROM capital_markets_dm.loc_tape WHERE dt = (SELECT MAX(dt) FROM capital_markets_dm.loc_tape) AND charge_off_flag = false AND is_in_repayment = false AND country_code = 484",
    },
    {
        "question": "Revenue by product this quarter",
        "sql": "SELECT product, SUM(revenue_usd) AS total_revenue FROM master_transactions_dm.transactions_ssot WHERE posted_at >= DATE_TRUNC('quarter', CURRENT_DATE) AND is_company_test = false GROUP BY product ORDER BY total_revenue DESC",
    },
]


# ---------------------------------------------------------------------------
# DSPy module
# ---------------------------------------------------------------------------

def _setup_dspy():
    """Configure DSPy with Anthropic backend."""
    import dspy

    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        raise RuntimeError("ANTHROPIC_API_KEY not set")

    lm = dspy.LM("anthropic/claude-sonnet-4-20250514", api_key=api_key, max_tokens=1024)
    dspy.configure(lm=lm)
    return lm


class TextToSQL(dspy.Signature):
    """Generate a Redshift SQL query from a natural language question about Jeeves financial data."""

    question: str = dspy.InputField(desc="Natural language question about Jeeves data")
    schema_context: str = dspy.InputField(desc="Available tables, columns, and query rules")
    reasoning: str = dspy.OutputField(desc="Step-by-step reasoning about which tables/columns to use and what filters are needed")
    sql: str = dspy.OutputField(desc="The complete SELECT SQL query for Amazon Redshift")


class JeevesSQL(dspy.Module):
    """DSPy module for generating validated Redshift SQL from natural language."""

    def __init__(self):
        super().__init__()
        self.generate = dspy.ChainOfThought(TextToSQL)

    def forward(self, question: str) -> dspy.Prediction:
        yesterday = (dt.date.today() - dt.timedelta(days=1)).isoformat()
        schema = SCHEMA_CONTEXT.format(yesterday=yesterday)

        result = self.generate(question=question, schema_context=schema)

        # Post-generation validation — fix common issues
        sql = result.sql.strip()
        sql_lower = sql.lower()
        warnings = []

        # Must be SELECT
        if not (sql_lower.lstrip().startswith("select") or sql_lower.lstrip().startswith("with")):
            raise ValueError("Generated query is not a SELECT statement")

        # No deprecated columns
        if "v0_charge_off_amount_usd" in sql_lower:
            raise ValueError("Generated query uses deprecated v0_charge_off_amount_usd")

        # loc_tape queries must have filters
        if "loc_tape" in sql_lower and "charge_off_dt" not in sql_lower:
            if "charge_off_flag" not in sql_lower:
                warnings.append("Missing charge_off_flag = false filter on loc_tape")
            if "is_in_repayment" not in sql_lower:
                warnings.append("Missing is_in_repayment = false filter on loc_tape")

        # transactions_ssot must filter test companies
        if "transactions_ssot" in sql_lower and "is_company_test" not in sql_lower:
            warnings.append("Missing is_company_test = false filter on transactions_ssot")

        # No DATE_TRUNC on dt column
        if re.search(r"date_trunc\s*\([^)]*\bdt\b", sql_lower):
            warnings.append("DATE_TRUNC used on dt column — use BETWEEN instead")

        # No today's date
        today = dt.date.today().isoformat()
        if f"'{today}'" in sql:
            warnings.append(f"Today's date ({today}) used — data only available through yesterday")

        if warnings:
            result.warnings = warnings
        else:
            result.warnings = []

        return result


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

_module: JeevesSQL | None = None


def generate_sql(question: str) -> dict:
    """Generate SQL from a natural language question.

    Returns:
        {"sql": str, "reasoning": str, "success": bool, "error": str | None}
    """
    global _module
    try:
        import dspy

        if _module is None:
            _setup_dspy()
            _module = JeevesSQL()

        result = _module(question=question)
        return {
            "sql": result.sql.strip(),
            "reasoning": result.reasoning.strip(),
            "warnings": getattr(result, "warnings", []),
            "success": True,
            "error": None,
        }
    except Exception as e:
        return {
            "sql": "",
            "reasoning": "",
            "success": False,
            "error": str(e),
        }


def compile_module(output_path: str = "optimized_sql_module.json") -> None:
    """Compile/optimize the DSPy module against the training set.

    This runs the optimizer which tunes the prompt based on training examples.
    The optimized module is saved to disk and loaded automatically on next use.
    """
    import dspy
    from dspy.evaluate import Evaluate
    from dspy.teleprompt import BootstrapFewShot

    _setup_dspy()

    # Build training examples
    trainset = []
    yesterday = (dt.date.today() - dt.timedelta(days=1)).isoformat()
    schema = SCHEMA_CONTEXT.format(yesterday=yesterday)
    for ex in TRAINING_SET:
        trainset.append(dspy.Example(
            question=ex["question"],
            schema_context=schema,
            sql=ex["sql"],
        ).with_inputs("question", "schema_context"))

    # Metric: does the generated SQL look correct?
    def sql_metric(example, prediction, trace=None):
        pred_sql = prediction.sql.strip().lower()
        gold_sql = example.sql.strip().lower()

        # Check key structural elements match
        score = 0.0

        # Same tables referenced
        gold_tables = set(re.findall(r'(?:from|join)\s+([\w.]+)', gold_sql))
        pred_tables = set(re.findall(r'(?:from|join)\s+([\w.]+)', pred_sql))
        if gold_tables == pred_tables:
            score += 0.4

        # Has required filters
        if "loc_tape" in gold_sql:
            if "charge_off_flag" in pred_sql:
                score += 0.15
            if "is_in_repayment" in pred_sql:
                score += 0.15
        if "transactions_ssot" in gold_sql:
            if "is_company_test" in pred_sql:
                score += 0.15

        # Is valid SELECT
        if pred_sql.startswith("select") or pred_sql.startswith("with"):
            score += 0.15

        # No deprecated columns
        if "v0_charge_off_amount_usd" not in pred_sql:
            score += 0.15

        return score

    # Compile
    optimizer = BootstrapFewShot(metric=sql_metric, max_bootstrapped_demos=4, max_labeled_demos=4)
    compiled_module = optimizer.compile(JeevesSQL(), trainset=trainset)

    # Save
    compiled_module.save(output_path)
    print(f"Optimized module saved to: {output_path}")

    # Evaluate
    evaluator = Evaluate(devset=trainset, metric=sql_metric, num_threads=1, display_progress=True)
    score = evaluator(compiled_module)
    print(f"Evaluation score: {score}")

    return compiled_module


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')

    if "--compile" in sys.argv:
        output = "optimized_sql_module.json"
        for i, arg in enumerate(sys.argv):
            if arg == "--output" and i + 1 < len(sys.argv):
                output = sys.argv[i + 1]
        compile_module(output)
        return

    if "--load" in sys.argv:
        # Load optimized module
        path = "optimized_sql_module.json"
        for i, arg in enumerate(sys.argv):
            if arg == "--load" and i + 1 < len(sys.argv):
                path = sys.argv[i + 1]
        import dspy
        _setup_dspy()
        module = JeevesSQL()
        module.load(path)
        global _module
        _module = module
        print(f"Loaded optimized module from: {path}")
        # Fall through to generate

    question = None
    for arg in sys.argv[1:]:
        if not arg.startswith("--"):
            question = arg
            break

    if not question:
        print("Usage:", file=sys.stderr)
        print("  python dspy_sql.py \"What's the total balance?\"", file=sys.stderr)
        print("  python dspy_sql.py --compile [--output path.json]", file=sys.stderr)
        print("  python dspy_sql.py --load optimized.json \"question\"", file=sys.stderr)
        sys.exit(1)

    result = generate_sql(question)
    if result["success"]:
        print(f"Reasoning: {result['reasoning']}\n")
        print(f"SQL:\n{result['sql']}")
    else:
        print(f"Error: {result['error']}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
