-- Fails on any day where RAW hourly metering, MART.DAILY_SPEND, and MART.DAILY_TEAM_SPEND
-- disagree by more than 0.001 credits.
with raw_day as (
    select cast(start_time as date) as usage_date, sum(credits_used) as credits
    from raw.warehouse_metering_history
    group by cast(start_time as date)
),
spend_day as (
    select usage_date, sum(credits) as credits from mart.daily_spend group by usage_date
),
team_day as (
    select usage_date, sum(credits) as credits from mart.daily_team_spend group by usage_date
)
select r.usage_date, r.credits as raw_credits, s.credits as spend_credits, t.credits as team_credits
from raw_day as r
left join spend_day as s on s.usage_date = r.usage_date
left join team_day as t on t.usage_date = r.usage_date
where abs(r.credits - coalesce(s.credits, -1)) > 0.001
    or abs(r.credits - coalesce(t.credits, -1)) > 0.001
