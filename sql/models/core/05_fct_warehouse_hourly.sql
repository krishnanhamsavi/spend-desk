-- Grain: one warehouse hour with metered credits.
-- idle_credits: compute billed while no query was executing (CREDITS_USED_COMPUTE minus CREDITS_ATTRIBUTED_COMPUTE_QUERIES).
-- min_billing_floor_credits: the part of idle that Snowflake bills no matter what, 60 seconds per resume.
-- Lowering AUTO_SUSPEND cannot recover the floor. How much of the rest it recovers depends on query gaps (estimated by the recommender).
create or replace table core.fct_warehouse_hourly as
with resumes as (
    select
        warehouse_name,
        date_trunc('hour', "TIMESTAMP") as hour_start,
        count(*) as resumes
    from raw.warehouse_events_history
    where event_name = 'RESUME_WAREHOUSE'
    group by warehouse_name, date_trunc('hour', "TIMESTAMP")
),
base as (
    select
        m.start_time as hour_start,
        cast(m.start_time as date) as usage_date,
        m.warehouse_id,
        m.warehouse_name,
        w.team,
        h.warehouse_size,
        s.credits_per_hour,
        m.credits_used,
        m.credits_used_compute,
        m.credits_used_cloud_services,
        coalesce(m.credits_attributed_compute_queries, 0) as credits_attributed_compute_queries,
        greatest(m.credits_used_compute - coalesce(m.credits_attributed_compute_queries, 0), 0)
            as idle_credits,
        coalesce(r.resumes, 0) as resumes
    from raw.warehouse_metering_history as m
    left join core.dim_warehouse as w on w.warehouse_name = m.warehouse_name
    left join core.warehouse_size_history as h
        on h.warehouse_name = m.warehouse_name
        and m.start_time >= h.valid_from
        and m.start_time < h.valid_to
    left join core.dim_warehouse_size as s on s.warehouse_size = h.warehouse_size
    left join resumes as r
        on r.warehouse_name = m.warehouse_name
        and r.hour_start = m.start_time
)
select
    base.*,
    least(resumes * 60 / 3600.0 * credits_per_hour, idle_credits) as min_billing_floor_credits,
    idle_credits - least(resumes * 60 / 3600.0 * credits_per_hour, idle_credits)
        as idle_excl_min_billing_credits
from base;
