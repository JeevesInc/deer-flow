---
name: jeeves-sql-library
description: Use this skill when the user asks for a specific SQL analysis, references a query by name, or needs a query pattern for data tape, DQ, roll rates, repayment, rollforward, revenue, credit scoring, recoveries, collections, or single-customer lookups. Also use when the user says "run the [X] query" or "find me the [X] SQL."
allowed-tools:
  - bash
  - read_file
---

# Jeeves SQL Library

The team maintains a library of ~56 SQL files in the `.github/sql/` directory of the Capital Markets Google Drive workspace. These are production-tested query templates for capital markets analytics.

**Drive location:** `.github/` folder ID `1Vs2emp9jbSF3AbTqn6jkdqIHNIOy50yK`, then navigate to `sql/` subfolder.

## SQL Catalog

### Data Tape
Generate lender-facing data tapes with loan-level detail.

| File | Description |
|------|-------------|
| `loc_data_tape.sql` | LOC data tape — standard lender format |
| `gwc_data_tape.sql` | GWC data tape — standard lender format |
| `loc_data_tape_granular.sql` | LOC granular tape with additional fields |
| `loc_data_tape_*.sql` | Other LOC tape variants (lender-specific formats) |

### DQ / Delinquency
Delinquency aging, DPD buckets, and DQ trending.

| File | Description |
|------|-------------|
| `loc_dq_*.sql` | LOC delinquency queries (aging buckets, trends) |
| `gwc_dq_*.sql` | GWC delinquency queries |
| Delinquency aging | DPD bucket distribution over time |

### Roll Rates
Transition matrices showing how accounts move between DPD buckets.

| File | Description |
|------|-------------|
| `loc_roll_rates.sql` | LOC roll rate transition matrix |
| `gwc_roll_rates.sql` | GWC roll rate transition matrix |

### Repayment
Repayment plan tracking and payment performance.

| File | Description |
|------|-------------|
| `loc_repayment_*.sql` | LOC repayment plan queries |

### Rollforward
Balance rollforward reporting (BOP → disbursements → payments → charge-offs → EOP).

| File | Description |
|------|-------------|
| `loc_rollforward.sql` | LOC balance rollforward |

### Revenue
Revenue analysis and breakdown queries.

| File | Description |
|------|-------------|
| Revenue queries | Revenue by product, country, time period |

### Credit / Scoring
JURS credit model and scoring queries.

| File | Description |
|------|-------------|
| JURS model queries | Jeeves Underwriting Risk Score model |
| Credit scoring queries | Score distribution, migration, performance |

### Recoveries
Tracking of charged-off account recoveries.

| File | Description |
|------|-------------|
| Recovery queries | Post-charge-off recovery tracking |

### Collections
Collections pipeline and RMS (Recovery Management System) data.

| File | Description |
|------|-------------|
| Collections pipeline | Active collections queue and status |
| RMS data queries | External collections agency data |

### Single Customer Lookups
Detailed queries for a single company/customer.

| File | Description |
|------|-------------|
| `single_company_*.sql` | All data for a specific company (balance, payments, DQ history) |

## How to Use

1. **Find the SQL file** — Browse the `.github/sql/` folder to locate the exact file:
   ```bash
   python /mnt/skills/custom/google-drive/list_drive_folder.py "1Vs2emp9jbSF3AbTqn6jkdqIHNIOy50yK" --recursive
   ```

2. **Fetch the SQL** — Download the query template:
   ```bash
   python /mnt/skills/custom/google-drive/fetch_doc.py "<FILE_ID>"
   ```

3. **Adapt and run** — Modify the SQL for the user's specific parameters:
   - Replace date range placeholders with the requested period
   - Replace country/company filters as needed
   - Use the `jeeves-redshift` skill for connection details
   - Execute via bash with psycopg2

## Rules

- **Always adapt SQL to current parameters** — never run a template query without updating dates, filters, and thresholds
- Reference the `jeeves-redshift` skill for Redshift connection boilerplate and schema details
- State data freshness: tell the user what date range the results cover
- If the exact SQL file name is unknown, list the folder first — don't guess file names
- For ad-hoc analytics not covered by a template, write custom SQL using the `jeeves-analytics` skill patterns
