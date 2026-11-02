-- Grain: team, current fiscal quarter. The headline QBR number.
-- Projected quarter end = actual quarter to date + forecast for the rest of the quarter.
-- The range adds the daily 80 percent bounds, which assumes errors move together. That makes it
-- wider than a true 80 percent interval for the quarter total, so treat it as a conservative band.
-- Runs after the forecast step writes mart.forecast_daily.
create or replace table mart.forecast_vs_budget as
with budget as (
    select team, fiscal_quarter, quarter_start, quarter_end, quarter_days, budget_credits
    from mart.budget_quarterly
    where fiscal_quarter = '{{ current_quarter }}'
),
team_actual as (
    select team, sum(credits) as qtd_credits
    from mart.daily_team_spend
    where usage_date between cast('{{ quarter_start }}' as date) and cast('{{ as_of }}' as date)
    group by team
),
actual as (
    select team, qtd_credits from team_actual
    union all
    select 'All teams', sum(qtd_credits) from team_actual
),
fc as (
    select
        team,
        max(model) as model,
        sum(forecast_credits) as remaining_credits,
        sum(lower_credits) as remaining_low_credits,
        sum(upper_credits) as remaining_high_credits
    from mart.forecast_daily
    where usage_date > cast('{{ as_of }}' as date)
        and usage_date <= cast('{{ quarter_end }}' as date)
    group by team
)
select
    b.team,
    b.fiscal_quarter,
    cast('{{ as_of }}' as date) as as_of,
    b.quarter_start,
    b.quarter_end,
    b.quarter_days,
    fc.model,
    b.budget_credits,
    a.qtd_credits,
    fc.remaining_credits,
    a.qtd_credits + fc.remaining_credits as projected_credits,
    a.qtd_credits + fc.remaining_low_credits as projected_low_credits,
    a.qtd_credits + fc.remaining_high_credits as projected_high_credits,
    a.qtd_credits + fc.remaining_credits - b.budget_credits as variance_credits,
    (a.qtd_credits + fc.remaining_credits - b.budget_credits) / b.budget_credits as variance_pct,
    b.budget_credits * {{ dollars_per_credit }} as budget_dollars,
    a.qtd_credits * {{ dollars_per_credit }} as qtd_dollars,
    (a.qtd_credits + fc.remaining_credits) * {{ dollars_per_credit }} as projected_dollars,
    (a.qtd_credits + fc.remaining_low_credits) * {{ dollars_per_credit }} as projected_low_dollars,
    (a.qtd_credits + fc.remaining_high_credits) * {{ dollars_per_credit }} as projected_high_dollars,
    (a.qtd_credits + fc.remaining_credits - b.budget_credits) * {{ dollars_per_credit }}
        as variance_dollars
from budget as b
join actual as a on a.team = b.team
join fc on fc.team = b.team;
