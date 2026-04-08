---
name: jeeves-redshift
description: Use this skill when the user asks questions about Jeeves data — portfolio balances, transactions, revenue, delinquency, charge-offs, company info, or any Redshift data warehouse query. This skill provides the connection method and full schema reference for all confirmed Jeeves Redshift tables.
allowed-tools:
  - bash
  - write_file
  - read_file
---

# Jeeves Redshift Access

**CRITICAL: Redshift data is only available through yesterday.** Never use today's date or future dates in queries — the data will be incomplete or missing. When the user says "current" or "latest", use yesterday's date.

## Running Queries

Use the SQL runner — it handles connection, validation, and output formatting automatically:

```bash
# Inline query
python /mnt/skills/custom/jeeves-redshift/sql_runner.py "SELECT SUM(balance_usd) FROM capital_markets_dm.loc_tape WHERE dt = '2026-04-02' AND charge_off_flag = false AND is_in_repayment = false"

# Query from file
python /mnt/skills/custom/jeeves-redshift/sql_runner.py --file /mnt/user-data/workspace/query.sql

# Save large results to Excel
python /mnt/skills/custom/jeeves-redshift/sql_runner.py "SELECT ..." --output /mnt/user-data/outputs/results.xlsx

# Save as CSV
python /mnt/skills/custom/jeeves-redshift/sql_runner.py "SELECT ..." --output /mnt/user-data/outputs/results.csv

# Increase inline display limit (default 100 rows)
python /mnt/skills/custom/jeeves-redshift/sql_runner.py "SELECT ..." --limit 500

# Generate SQL from natural language (uses DSPy + Claude)
python /mnt/skills/custom/jeeves-redshift/sql_runner.py --generate "What's the total portfolio balance by country?"
```

The runner automatically:
- Validates SQL before executing (catches common mistakes, warns on missing filters)
- Blocks non-SELECT queries
- Formats small results inline, saves large results to files
- Provides actionable error hints on failure

## Key Tables

| Table | Description |
|-------|-------------|
| `master_transactions_dm.transactions_ssot` | All transactions — revenue, GTV, fees, FX. 88 columns. |
| `master_customer_dm.companies_dm` | Company master dimension — 167 columns covering company profile, credit, activation, deposits, activity. |
| `capital_markets_dm.loc_tape` | LOC (Line of Credit) daily tape — balance snapshots, disbursements, payments, delinquency, charge-offs. By company by day. |
| `capital_markets_dm.gwc_tape` | GWC (Global Working Capital) daily tape — principal, interest, VAT/fees/tax breakdowns, obligation tracking. By company by day. |
| `analytics_sandbox.loc_vintage_data` | LOC vintage/cohort data — BOP/EOP balances, charge-offs, repayments, period-over-period deltas. |

### TODO — Tables to add after verification:
- `analytics_sandbox.jurs_test` — JURS loss rate scores (needs validation)
- `capital_markets_dm.rms_transactions` — RMS transaction-level collections data (needs validation)
- Borrowing base table — not found in schema, may be derived or in a different location

---

## Table Details

### master_transactions_dm.transactions_ssot

The single source of truth for all Jeeves transactions. Use this for **revenue** and **GTV** questions.

