"""US BB v2 — replaces broken companies_dm with working alternatives."""
import sys, os, datetime as dt, psycopg2
sys.path.insert(0, '/mnt/skills/custom/jeeves-borrowing-base')

from redshift_util import ensure_deps
ensure_deps('xlsxwriter')
import pandas as pd
from pathlib import Path
from eligibility import calculate_eligibility_fields, calculate_eligibility_summary

date_end = dt.date(2026, 5, 3)
date_beg = date_end.replace(day=1) - dt.timedelta(days=1)  # 2026-04-30
print(f"US Borrowing Base: BOP={date_beg}, EOP={date_end}")

def new_con(timeout_ms=300000):
    return psycopg2.connect(
        host=os.environ['REDSHIFT_HOST'], port=int(os.environ['REDSHIFT_PORT']),
        dbname=os.environ['REDSHIFT_DB'], user=os.environ['REDSHIFT_USER'],
        password=os.environ['REDSHIFT_PASSWORD'], sslmode='require', sslrootcert='disable',
        connect_timeout=30, options=f'-c statement_timeout={timeout_ms}',
    )

TAPE_SQL = """
with
collateral_totals as (
    select
        companyid
        , sum(collateralamountusd) as total_collateral_amount_usd
    from
        dms_mysql_jeeves_raw.collateral_records
    group by 1
),
max_dq as (
    select
        company_id
        , max(days_past_due) as max_dpd
    from
        capital_markets_dm.loc_tape
    where
        dt >= dateadd(day, -180, '{date}')
    group by 1
),
sofom_transfer as (
    select
        c.id
        , c.name
        , cs.settingValue as transfer_flag
        , cs.updatedat as assignment_dt
    from
        dms_mysql_jeeves_raw.companies c
    inner join
        dms_mysql_jeeves_raw.company_settings cs on cs.companyId = c.id
    where
        cs.settingKey = 'SOFOM_JPMORGAN_ENABLED'
),
entity_balances_base as (
    select
        dt, company_id,
        sum(case when assignment_dt::date = dt then balance end) over (partition by company_id) as jvs_transfer_balance,
        assignment_dt,
        sum(case when t.assignment_dt::date < lt.dt then credit_amount end)
            over (partition by company_id order by dt rows unbounded preceding) as cumulative_credits
    from capital_markets_dm.loc_tape lt
    join sofom_transfer t on t.id = lt.company_id and t.assignment_dt::date <= lt.dt
),
entity_balances as (
    select dt, company_id, jvs_transfer_balance, assignment_dt, cumulative_credits,
        greatest(jvs_transfer_balance + coalesce(cumulative_credits, 0), 0) as jvs_remaining
    from entity_balances_base
),
credit_limits as (
    select company_id, cl_usd as credit_limit_usd
    from (
        select company_id, cl_usd,
            row_number() over (partition by company_id order by end_of_mth desc) as rn
        from analytics_sandbox.credit_limit_usd_history
    ) t
    where rn = 1
),
raw_cos as (
    select
        id as company_id
        , name
        , ein
        , coalesce(naicsindustryid, 9999) as naics_industry_id
        , creditlineassignationdate as onboarding_date
    from dms_mysql_jeeves_raw.companies
)
select
    lt.dt, lt.company_id, lt.loan_id, lt.country_code, lt.product, lt.currency,
    lt.delinquent_dt, lt.days_past_due, lt.dq_bucket, lt.dq_bucket_daily, lt.dq_bucket_monthly,
    lt.charge_off_exclusion, lt.charge_off_dt, lt.charge_off_flag,
    lt.disbursement_amount, lt.payment_amount, lt.cashback_amount,
    lt.late_payment_penalty_amount, lt.jeeves_pay_disbursement_amount, lt.jeeves_pay_fee_amount,
    lt.loan_allocation_amount, lt.fx_adjustment_amount, lt.adjustment_amount,
    lt.debit_amount, lt.credit_amount, lt.balance,
    case when st.transfer_flag = 'on' then eb.jvs_remaining end as jvs_remaining,
    case when st.transfer_flag = 'on' then lt.balance - eb.jvs_remaining end as sofom_balance,
    case when st.transfer_flag = 'on' then (lt.balance - eb.jvs_remaining) * lt.spot_rate end as sofom_balance_usd,
    st.transfer_flag,
    lt.card_balance, lt.jp_balance, lt.jp_principal_balance, lt.jp_interest_balance,
    lt.overpay_balance, lt.invoiced, lt.spot_rate,
    lt.disbursement_amount_usd, lt.payment_amount_usd, lt.cashback_amount_usd,
    lt.late_payment_penalty_amount_usd, lt.jeeves_pay_disbursement_amount_usd, lt.jeeves_pay_fee_amount_usd,
    lt.loan_allocation_amount_usd, lt.fx_adjustment_amount_usd, lt.adjustment_amount_usd,
    lt.debit_amount_usd, lt.credit_amount_usd, lt.balance_usd,
    lt.card_balance_usd, lt.jp_balance_usd, lt.jp_principal_balance_usd, lt.jp_interest_balance_usd,
    lt.invoiced_usd, lt.forex_adjustment, lt.is_in_repayment, lt.repayment_dt,
    lt.fee_amount, lt.fee_amount_usd, lt.status,
    lt.card_disbursement, lt.card_payment, lt.card_cashback, lt.card_late_payment_penalty,
    lt.card_loan_allocation, lt.card_fx_adjustment, lt.card_adjustment,
    lt.jp_disbursement, lt.jp_fee, lt.jp_payment, lt.jp_cashback, lt.jp_late_payment_penalty,
    lt.jp_loan_allocation, lt.jp_fx_adjustment, lt.jp_adjustment,
    lt.prior_currency, lt.prior_balance, lt.prior_spot_rate, lt.currency_switch_adjustment_usd,
    rc.onboarding_date,
    md.max_dpd,
    jursf.jur_loss_rate_grade as uw_score,
    rc.name, rc.ein,
    cl.credit_limit_usd,
    lt.state_name, lt.city_name,
    rc.naics_industry_id,
    case when td.company_type = 'Startup' then 1 else 0 end as is_startup,
    cr.total_collateral_amount_usd,
    igr.coverageamountusd
from capital_markets_dm.loc_tape lt
left join raw_cos rc on rc.company_id = lt.company_id
left join credit_limits cl on cl.company_id = lt.company_id
left join collateral_totals cr on cr.companyid = lt.company_id
left join (
    select companyid, coverageamountusd,
        row_number() over (partition by companyid order by updatedat desc) as rn
    from dms_mysql_jeeves_raw.insurance_guarantee_records
) igr on igr.companyid = lt.company_id and igr.rn = 1
left join max_dq md on md.company_id = lt.company_id
left join entity_balances eb on eb.company_id = lt.company_id and eb.dt = lt.dt
left join sofom_transfer st on st.id = lt.company_id
left join (
    select company_id, jur_loss_rate_grade,
        row_number() over (partition by company_id order by updated_at desc) as rn
    from analytics_sandbox.jeeves_unified_risk_scoring_final
) jursf on jursf.company_id = lt.company_id and jursf.rn = 1
left join (
    select company_id, company_type,
        row_number() over (partition by company_id order by updated_at desc) as rn
    from dms_mysql_underwriting_raw.taktile_data
) td on td.company_id = lt.company_id and td.rn = 1
where lt.dt = '{date}'
  and is_in_repayment is False
  and charge_off_flag is False
"""

