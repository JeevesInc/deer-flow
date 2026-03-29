with
dates as (
  select
    '{}'::date as start_date
    , '{}'::date as end_date
    )
, onboarding_date as (
    select
        company_id
        , min(dt) as onboarding_date
        from
            capital_markets_dm.loc_tape lt
        where
            1=1
        group by 1
)
, beginning_of_period as (
select
  company_id
  , country_code
  , balance_usd
  , days_past_due
  from
    capital_markets_dm.loc_tape l
    join dates d on l.dt = d.start_date
  where
    1=1
    and charge_off_flag is False
    and is_in_repayment is False
)
, end_of_period as (
select
  company_id
  , country_code
  , balance_usd
  , days_past_due
  from
    capital_markets_dm.loc_tape l
    join dates d on l.dt = d.end_date
  where
    1=1
    and is_in_repayment is False
    and charge_off_flag is False
)
, transactions as (
select
  l.company_id
  , sum(disbursement_amount * spot_rate) as card_disbursement_amount_usd
  , sum(jeeves_pay_disbursement_amount * spot_rate) as jeeves_pay_disbursement_amount_usd
  , sum(payment_amount * spot_rate) as payment_amount_usd
  , sum(cashback_amount * spot_rate) as cashback_amount_usd
  , sum(late_payment_penalty_amount * spot_rate) as late_payment_penalty_amount_usd
  , sum(jeeves_pay_fee_amount * spot_rate) as jeeves_pay_fee_amount_usd
  , sum(loan_allocation_amount * spot_rate) as loan_allocation_amount_usd
  , sum(fx_adjustment_amount * spot_rate) as fx_adjustment_amount_usd
  , sum(case when charge_off_dt is null then forex_adjustment else 0 end) as forex_adjustment_usd
  , sum(adjustment_amount * spot_rate) as adjustment_amount_usd
  , sum(currency_switch_adjustment_usd) as usd_delta_currency_switch
  from
    capital_markets_dm.loc_tape l
  join dates d on l.dt between d.start_date + 1 and d.end_date
  where
    1=1
    and (charge_off_flag is false or charge_off_dt = l.dt)
    and is_in_repayment is False
  group by 1
)
, co_date as (
  select
    company_id
    , min(dt) - 1
    as co_dt
    from
      capital_markets_dm.loc_tape
      where
      1=1
      and charge_off_flag is true
      and is_in_repayment is false group by 1
)
, charge_offs as (
  select
    l.company_id
    , sum(balance_usd) as charge_off_usd
  from
    capital_markets_dm.loc_tape l join dates d on l.dt between d.start_date + 1 and d.end_date
  join co_date c on co_dt = l.dt and c.company_id = l.company_id
  group by 1
)
, repayment_dt as (
select company_id, min(dt) - 1 as repayment_dt from capital_markets_dm.loc_tape where is_in_repayment is true group by 1
)
, repayments as (
select
  r.company_id
, sum(balance_usd) as repayment_usd
from capital_markets_dm.loc_tape l
join repayment_dt r on r.company_id = l.company_id and r.repayment_dt = l.dt
join dates d on l.dt between d.start_date + 1 and d.end_date
group by 1
)
, base as (
select
  coalesce(t.company_id, b.company_id, e.company_id) as company_id
  , coalesce(b.country_code, e.country_code) as country_code
  , coalesce(b.balance_usd, 0) as bop_balance_usd
  , coalesce(b.days_past_due, 0) as bop_days_past_due
  , coalesce(t.card_disbursement_amount_usd, 0) as card_disbursement_amount_usd
  , coalesce(t.jeeves_pay_disbursement_amount_usd, 0) as jeeves_pay_disbursement_amount_usd
  , coalesce(t.payment_amount_usd, 0) as payment_amount_usd
  , coalesce(t.cashback_amount_usd, 0) as cashback_amount_usd
  , coalesce(t.late_payment_penalty_amount_usd, 0) as late_payment_penalty_amount_usd
  , coalesce(t.jeeves_pay_fee_amount_usd, 0) as jeeves_pay_fee_amount_usd
  , coalesce(t.loan_allocation_amount_usd, 0) as loan_allocation_amount_usd
  , coalesce(t.fx_adjustment_amount_usd, 0) as fx_adjustment_amount_usd
  , coalesce(t.forex_adjustment_usd, 0) as forex_adjustment_usd
  , coalesce(t.adjustment_amount_usd, 0) as adjustment_amount_usd
  , coalesce(t.usd_delta_currency_switch,0) as usd_delta_currency_switch
  , coalesce(c.charge_off_usd, 0) as charge_off_usd
  , coalesce(r.repayment_usd,0) as repayment_usd
  , coalesce(e.balance_usd, 0) as eop_balance_usd
  , coalesce(e.days_past_due, 0) as eop_days_past_due
from transactions as t
left join beginning_of_period as b
  on t.company_id = b.company_id
left join end_of_period as e
  on t.company_id = e.company_id
left join charge_offs as c
  on t.company_id = c.company_id
left join repayments r on r.company_id = t.company_id
)
, roll_forward as (
select *
, eop_balance_usd - bop_balance_usd as change_in_balances
, card_disbursement_amount_usd
+ jeeves_pay_disbursement_amount_usd
  + payment_amount_usd
  + cashback_amount_usd
  + late_payment_penalty_amount_usd
  + jeeves_pay_fee_amount_usd
  + loan_allocation_amount_usd
  + fx_adjustment_amount_usd
  + forex_adjustment_usd
  + adjustment_amount_usd
  + usd_delta_currency_switch
  - charge_off_usd
  - repayment_usd as intra_period_transactions
, abs(change_in_balances - intra_period_transactions) as diff
from base
)
select
  *
  from
    roll_forward
  order by
    abs(diff) desc