**Key columns:**
- `transaction_reference_id` (varchar): Unique transaction ID
- `created_at` (date), `posted_at` (timestamp), `completed_at` (timestamp): Transaction dates
- `product` (varchar): Product type
- `sub_product` (varchar): Sub-product
- `product_category` (varchar): Product category
- `transaction_type_tag` (varchar): Transaction classification
- `transaction_status` (varchar): Status
- `is_reversal` (boolean): Whether this is a reversal
- `is_cross_border` (boolean): Cross-border flag
- `send_currency` (integer), `send_currency_alphacode` (char): Sending currency
- `send_amount` (double): Amount sent
- `receive_currency` (integer), `receive_currency_alphacode` (char): Receiving currency
- `receive_amount` (double): Amount received
- `transaction_amount_usd` (double): Transaction amount in USD
- `gtv_usd` (double): Gross transaction value in USD
- `revenue_usd` (numeric): **Total revenue in USD — primary revenue metric**
- `revenue_fx_usd` (numeric): FX revenue
- `revenue_fx_markup_usd` (numeric): FX markup revenue
- `revenue_interchange_usd` (numeric): Interchange revenue
- `revenue_interest_usd` (numeric): Interest revenue
- `revenue_jeevespay_usd` (numeric): Jeeves Pay revenue
- `revenue_gmf_fee_usd` (numeric): GMF fee revenue
- `revenue_wallet_transfer_usd` (numeric): Wallet transfer revenue
- `revenue_other_fees_usd` (numeric): Other fee revenue
- `company_id` (bigint): **JOIN KEY** to all other tables
- `company_name` (varchar): Company name
- `company_country` (varchar): Company country
- `company_country_code` (integer): Country code
- `is_company_test` (boolean): Test company flag — exclude in production queries
- `beneficiary_name` (varchar): Merchant/beneficiary name
- `beneficiary_mcc` (bigint): Merchant category code
- `beneficiary_merchant_category` (varchar): Merchant category
- `card_type` (varchar): Card type
- `processor` (varchar): Payment processor
- `payment_rail` (varchar): Payment rail

### master_customer_dm.companies_dm

Company master dimension. Use for company metadata, credit info, activation details.

**Key columns:**
- `company_id` (bigint): **JOIN KEY** to all other tables
- `name` (varchar): Company name
- `legal_name` (varchar): Legal name
- `business_name` (varchar): Business name
- `country_code` (integer): Country (840=US, 484=MX, 170=CO, 124=CA, 76=BR)
- `country_name` (varchar): Country name
- `state_name` (varchar): State/region
- `city_name` (varchar): City
- `platform_status` (varchar): Platform status
- `activity_status` (varchar): Activity status
- `credit_limit` (numeric): Credit limit
- `credit_limit_usd` (numeric): Credit limit in USD
- `current_balance` (numeric): Current balance
- `current_days_past_due` (numeric): Current DPD
- `current_delinquent_dt` (date): Current delinquency date
- `max_days_past_due` (numeric): Historical max DPD
- `activation_date` (date): First activation date
- `last_transaction_date` (date): Most recent transaction
- `naics_industry_id` (varchar): NAICS industry code
- `is_test` (boolean): Test company flag
- `funding_source` (varchar): Funding source
- `loc_account_status` (varchar): LOC account status
- `in_collection_date` (date): Collection start date
- `charged_off_date` (date): Charge-off date
- `subscription_type` (varchar): Subscription type
- `sales_rep_email` (varchar): Sales rep
- `kam_email` (varchar): Key account manager

### capital_markets_dm.loc_tape

Daily LOC portfolio tape. One row per company per day. Use for **balances, disbursements, payments, delinquency, charge-offs**.

**Key columns:**
- `dt` (date): **Snapshot date — ALWAYS filter on this for time-series queries**
- `company_id` (integer): **JOIN KEY**
- `loan_id` (integer): Loan identifier
- `country_code` (integer): Country (840=US, 484=MX, 170=CO)
- `product` (varchar): Always 'LOC'
- `status` (varchar): ACTIVE, CHURNED, INACTIVE
- `balance_usd` (numeric): Total outstanding balance in USD
- `card_balance_usd` (numeric): Card balance
- `jp_balance_usd` (numeric): Jeeves Pay balance
- `disbursement_amount_usd` (numeric): Card spend/disbursements
- `jeeves_pay_disbursement_amount_usd` (numeric): Jeeves Pay spend
- `payment_amount_usd` (numeric): Payments received
- `days_past_due` (integer): Days overdue (0 = current)
- `dq_bucket` (varchar): Delinquency bucket (current, 1-30, 31-60, 61-90, 90+)
- `dq_bucket_daily` (varchar): Daily DQ bucket
- `dq_bucket_monthly` (varchar): Monthly DQ bucket
- `delinquent_dt` (date): Date became delinquent
- `charge_off_dt` (date): Charge-off date
- `charge_off_flag` (boolean): True if charged off
- `is_in_repayment` (boolean): Repayment plan flag
- `repayment_dt` (date): Repayment start date
- `state_code` (varchar): State code
- `city_name` (varchar): City