RF_SQL = Path('/mnt/skills/custom/jeeves-borrowing-base/sql/loc_acct_rollforward.sql').read_text()

con = new_con()
try:
    print(f"  Querying tape for {date_beg} (BOP)...")
    df_beg = pd.read_sql_query(TAPE_SQL.format(date=date_beg.isoformat()), con)
    print(f"    {len(df_beg)} rows")

    print(f"  Querying tape for {date_end} (EOP)...")
    df_end = pd.read_sql_query(TAPE_SQL.format(date=date_end.isoformat()), con)
    print(f"    {len(df_end)} rows")

    print(f"  Querying rollforward {date_beg} -> {date_end}...")
    rollforward = pd.read_sql_query(RF_SQL.format(date_beg.isoformat(), date_end.isoformat()), con)
    print(f"    {len(rollforward)} rows")
finally:
    con.close()

print("  Calculating eligibility (BOP)...")
df_beg = calculate_eligibility_fields(df_beg)
print("  Calculating eligibility (EOP)...")
df_end = calculate_eligibility_fields(df_end)
print("  Generating eligibility summary...")
elig_summary = calculate_eligibility_summary(df_end, balance_col='balance_usd')

TAPE_COL_ORDER = [
    'dt','company_id','loan_id','country_code','product','currency','delinquent_dt',
    'days_past_due','dq_bucket','dq_bucket_daily','dq_bucket_monthly','charge_off_exclusion',
    'charge_off_dt','charge_off_flag','disbursement_amount','payment_amount','cashback_amount',
    'late_payment_penalty_amount','jeeves_pay_disbursement_amount','jeeves_pay_fee_amount',
    'loan_allocation_amount','fx_adjustment_amount','adjustment_amount','debit_amount',
    'credit_amount','balance','jvs_remaining','sofom_balance','sofom_balance_usd',
    'transfer_flag','card_balance','jp_balance','jp_principal_balance','jp_interest_balance',
    'overpay_balance','invoiced','spot_rate','disbursement_amount_usd','payment_amount_usd',
    'cashback_amount_usd','late_payment_penalty_amount_usd','jeeves_pay_disbursement_amount_usd',
    'jeeves_pay_fee_amount_usd','loan_allocation_amount_usd','fx_adjustment_amount_usd',
    'adjustment_amount_usd','debit_amount_usd','credit_amount_usd','balance_usd',
    'card_balance_usd','jp_balance_usd','jp_principal_balance_usd','jp_interest_balance_usd',
    'invoiced_usd','forex_adjustment','is_in_repayment','repayment_dt',
    'fee_amount','fee_amount_usd','status',
    'card_disbursement','card_payment','card_cashback','card_late_payment_penalty',
    'card_loan_allocation','card_fx_adjustment','card_adjustment','jp_disbursement','jp_fee',
    'jp_payment','jp_cashback','jp_late_payment_penalty','jp_loan_allocation','jp_fx_adjustment',
    'jp_adjustment','prior_currency','prior_balance','prior_spot_rate',
    'currency_switch_adjustment_usd','onboarding_date','max_dpd','uw_score','name','ein',
    'credit_limit_usd','elig','state_name','city_name','naics_industry_id','is_startup',
    'total_collateral_amount_usd','coverageamountusd',
    'elig_juris','elig_a','elig_b','elig_c','elig_d','elig_e','elig_f','elig_g','elig_h',
    'elig_i','elig_j','elig_k','elig_l','elig_m','elig_n','elig_o','elig_p','elig_q','elig_r',
    'elig_s','elig_t','elig_u','elig_v','elig_w','elig_x','elig_y','elig_z','elig_aa','elig_bb',
    'elig_cc','elig_dd','elig_ee','elig_ff','elig_gg','elig_hh','elig_ii','elig_jj','elig_kk',
    'elig_ll','elig_mm','elig_nn','elig_oo','elig_pp','elig_qq',
]

