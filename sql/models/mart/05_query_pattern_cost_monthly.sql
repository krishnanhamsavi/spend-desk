-- Grain: month, query pattern (QUERY_PARAMETERIZED_HASH), warehouse.
-- attributed_credits is execution compute only. Idle time is a warehouse cost, not a pattern cost.
create or replace table mart.query_pattern_cost_monthly as
select
    date_trunc('month', usage_date) as usage_month,
    query_parameterized_hash,
    warehouse_name,
    max(team) as team,
    max(query_type) as query_type,
    min_by(query_text, start_time) as example_query_text,
    count(*) as sampled_queries,
    count(*) / {{ query_sample_rate }} as est_queries,
    sum(attributed_compute_credits) as attributed_credits,
    sum(attributed_compute_credits) * {{ dollars_per_credit }} as attributed_dollars,
    sum(attributed_compute_credits) / nullif(count(*) / {{ query_sample_rate }}, 0)
        as credits_per_query,
    sum(credits_used_cloud_services) / {{ query_sample_rate }} as est_cloud_services_credits,
    avg(scan_ratio) as avg_scan_ratio,
    avg(elapsed_s) as avg_elapsed_s,
    avg(case when execution_status = 'FAIL' then 1.0 else 0.0 end) as fail_share,
    count(distinct user_name) as distinct_users,
    mode(user_name) as top_user
from core.fct_query
group by date_trunc('month', usage_date), query_parameterized_hash, warehouse_name;