**Charge-off amount:** `balance_usd WHERE dt = charge_off_dt` (never use deprecated `v0_charge_off_amount_usd`)

### capital_markets_dm.gwc_tape

Daily GWC (Global Working Capital / Jeeves Pay loans) tape. One row per loan per day.

**Key columns:**
- `dt` (date): **Snapshot date**
- `company_id` (bigint): **JOIN KEY**
- `loan_id` (bigint): Loan identifier
- `loan_reference_number` (varchar): External reference
- `is_bundled` (boolean): Whether bundled with LOC
- `product` (varchar): Product type
- `country_code` (integer): Country
- `principal_disbursed` / `principal_disbursed_usd`: Principal disbursed
- `principal_amount_due` / `principal_amount_due_usd`: Principal due
- `interest_amount_due` / `interest_amount_due_usd`: Interest due
- `fee_amount_due` / `fee_amount_due_usd`: Fees due
- `tax_amount_due` / `tax_amount_due_usd`: Tax due
- `payment_amount` / `payment_amount_usd`: Payments
- `balance` / `balance_usd`: Total balance
- `principal_balance` / `principal_balance_usd`: Principal balance
- `interest_balance` / `interest_balance_usd`: Interest balance
- `overpay_balance` / `overpay_balance_usd`: Overpayment balance
- `days_past_due` (integer): Days overdue
- `dq_bucket_daily` (varchar): Daily DQ bucket
- `dq_bucket_monthly` (varchar): Monthly DQ bucket
- `delinquent_dt` (date): Delinquency date
- `charge_off_dt` (date): Charge-off date
- `charge_off_flag` (boolean): Charged off
- `status` (varchar): Status

### analytics_sandbox.loc_vintage_data

Vintage/cohort analysis data for LOC portfolio.

**Key columns:**
- `company_id` (bigint): Company
- `country_code` (bigint): Country
- `start_date` (date): Period start
- `end_date` (date): Period end
- `bop_balance_usd` (double): Beginning-of-period balance
- `eop_balance_usd` (double): End-of-period balance
- `bop_days_past_due` (bigint): BOP DPD
- `eop_days_past_due` (bigint): EOP DPD
- `card_disbursement_amount_usd` (double): Card disbursements in period
- `jeeves_pay_disbursement_amount_usd` (double): JP disbursements
- `payment_amount_usd` (double): Payments
- `charge_off_usd` (double): Charge-offs
- `repayment_usd` (double): Repayments
- `change_in_balances` (double): Balance delta
- `intra_period_transactions` (double): Intra-period activity

---

## Query Rules

- **READ-ONLY:** Only SELECT queries. Never INSERT, UPDATE, DELETE, DROP, etc.
- **JOIN KEY:** `company_id` exists in ALL tables — it is the universal join key
- **Filter test companies:** `WHERE is_company_test = false` (transactions_ssot) or `WHERE is_test = false` (companies_dm)
- **Filter by market:** Use `country_code` — 840=US, 484=MX, 170=CO, 124=CA, 76=BR
- **Latest snapshot:** `WHERE dt = (SELECT MAX(dt) FROM capital_markets_dm.loc_tape)`
- **Active portfolio:** `WHERE charge_off_flag = false AND is_in_repayment = false` on loc_tape (ALWAYS include both filters)
- **DPD buckets:** current (0), 1-30, 31-60, 61-90, 90+
- **Date filtering on loc_tape/gwc_tape:** Always use `dt` column with BETWEEN, never DATE_TRUNC
- **NULL arithmetic:** When adding columns, wrap each in `COALESCE(col, 0)` — NULL + value = NULL
- **Revenue vs spend:** `revenue_usd` from transactions_ssot = Jeeves revenue; `disbursement_amount_usd` from loc_tape = customer spend
- **String matching:** Use `ILIKE '%pattern%'` for case-insensitive partial matches
- **CTEs:** Fully supported and encouraged for complex queries

## Query Examples by Intent

