#!/usr/bin/env python3
"""Seed the SQL repo with common queries from the DSPy training set.

Run once to populate the repo:
    python seed_repo.py
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from sql_repo import save_query

SEED_QUERIES = [
    {
        "name": "total_portfolio_balance",
        "sql": "SELECT SUM(balance_usd) AS total_balance FROM capital_markets_dm.loc_tape WHERE dt = (SELECT MAX(dt) FROM capital_markets_dm.loc_tape) AND charge_off_flag = false AND is_in_repayment = false",
        "tags": ["portfolio", "balance", "loc"],
        "description": "Total active LOC portfolio balance as of latest date",
    },
    {
        "name": "card_spend_by_month",
        "sql": "SELECT DATE_TRUNC('month', dt) AS month, SUM(COALESCE(disbursement_amount_usd, 0)) AS card_spend FROM capital_markets_dm.loc_tape WHERE dt BETWEEN CURRENT_DATE - INTERVAL '6 months' AND CURRENT_DATE - 1 AND charge_off_flag = false AND is_in_repayment = false GROUP BY 1 ORDER BY 1",
        "tags": ["spend", "disbursement", "monthly", "loc"],
        "description": "Monthly card spend (disbursement) trend for last 6 months",
    },
    {
        "name": "dpd_90plus_by_country",
        "sql": "SELECT country_code, SUM(CASE WHEN days_past_due > 90 THEN balance_usd ELSE 0 END) / NULLIF(SUM(balance_usd), 0) AS dpd_90plus_rate, SUM(balance_usd) AS total_balance FROM capital_markets_dm.loc_tape WHERE dt = (SELECT MAX(dt) FROM capital_markets_dm.loc_tape) AND charge_off_flag = false AND is_in_repayment = false GROUP BY country_code",
        "tags": ["dpd", "delinquency", "country", "loc"],
        "description": "90+ DPD rate by country code",
    },
    {
        "name": "charge_off_amount_by_company",
        "sql": "SELECT company_id, balance_usd AS charge_off_amount, charge_off_dt FROM capital_markets_dm.loc_tape WHERE dt = charge_off_dt AND charge_off_flag = true ORDER BY charge_off_dt DESC",
        "tags": ["charge-off", "company", "loc"],
        "description": "Charge-off amounts by company (balance on charge-off date)",
    },
    {
        "name": "total_revenue_by_quarter",
        "sql": "SELECT DATE_TRUNC('quarter', posted_at) AS quarter, SUM(revenue_usd) AS total_revenue FROM master_transactions_dm.transactions_ssot WHERE is_company_test = false AND posted_at >= CURRENT_DATE - INTERVAL '1 year' GROUP BY 1 ORDER BY 1",
        "tags": ["revenue", "quarterly", "transactions"],
        "description": "Quarterly revenue for the last year",
    },
    {
        "name": "top_borrowers_with_names",
        "sql": "SELECT c.name, c.country_code, lt.balance_usd FROM capital_markets_dm.loc_tape lt JOIN master_customer_dm.companies_dm c ON c.company_id = lt.company_id WHERE lt.dt = (SELECT MAX(dt) FROM capital_markets_dm.loc_tape) AND lt.charge_off_flag = false AND lt.is_in_repayment = false AND c.is_test = false ORDER BY lt.balance_usd DESC LIMIT 10",
        "tags": ["top", "borrowers", "balance", "loc"],
        "description": "Top 10 borrowers by balance with company names",
    },
    {
        "name": "dq_bucket_distribution",
        "sql": "SELECT dq_bucket, COUNT(DISTINCT company_id) AS accounts, SUM(balance_usd) AS balance FROM capital_markets_dm.loc_tape WHERE dt = (SELECT MAX(dt) FROM capital_markets_dm.loc_tape) AND charge_off_flag = false AND is_in_repayment = false GROUP BY dq_bucket ORDER BY dq_bucket",
        "tags": ["dq", "delinquency", "bucket", "loc"],
        "description": "DQ bucket distribution (account count + balance)",
    },
    {
        "name": "monthly_charge_off_trend",
        "sql": "SELECT DATE_TRUNC('month', charge_off_dt) AS month, COUNT(DISTINCT company_id) AS accounts, SUM(balance_usd) AS charged_off_balance FROM capital_markets_dm.loc_tape WHERE charge_off_dt IS NOT NULL AND dt = charge_off_dt AND charge_off_dt >= CURRENT_DATE - INTERVAL '6 months' GROUP BY 1 ORDER BY 1",
        "tags": ["charge-off", "monthly", "trend", "loc"],
        "description": "Monthly charge-off trend (last 6 months)",
    },
    {
        "name": "portfolio_balance_by_country",
        "sql": "SELECT country_code, SUM(balance_usd) AS balance, COUNT(DISTINCT company_id) AS accounts FROM capital_markets_dm.loc_tape WHERE dt = (SELECT MAX(dt) FROM capital_markets_dm.loc_tape) AND charge_off_flag = false AND is_in_repayment = false GROUP BY country_code ORDER BY balance DESC",
        "tags": ["portfolio", "balance", "country", "loc"],
        "description": "Portfolio balance and account count by country",
    },
    {
        "name": "revenue_by_product",
        "sql": "SELECT product, SUM(revenue_usd) AS total_revenue FROM master_transactions_dm.transactions_ssot WHERE posted_at >= DATE_TRUNC('quarter', CURRENT_DATE) AND is_company_test = false GROUP BY product ORDER BY total_revenue DESC",
        "tags": ["revenue", "product", "transactions"],
        "description": "Revenue by product for current quarter",
    },
    {
        "name": "active_accounts_by_country",
        "sql": "SELECT country_code, COUNT(DISTINCT company_id) AS active_accounts FROM capital_markets_dm.loc_tape WHERE dt = (SELECT MAX(dt) FROM capital_markets_dm.loc_tape) AND charge_off_flag = false AND is_in_repayment = false GROUP BY country_code ORDER BY active_accounts DESC",
        "tags": ["accounts", "country", "loc"],
        "description": "Active account count by country",
    },
    {
        "name": "gwc_portfolio_balance",
        "sql": "SELECT SUM(balance_usd) AS gwc_balance FROM capital_markets_dm.gwc_tape WHERE dt = (SELECT MAX(dt) FROM capital_markets_dm.gwc_tape) AND charge_off_flag = false",
        "tags": ["gwc", "balance", "jeeves-pay"],
        "description": "Total GWC/Jeeves Pay portfolio balance",
    },
    {
        "name": "rpp_installments",
        "sql": "SELECT loan_id, company_id, dt AS due_date, principal_amount_due_usd, balance_usd, days_past_due FROM capital_markets_dm.gwc_tape WHERE loan_reference_number ILIKE 'RPP%' AND dt >= CURRENT_DATE - 30 AND principal_amount_due_usd != 0 ORDER BY dt",
        "tags": ["rpp", "installments", "gwc", "repayment"],
        "description": "RPP (Repayment Plan) loan installments from GWC tape",
    },
    {
        "name": "vintage_cohort_charge_off_rates",
        "sql": "SELECT start_date, SUM(charge_off_usd) / NULLIF(SUM(bop_balance_usd), 0) AS co_rate, SUM(bop_balance_usd) AS bop_balance, SUM(charge_off_usd) AS charge_offs FROM analytics_sandbox.loc_vintage_data GROUP BY start_date ORDER BY start_date",
        "tags": ["vintage", "cohort", "charge-off", "rate"],
        "description": "Vintage cohort charge-off rates",
    },
    {
        "name": "recent_activations",
        "sql": "SELECT company_id, name, country_code, activation_date, credit_limit_usd FROM master_customer_dm.companies_dm WHERE activation_date >= CURRENT_DATE - 30 AND is_test = false ORDER BY activation_date DESC",
        "tags": ["activation", "new", "companies"],
        "description": "Companies activated in the last 30 days",
    },
    {
        "name": "daily_balance_trend",
        "sql": "SELECT dt, SUM(balance_usd) AS total_balance, COUNT(DISTINCT company_id) AS active_accounts FROM capital_markets_dm.loc_tape WHERE dt BETWEEN CURRENT_DATE - 31 AND CURRENT_DATE - 1 AND charge_off_flag = false AND is_in_repayment = false GROUP BY dt ORDER BY dt",
        "tags": ["daily", "balance", "trend", "loc"],
        "description": "Daily portfolio balance trend for the past month",
    },
    {
        "name": "card_vs_jp_balance_split",
        "sql": "SELECT SUM(COALESCE(card_balance_usd, 0)) AS card_balance, SUM(COALESCE(jp_balance_usd, 0)) AS jp_balance, SUM(balance_usd) AS total_balance FROM capital_markets_dm.loc_tape WHERE dt = (SELECT MAX(dt) FROM capital_markets_dm.loc_tape) AND charge_off_flag = false AND is_in_repayment = false",
        "tags": ["card", "jeeves-pay", "balance", "split", "loc"],
        "description": "Card vs Jeeves Pay balance split",
    },
    {
        "name": "high_credit_limit_companies",
        "sql": "SELECT c.company_id, c.name, c.country_code, c.credit_limit_usd, lt.balance_usd FROM master_customer_dm.companies_dm c JOIN capital_markets_dm.loc_tape lt ON c.company_id = lt.company_id WHERE lt.dt = (SELECT MAX(dt) FROM capital_markets_dm.loc_tape) AND lt.charge_off_flag = false AND lt.is_in_repayment = false AND c.is_test = false AND c.credit_limit_usd > 1000000 ORDER BY c.credit_limit_usd DESC",
        "tags": ["credit-limit", "companies", "high-value"],
        "description": "Companies with credit limit over $1M and current balance",
    },
    {
        "name": "monthly_payment_trend",
        "sql": "SELECT DATE_TRUNC('month', dt) AS month, SUM(COALESCE(payment_amount_usd, 0)) AS total_payments FROM capital_markets_dm.loc_tape WHERE dt BETWEEN CURRENT_DATE - INTERVAL '6 months' AND CURRENT_DATE - 1 AND charge_off_flag = false AND is_in_repayment = false GROUP BY 1 ORDER BY 1",
        "tags": ["payments", "monthly", "trend", "loc"],
        "description": "Monthly payment amounts for the last 6 months",
    },
]


def main():
    print(f"Seeding SQL repo with {len(SEED_QUERIES)} queries...")
    for q in SEED_QUERIES:
        save_query(q['name'], q['sql'], tags=q.get('tags', []), description=q.get('description', ''))
        print(f"  Saved: {q['name']}")
    print(f"\nDone! {len(SEED_QUERIES)} queries saved to repo.")


if __name__ == '__main__':
    main()
