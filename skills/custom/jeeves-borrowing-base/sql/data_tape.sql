with
collateral_totals as (
    select
        companyid
        , sum(collateralamountusd) as total_collateral_amount_usd
    from
        dms_mysql_jeeves_raw.collateral_records
    group by
        1
),
max_dq as (
    select
        company_id
        , max(days_past_due) as max_dpd
    from
        capital_markets_dm.loc_tape lt
    where
        1=1
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
        dms_mysql_jeeves_raw.company_settings cs
        on cs.companyId = c.id
    where
        1=1
        and cs.settingKey = 'SOFOM_JPMORGAN_ENABLED'
        --  and transfer_flag = 'on'
),
entity_balances_base as (
    select 
        dt
        , company_id
        , sum(case when assignment_dt::date = dt then balance end) over (partition by company_id) as jvs_transfer_balance
        , assignment_dt
        , sum(case when t.assignment_dt::date < lt.dt then credit_amount end) 
            over (partition by company_id order by dt rows unbounded preceding) as cumulative_credits
    from 
        capital_markets_dm.loc_tape lt
    join 
        sofom_transfer t 
        on t.id = lt.company_id 
        and t.assignment_dt::date <= lt.dt
)
, entity_balances as (
    select
        dt
        , company_id
        , jvs_transfer_balance
        , assignment_dt
        , cumulative_credits
        , greatest(jvs_transfer_balance + coalesce(cumulative_credits, 0), 0) as jvs_remaining
    from
        entity_balances_base
)
select
    lt.dt
    , lt.company_id
    , lt.loan_id
    , lt.country_code
    , lt.product
    , lt.currency
    , lt.delinquent_dt
    , lt.days_past_due
    , lt.dq_bucket
    , lt.dq_bucket_daily
    , lt.dq_bucket_monthly
    , lt.charge_off_exclusion
    , lt.charge_off_dt
    , lt.charge_off_flag
    , lt.disbursement_amount
    , lt.payment_amount
    , lt.cashback_amount
    , lt.late_payment_penalty_amount
    , lt.jeeves_pay_disbursement_amount
    , lt.jeeves_pay_fee_amount
    , lt.loan_allocation_amount
    , lt.fx_adjustment_amount
    , lt.adjustment_amount
    , lt.debit_amount
    , lt.credit_amount
    , lt.balance
    , case when sofom_transfer.transfer_flag = 'on' then eb.jvs_remaining end as jvs_remaining
    , case when sofom_transfer.transfer_flag = 'on' then lt.balance - eb.jvs_remaining end as sofom_balance
    , case when sofom_transfer.transfer_flag = 'on' then (lt.balance - eb.jvs_remaining) * lt.spot_rate end as sofom_balance_usd
    , sofom_transfer.transfer_flag
    , lt.card_balance
    , lt.jp_balance
    , lt.jp_principal_balance
    , lt.jp_interest_balance
    , lt.overpay_balance
    , lt.invoiced
    , lt.spot_rate
    , lt.disbursement_amount_usd
    , lt.payment_amount_usd
    , lt.cashback_amount_usd
    , lt.late_payment_penalty_amount_usd
    , lt.jeeves_pay_disbursement_amount_usd
    , lt.jeeves_pay_fee_amount_usd
    , lt.loan_allocation_amount_usd
    , lt.fx_adjustment_amount_usd
    , lt.adjustment_amount_usd
    , lt.debit_amount_usd
    , lt.credit_amount_usd
    , lt.balance_usd
    , lt.card_balance_usd
    , lt.jp_balance_usd
    , lt.jp_principal_balance_usd
    , lt.jp_interest_balance_usd
    , lt.invoiced_usd
    , lt.forex_adjustment
    , lt.is_in_repayment
    , lt.repayment_dt
    , lt.fee_amount
    , lt.fee_amount_usd
    , lt.status
    , lt.card_disbursement
    , lt.card_payment
    , lt.card_cashback
    , lt.card_late_payment_penalty
    , lt.card_loan_allocation
    , lt.card_fx_adjustment
    , lt.card_adjustment
    , lt.jp_disbursement
    , lt.jp_fee
    , lt.jp_payment
    , lt.jp_cashback
    , lt.jp_late_payment_penalty
    , lt.jp_loan_allocation
    , lt.jp_fx_adjustment
    , lt.jp_adjustment
    , lt.prior_currency
    , lt.prior_balance
    , lt.prior_spot_rate
    , lt.currency_switch_adjustment_usd
    , c.credit_limit_approved_date as onboarding_date
    , md.max_dpd
    , jursf.jur_loss_rate_grade as uw_score
    , c.name
    , c.ein
    , c.credit_limit_usd
    , c.state_name
    , c.city_name 
    , coalesce(c.naics_industry_id, 9999) as naics_industry_id
    , case when td.company_type = 'Startup' then 1 else 0 end as is_startup
    , cr.total_collateral_amount_usd
    , igr.coverageamountusd 
from
    capital_markets_dm.loc_tape lt
left join
    master_customer_dm.companies_dm c
    on c.company_id = lt.company_id
left join
    collateral_totals cr
    on cr.companyid = lt.company_id
left join 
	(
		select
			companyid
			, coverageamountusd
			, row_number() over (partition by companyid order by updatedat desc) as rn
		from
			dms_mysql_jeeves_raw.insurance_guarantee_records
	) igr 
	on igr.companyid = lt.company_id 
	and igr.rn = 1 
left join
    max_dq md
    on md.company_id = lt.company_id
left join
    entity_balances eb
    on eb.company_id = lt.company_id
    and eb.dt = lt.dt
left join 
	sofom_transfer 
	on sofom_transfer.id = lt.company_id
left join
    (
        select
            company_id
            , jur_loss_rate_grade
            , row_number() over (partition by company_id order by updated_at desc) as rn
        from
            analytics_sandbox.jeeves_unified_risk_scoring_final
    ) jursf
    on jursf.company_id = lt.company_id
    and jursf.rn = 1
left join
    (
        select
            company_id
            , company_type
            , row_number() over (partition by company_id order by updated_at desc) as rn
        from
            dms_mysql_underwriting_raw.taktile_data
    ) td
    on td.company_id = lt.company_id
    and td.rn = 1
where
    1=1
    and lt.dt = '{}'
    and is_in_repayment is False
    and charge_off_flag is False