def reorder_tape(df):
    for col in TAPE_COL_ORDER:
        if col not in df.columns:
            df[col] = None
    return df[TAPE_COL_ORDER]

df_beg = reorder_tape(df_beg)
df_end = reorder_tape(df_end)

output_dir = os.environ.get('OUTPUTS_PATH', '/mnt/user-data/outputs')
os.makedirs(output_dir, exist_ok=True)
filename = "Borrowing Base - US - 20260503.xlsx"
output_path = os.path.join(output_dir, filename)

print(f"  Writing {filename}...")
with pd.ExcelWriter(output_path, engine='xlsxwriter') as writer:
    df_beg.to_excel(writer, sheet_name='tape_start', index=False)
    df_end.to_excel(writer, sheet_name='tape_end', index=False)
    rollforward.to_excel(writer, sheet_name='rollforward', index=False)
    elig_summary.to_excel(writer, sheet_name='eligibility_summary', index=False)

print(f"\nDone! Output: {output_path}")
print(f"Total EOP balance: ${df_end['balance_usd'].sum():,.2f}")
eligible = df_end[df_end['elig'] == 1]
print(f"Eligible balance:  ${eligible['balance_usd'].sum():,.2f}")
print(f"Eligible accounts: {len(eligible)} of {len(df_end)}")
