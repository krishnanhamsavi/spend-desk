-- Grain: day, warehouse. Efficiency signals the recommender and the anomaly drill down use.
create or replace table mart.daily_warehouse_efficiency as
with h as (
    select
        usage_date,
        warehouse_name,
        max(team) as team,
        max_by(warehouse_size, hour_start) as end_of_day_size,
        sum(credits_used) as credits_used,
        sum(credits_used_compute) as compute_credits,
        sum(credits_used_cloud_services) as cloud_services_credits,
        sum(credits_attributed_compute_queries) as attributed_credits,
        sum(idle_credits) as idle_credits,
        sum(min_billing_floor_credits) as min_billing_floor_credits,
        sum(idle_excl_min_billing_credits) as idle_excl_min_billing_credits,
        sum(resumes) as resumes,
        count(*) as metered_hours
    from core.fct_warehouse_hourly
    group by usage_date, warehouse_name
),
q as (
    select
        usage_date,
        warehouse_name,
        count(*) as sampled_queries,
        sum(queued_overload_s) as queued_overload_s,
        sum(elapsed_s) as elapsed_s,
        avg(case when bytes_spilled_to_remote_storage > 0 then 1.0 else 0.0 end)
            as remote_spill_query_share,
        avg(case when bytes_spilled_to_local_storage > 0 then 1.0 else 0.0 end)
            as local_spill_query_share,
        avg(bytes_spilled_to_remote_storage) / 1e9 as remote_spill_gb_per_query,
        avg(scan_ratio) as avg_scan_ratio,
        percentile_cont(0.95) within group (order by elapsed_s) as p95_elapsed_s
    from core.fct_query
    group by usage_date, warehouse_name
)
select
    h.*,
    h.credits_used * {{ dollars_per_credit }} as dollars,
    h.cloud_services_credits / nullif(h.compute_credits, 0) as cloud_services_ratio,
    h.idle_credits / nullif(h.compute_credits, 0) as idle_share,
    coalesce(q.sampled_queries, 0) as sampled_queries,
    coalesce(q.sampled_queries, 0) / {{ query_sample_rate }} as est_queries,
    q.queued_overload_s / nullif(q.elapsed_s, 0) as queued_time_share,
    q.remote_spill_query_share,
    q.local_spill_query_share,
    q.remote_spill_gb_per_query,
    q.avg_scan_ratio,
    q.p95_elapsed_s
from h
left join q on q.usage_date = h.usage_date and q.warehouse_name = h.warehouse_name;
