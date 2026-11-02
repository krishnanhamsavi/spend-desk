-- Grain: day, account. Snowflake bills cloud services only above 10 percent of that day's compute.
create or replace table mart.cloud_services_billing_daily as
select
    usage_date,
    sum(credits_used_compute) as compute_credits,
    sum(credits_used_cloud_services) as cloud_services_credits,
    0.10 * sum(credits_used_compute) as cloud_services_allowance,
    greatest(sum(credits_used_cloud_services) - 0.10 * sum(credits_used_compute), 0)
        as cloud_services_billable_credits,
    greatest(sum(credits_used_cloud_services) - 0.10 * sum(credits_used_compute), 0)
        * {{ dollars_per_credit }} as cloud_services_billable_dollars
from core.fct_warehouse_hourly
group by usage_date;
