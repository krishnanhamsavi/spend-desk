-- Grain: day, team. Complete grid, so a team with no usage on a day gets zero rather than a missing row.
-- This is the series the forecast and anomaly detector read.
create or replace table mart.daily_team_spend as
with days as (
    select distinct usage_date from core.fct_warehouse_hourly
),
spend as (
    select
        usage_date,
        team,
        sum(credits_used) as credits,
        sum(credits_used_compute) as compute_credits,
        sum(credits_used_cloud_services) as cloud_services_credits,
        sum(idle_credits) as idle_credits
    from core.fct_warehouse_hourly
    group by usage_date, team
)
select
    d.usage_date,
    t.team,
    coalesce(s.credits, 0) as credits,
    coalesce(s.compute_credits, 0) as compute_credits,
    coalesce(s.cloud_services_credits, 0) as cloud_services_credits,
    coalesce(s.idle_credits, 0) as idle_credits,
    coalesce(s.credits, 0) * {{ dollars_per_credit }} as dollars
from days as d
cross join core.dim_team as t
left join spend as s on s.usage_date = d.usage_date and s.team = t.team;
