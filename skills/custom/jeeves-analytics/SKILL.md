---
name: jeeves-analytics
description: Use this skill for ad-hoc analytical questions about the Jeeves credit portfolio — DPD rates, charge-off trends, portfolio balances, revenue breakdowns, vintage analysis, and concentration reports. Builds on the jeeves-redshift skill for data access.
allowed-tools:
  - bash
  - write_file
  - read_file
---

# Jeeves Portfolio Analytics

> **Accuracy is mandatory.** Every fact, number, and claim in your output must come from a verified source — a Redshift query result, a document you have actually read, or an explicit user statement. Never guess, assume, extrapolate, or fill gaps with general knowledge. If you do not have a source, say so. Mark unverified items as **[Needs Confirmation]**. Getting it wrong is worse than leaving it blank.


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

## Forecasting / predictive-model asks — HARD RULES (added 2026-08-19 from the Jeeves Pay spend model, thread e405278b)

1. **Never report "no predictive value" or "too noisy" until calendar features are exhausted.** `dow` + `dom` + crude first/last-week flags are NOT calendar features. Test ordinal/compound ones before any negative conclusion: **last-weekday-of-month, nth-weekday (e.g. last Friday), business-day-of-month, days-to-month-end, payday proximity, holiday adjacency**. Brian pushed back twice on exactly this ("did you look at day of week? like the last friday of every month", "I dont believe that theres no predictive value here") and the features were in fact material.
2. **Validate at the ENTITY level, not the portfolio level.** "i care about a name by name basis, not portfolio" (2026-08-18). The default deliverable for any spend/receivables forecast is **top-50 names, forecast vs actual, with charts** — scatter, sorted per-name error bars, and per-name time series. A portfolio-aggregate WAPE answers a question he did not ask.
3. **Pick the metric that matches the decision.** On a panel that is ~98% zeros, amount-error metrics (WAPE/MAPE) are dominated by noise and hide real skill; score **ranking and dollar concentration** (top-decile share of actual dollars, hit rate) alongside amounts, and say which metric drives the decision (T-1 cash movement = ranking + magnitude bands, not point accuracy).
4. **Always show out-of-sample walk-forward** with the calibration fitted only on pre-cutoff data, and state the cutoffs explicitly. Brian asks for validation every single time ("yes, and well need to see a validation") — build it into the first delivery.
5. **Compute budget: a modeling run that will exceed ~15 minutes must be chunked.** The harness cancels a task at 20 minutes and the finished write-up is lost (happened 2026-08-18 14:51). Persist each stages output (Excel/notebook/JSON) to disk as it completes, then report from the artifact. Do NOT post an interim "building..." line — a bare `?` from Brian is the cost of that.

## Rules

- Always state the snapshot date in the response
- MX balances are USD unless explicitly asked for MXN. Use `spot_rate` from loc_tape for conversion — do NOT hardcode an FX rate.
- "Active" accounts = balance_usd > 0 AND charge_off_flag = false AND is_in_repayment = false
- Revenue questions → use transactions_ssot.revenue_usd
- Spend questions → use loc_tape.disbursement_amount_usd
- Always exclude test companies
- Use COALESCE when combining amount columns in arithmetic
