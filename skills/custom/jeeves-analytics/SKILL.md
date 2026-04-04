---
name: jeeves-analytics
description: Use this skill for ad-hoc analytical questions about the Jeeves credit portfolio — DPD rates, charge-off trends, portfolio balances, revenue breakdowns, vintage analysis, and concentration reports. Builds on the jeeves-redshift skill for data access.
allowed-tools:
  - bash
  - write_file
  - read_file
---

# Jeeves Portfolio Analytics

Ad-hoc analytical questions about the Jeeves credit portfolio.

## Examples

- "What's our 90+ DPD rate in Mexico vs last month?"
- "Which industries have the highest default rates?"
- "What's the total portfolio balance by country?"
- "Show me charge-off trends over the last 6 months"
- "Top 20 borrowers by balance"
- "How has DPD distribution changed month over month?"
- "Total revenue this quarter vs last quarter"

## Approach

1. Load `jeeves-redshift` skill for connection details and full schema reference
2. Install driver: `pip install psycopg2-binary`
3. Write and execute SQL against confirmed tables:
   - `capital_markets_dm.loc_tape` — LOC balances, disbursements, DPD, charge-offs (by company by day)
   - `capital_markets_dm.gwc_tape` — GWC/Jeeves Pay loan balances and obligations (by loan by day)
   - `master_customer_dm.companies_dm` — Company metadata (name, country, industry, credit limit)
   - `master_transactions_dm.transactions_ssot` — Revenue, GTV, transaction details
   - `analytics_sandbox.loc_vintage_data` — Vintage cohort analysis, BOP/EOP balances
4. Single metric → respond inline:
   > **[Metric]:** [value] (as of [date]) | vs prior: [delta]
5. Multi-metric or table → write to .xlsx with openpyxl, present the file

## Common Patterns

### Portfolio balance (latest snapshot)
```sql
SELECT country_code, COUNT(DISTINCT company_id) AS accounts,
       SUM(balance_usd) AS total_balance
FROM capital_markets_dm.loc_tape
WHERE dt = (SELECT MAX(dt) FROM capital_markets_dm.loc_tape)
  AND charge_off_flag = false AND is_in_repayment = false
GROUP BY country_code
```

### Delinquency distribution
```sql
SELECT dq_bucket, COUNT(DISTINCT company_id) AS accounts,
       SUM(balance_usd) AS balance
FROM capital_markets_dm.loc_tape
WHERE dt = (SELECT MAX(dt) FROM capital_markets_dm.loc_tape)
  AND charge_off_flag = false AND is_in_repayment = false
GROUP BY dq_bucket
ORDER BY dq_bucket
```

### Monthly charge-offs
```sql
SELECT DATE_TRUNC('month', charge_off_dt) AS month,
       COUNT(DISTINCT company_id) AS accounts,
       SUM(balance_usd) AS charged_off_balance
FROM capital_markets_dm.loc_tape
WHERE charge_off_dt IS NOT NULL
  AND dt = charge_off_dt
GROUP BY 1 ORDER BY 1
```

### Revenue by product
```sql
SELECT product, sub_product,
       SUM(revenue_usd) AS total_revenue,
       SUM(gtv_usd) AS total_gtv
FROM master_transactions_dm.transactions_ssot
WHERE posted_at BETWEEN '2025-01-01' AND '2025-12-31'
  AND is_company_test = false
GROUP BY product, sub_product
ORDER BY total_revenue DESC
```

## Rules

- Always state the snapshot date in the response
- MX balances are USD unless explicitly asked for MXN (default rate: 17.5)
- "Active" accounts = balance_usd > 0 AND charge_off_flag = false AND is_in_repayment = false
- Revenue questions → use transactions_ssot.revenue_usd
- Spend questions → use loc_tape.disbursement_amount_usd
- Always exclude test companies
- Use COALESCE when combining amount columns in arithmetic
