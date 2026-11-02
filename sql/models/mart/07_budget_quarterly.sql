-- Grain: team, fiscal quarter. The FY plan as Finance would have set it on plan_date.
-- Budget = trailing daily run rate at plan_date x (1 + planned quarterly growth) ^ quarter index x days.
-- ASSUMPTION: growth rates and plan_date live in config.yaml. The plan uses no data after plan_date.
create or replace table mart.budget_quarterly as
with base as (
    select team, avg(credits) as base_daily_credits
    from mart.daily_team_spend
    where usage_date between cast('{{ base_start }}' as date) and cast('{{ plan_date }}' as date)
    group by team
),
quarters as (
    select fiscal_quarter, quarter_start, quarter_end, quarter_index, quarter_days
    from (values {{ fiscal_quarters }})
        as q (fiscal_quarter, quarter_start, quarter_end, quarter_index, quarter_days)
),
growth as (
    select team, quarterly_growth
    from (values {{ budget_growth }}) as g (team, quarterly_growth)
),
team_budget as (
    select
        b.team,
        q.fiscal_quarter,
        cast(q.quarter_start as date) as quarter_start,
        cast(q.quarter_end as date) as quarter_end,
        q.quarter_days,
        b.base_daily_credits,
        g.quarterly_growth,
        b.base_daily_credits * power(1 + g.quarterly_growth, q.quarter_index) * q.quarter_days
            as budget_credits
    from base as b
    join growth as g on g.team = b.team
    cross join quarters as q
)
select
    team,
    fiscal_quarter,
    quarter_start,
    quarter_end,
    quarter_days,
    base_daily_credits,
    quarterly_growth,
    budget_credits,
    budget_credits * {{ dollars_per_credit }} as budget_dollars
from team_budget
union all
select
    'All teams' as team,
    fiscal_quarter,
    min(quarter_start),
    min(quarter_end),
    min(quarter_days),
    sum(base_daily_credits),
    null,
    sum(budget_credits),
    sum(budget_credits) * {{ dollars_per_credit }}
from team_budget
group by fiscal_quarter;