## SQL Style\nALWAYS write SQL with leading commas, not trailing commas. Example:\n  SELECT\n      col1\n    , col2\n    , col3\n  FROM ...\n  ORDER BY\n      col1\n    , col2\n\n## RPP Data Model\nRPP accounts are LOC balances financed into GWC loans. Key facts:\n- Identify via: gwc_tape WHERE loan_reference_number ILIKE 'RPP%'\n- One row per installment = rows WHERE principal_amount_due_usd != 0\n- dt on those rows = the invoice due date\n- balance_usd = running unpaid balance at that point\n- days_past_due = days since that installment was due\n- delinquent_dt = date the current installment became overdue\n- 96 RPP loans as of 2026-04-07, 666 installment rows total\n- Do NOT use dms_mysql_jeeves_raw.instalments or company_statements for RPP — gwc_tape is the source of truth\n\nUse these as templates. Every loc_tape query MUST include `charge_off_flag = false AND is_in_repayment = false` to get the active portfolio.

### Balance (portfolio snapshot)
**Q:** "What's the total portfolio balance?"
```sql
SELECT SUM(balance_usd) AS total_balance
FROM capital_markets_dm.loc_tape
WHERE dt = (SELECT MAX(dt) FROM capital_markets_dm.loc_tape)
  AND charge_off_flag = false AND is_in_repayment = false
```

### Spend / disbursements
**Q:** "How much did customers spend on cards in March?"
```sql
SELECT SUM(COALESCE(disbursement_amount_usd, 0)) AS card_spend
FROM capital_markets_dm.loc_tape
WHERE dt BETWEEN '2026-03-01' AND '2026-03-31'
  AND charge_off_flag = false AND is_in_repayment = false
```
⚠️ `disbursement_amount_usd` = customer card spend. Do NOT use `loan_allocation_amount` (internal accounting).

### Delinquency / DPD
**Q:** "What's our 90+ DPD rate by country?"
```sql
SELECT country_code,
       SUM(CASE WHEN days_past_due > 90 THEN balance_usd ELSE 0 END)
         / NULLIF(SUM(balance_usd), 0) AS dpd_90plus_rate,
       SUM(balance_usd) AS total_balance
FROM capital_markets_dm.loc_tape
WHERE dt = (SELECT MAX(dt) FROM capital_markets_dm.loc_tape)
  AND charge_off_flag = false AND is_in_repayment = false
GROUP BY country_code
```

### Charge-offs
**Q:** "What was the charge-off amount for company 456?"
```sql
SELECT company_id, balance_usd AS charge_off_amount, charge_off_dt
FROM capital_markets_dm.loc_tape
WHERE company_id = 456 AND dt = charge_off_dt
```
⚠️ Charge-off amount = `balance_usd` on the `charge_off_dt` date. Never use deprecated `v0_charge_off_amount_usd`.

### Revenue
**Q:** "Total revenue in Q1 2026?"
```sql
SELECT SUM(revenue_usd) AS total_revenue
FROM master_transactions_dm.transactions_ssot
WHERE posted_at BETWEEN '2026-01-01' AND '2026-03-31'
  AND is_company_test = false
```
⚠️ Revenue is ONLY in `transactions_ssot.revenue_usd`. The `loc_tape` table has spend, not revenue.

### Date ranges on loc_tape
```sql
-- CORRECT: use BETWEEN on dt
WHERE dt BETWEEN '2025-10-01' AND '2025-10-31'

-- WRONG: never DATE_TRUNC on dt
-- WHERE DATE_TRUNC('month', dt) = '2025-10-01'  ← DO NOT DO THIS
```

### Company lookup with join
**Q:** "Top 10 borrowers by balance with company names"
```sql
SELECT c.name, c.country_name, lt.balance_usd
FROM capital_markets_dm.loc_tape lt
JOIN master_customer_dm.companies_dm c ON c.company_id = lt.company_id
WHERE lt.dt = (SELECT MAX(dt) FROM capital_markets_dm.loc_tape)
  AND lt.charge_off_flag = false AND lt.is_in_repayment = false
  AND c.is_test = false
ORDER BY lt.balance_usd DESC LIMIT 10
```

---

## Output Rules

