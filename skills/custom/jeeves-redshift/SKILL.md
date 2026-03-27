---
name: jeeves-redshift
description: Use this skill when the user asks questions about Jeeves data — portfolio balances, transactions, revenue, delinquency, charge-offs, company info, or any Redshift data warehouse query. This skill provides the connection method and full schema reference for all confirmed Jeeves Redshift tables.
allowed-tools:
  - bash
  - write_file
  - read_file
---

# Jeeves Redshift Access

## Setup

Before running any query, install the driver (once per session):

```bash
pip install psycopg2-binary
```

## Connection

```python
import psycopg2, os

conn = psycopg2.connect(
    host=os.environ['REDSHIFT_HOST'],
    port=int(os.environ['REDSHIFT_PORT']),
    dbname=os.environ['REDSHIFT_DB'],
    user=os.environ['REDSHIFT_USER'],
    password=os.environ['REDSHIFT_PASSWORD'],
    sslmode='require',
    sslrootcert='disable'
)
cur = conn.cursor()
cur.execute("<query>")
columns = [desc[0] for desc in cur.description]
rows = cur.fetchall()
conn.close()
```

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
- **Active portfolio:** `WHERE charge_off_flag = false` on loc_tape/gwc_tape
- **DPD buckets:** current (0), 1-30, 31-60, 61-90, 90+
- **Date filtering on loc_tape/gwc_tape:** Always use `dt` column with BETWEEN, never DATE_TRUNC
- **NULL arithmetic:** When adding columns, wrap each in `COALESCE(col, 0)` — NULL + value = NULL
- **Revenue vs spend:** `revenue_usd` from transactions_ssot = Jeeves revenue; `disbursement_amount_usd` from loc_tape = customer spend
- **String matching:** Use `ILIKE '%pattern%'` for case-insensitive partial matches
- **CTEs:** Fully supported and encouraged for complex queries

## Output Rules

- Single metric or <10 rows: respond inline in Slack
- Large dataset or multi-metric: write to .xlsx using openpyxl, present the file, and summarize inline
