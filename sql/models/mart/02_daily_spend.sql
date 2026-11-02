-- Grain: day, team, warehouse, service type. The governed source for credits and dollars.
-- Dollars use the assumed price per credit. Cloud services are gross, before the daily 10 percent adjustment.
create or replace table mart.daily_spend as
select
    usage_date,
    team,
    warehouse_name,
    'COMPUTE' as service_type,
    sum(credits_used_compute) as credits,
    sum(credits_used_compute) * {{ dollars_per_credit }} as dollars
from core.fct_warehouse_hourly
group by usage_date, team, warehouse_name
union all
select
    usage_date,
    team,
    warehouse_name,
    'CLOUD_SERVICES' as service_type,
    sum(credits_used_cloud_services) as credits,
    sum(credits_used_cloud_services) * {{ dollars_per_credit }} as dollars
from core.fct_warehouse_hourly
group by usage_date, team, warehouse_name;