- Single metric or <10 rows: respond inline in Slack
- Large dataset or multi-metric: write to .xlsx using openpyxl, present the file, and summarize inline

---

## dms_mysql_jeeves_raw Schema

Raw MySQL replication of the Jeeves production database via AWS DMS. **209 tables, 3,530 columns.** Source-of-truth operational data — use for company/user/card/loan/transfer details not available in the DM layer.

**Key differences from DM layer:**
- Column names are camelCase (e.g. `companyid`, `createdat`, `deletedat`)
- No `_usd` suffix — amounts are in native currency unless column ends in `usd`
- Booleans stored as `smallint` (0/1), not boolean
- Soft deletes: always add `WHERE deletedat IS NULL` for active records
- `_dms_created` = DMS replication timestamp, not business creation time
- Join to DM layer: `dms_mysql_jeeves_raw.companies.id` = `master_customer_dm.companies_dm.company_id`

### Core Entity Tables

| Table | Cols | Description |
|-------|------|-------------|
| `companies` | 102 | Raw company records — EIN, business type, billing method, credit line, status, KYB/KYC, platform currency |
| `users` | 72 | All users — role, email, spend limit, 2FA, status, login history |
| `cards` | 48 | Physical/virtual cards — cardstatus, spendlimit, cardtype, processor IDs (Galileo, Stripe, Tutuka) |
| `primary_account_owners` | 37 | KYB primary account owners — identity verification, TruNarrative |
| `business_ownerships` | 39 | Beneficial owners — percentofownership, KYB status, isdirector |
| `invite_users` | 39 | Invited users — role, spendlimit, department, location |
| `waitlists` | 47 | Onboarding waitlist/leads — country, product interest, referral |

### Transaction & Payment Tables

| Table | Cols | Description |
|-------|------|-------------|
| `transactions` | 65 | Raw card transactions — acttype, networkcode, transactionamount, merchantid, cardid, transactionstatus |
| `transfers` | 51 | Jeeves Pay / wire transfers — beneficiaryid, transferamount, paymenttype, transferstatus, usdamount |
| `loans` | 77 | GWC/JP loans — principal, interestrate, originationfeerate, VAT, term, disbursementtype, paymentstructure |
| `loan_payments` | 17 | Loan payment records — loanid, paymentdate, paymentsource, transactionstatus |
| `instalments` | 30 | Loan instalment schedule — duedate, paiddate, status, dayspastdue, usdcurrencyamount |
| `statement_transactions` | 35 | Statement-level transactions — producttype, billingcycletag, usdcurrencyamount |
| `reimbursements` | 31 | Employee reimbursements — amount, currency, merchantname, expensecategory, paymentstatus |

### Billing & Statements

| Table | Cols | Description |
|-------|------|-------------|
| `company_statements` | 52 | Monthly billing statements — month/year, invoicedamount, endingbalance, duedate, autodebit, settlementdatetime |
| `company_billing_configurations` | 14 | Billing day, due date interval, billing interval per company |
| `company_bank_accounts` | 61 | Linked bank accounts — ACH/Plaid details, autodebit status, verification status |
| `bank_accounts` | 21 | Internal credit line ledger — cl (credit limit), ab (available balance), tb (total balance), bb (billing balance) |

### Beneficiaries & Payments

| Table | Cols | Description |
|-------|------|-------------|
| `beneficiaries` | 11 | Payment beneficiary master — companyid, beneficiaryname |
| `beneficiary_payment_details` | 50 | Bank details — CLABE, SWIFT/BIC, routing, account number, currency, country |
| `transfer_details` | 22 | Transfer detail records |
| `transfer_approvals` | 12 | Approval workflow for transfers |

### Credit & Collateral

| Table | Cols | Description |
|-------|------|-------------|
| `collateral_records` | 24 | Collateral — type, local/USD amount, advancerate, effectivecollateralvalue, expiry, status |
| `credit_product_requests` | 13 | Credit line requests |
| `bank_accounts` | 21 | cl/ab/tb/bb/ar ledger columns (credit line accounting) |

### KYB / Compliance / Tax

| Table | Cols | Description |
|-------|------|-------------|
| `company_tax_status` | 20 | MX SAT tax status — RFC, regimens, obligations, economic activity |
| `company_tax_returns` | 28 | MX SAT annual tax returns — ISR, deductions, declarations |
| `sardine_audit_logs` | 10 | Sardine fraud/risk audit logs |
| `sardine_transaction_risks` | 14 | Transaction-level risk scores from Sardine |
| `device_fingerprints` | 27 | Device fingerprint records for fraud detection |

### SAT / Mexico Invoicing

| Table | Cols | Description |
|-------|------|-------------|
| `satws_invoices` | 43 | SAT invoices (CFDI) — issuer, receiver, total, tax, status, issuedat |
| `satws_invoice_items` | 19 | Line items on SAT invoices |
| `satws_invoice_payments` | 20 | Payment records against SAT invoices |
| `satws_extractions` | 19 | SAT data extraction jobs |
| `company_satws_accounts` | 18 | Company SAT account linkages |

### Cards & Spend Controls

| Table | Cols | Description |
|-------|------|-------------|
| `card_transaction_details` | 21 | Detailed card transaction metadata |
| `temporary_spend_limits` | 19 | Temporary spend limit overrides |
| `needs_attention_items` | 26 | Expense policy violations — missing receipts, notes, policy breaches |
| `policies` | 17 | Spend policies |
| `mcc_custom_categories` | 13 | Custom MCC category mappings |

### Open Banking & FX

| Table | Cols | Description |
|-------|------|-------------|
| `open_banking_balances` | 12 | Open banking balance snapshots |
| `open_banking_transactions` | 28 | Open banking transaction feed |
| `exchange_rates` | 11 | Exchange rate records |
| `exchange_rate_logs` | 13 | Exchange rate change log |
| `routefusion_entities` | 22 | Routefusion payment entity records |
| `cross_border_fees` | 11 | Cross-border fee schedules |

### Example Joins

```sql
-- Company name + credit limit from raw + DM
SELECT r.id, r.name, r.ein, c.credit_limit_usd, c.activity_status
FROM dms_mysql_jeeves_raw.companies r
JOIN master_customer_dm.companies_dm c ON c.company_id = r.id
WHERE r.deletedat IS NULL AND c.is_test = false

-- Active cards for a company
SELECT c.id, c.cardtype, c.cardstatus, c.spendlimit, u.email
FROM dms_mysql_jeeves_raw.cards c
JOIN dms_mysql_jeeves_raw.users u ON u.id = c.userid
WHERE c.companyid = 12345 AND c.deletedat IS NULL

-- Loan instalment schedule
SELECT i.id, i.duedate, i.paiddate, i.usdcurrencyamount, i.status, i.dayspastdue
FROM dms_mysql_jeeves_raw.instalments i
JOIN dms_mysql_jeeves_raw.loans l ON l.id = i.loanid
WHERE l.companyid = 12345 AND i.deletedat IS NULL
ORDER BY i.duedate

-- SAT invoices for a company
SELECT s.invoiceid, s.total, s.tax, s.status, s.issuedat
FROM dms_mysql_jeeves_raw.satws_invoices s
WHERE s.companyid = 12345
ORDER BY s.issuedat DESC
```

### Full Table List (209 tables)
accounting_sync_cycles, activity_logs, admin_settings, admins, all_product_transactions, api_client_calendar_configurations, api_client_credentials, auto_debit_bank_accounts, autopay_logs, backend_dead_letters, bank_account_link_logs, bank_accounts, bank_histories, banks, beneficiaries, beneficiary_payment_details, bill_pay_billers, bulk_transfers, business_ownerships, card_digital_wallets, card_product_configurations, card_product_requests, card_requests, card_shipment_approvals, card_transaction_details, card_transaction_settlement, cards, cards_gateway_dead_letters, cities, collateral_records, companies, company_account_mappings, company_accounting_integration_settings, company_accounting_integrations, company_addresses, company_bank_accounts, company_bank_accounts_fallback, company_billing_configuration_logs, company_billing_configurations, company_business_leaderships, company_cardservices, company_contacts, company_investors, company_jp_credit_contact_details, company_mexico_credit_bureau_data, company_migrations, company_operating_cou
