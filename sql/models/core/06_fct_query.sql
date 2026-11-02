-- Grain: one sampled query that ran on a warehouse.
-- attributed_compute_credits spreads each warehouse hour's CREDITS_ATTRIBUTED_COMPUTE_QUERIES across the
-- sampled queries that started in that hour, by execution time. The sample is uniform, so pattern
-- shares are unbiased and the allocated credits already sum to the full hourly total.
create or replace table core.fct_query as
with q as (
    select
        query_id,
        start_time,
        cast(start_time as date) as usage_date,
        date_trunc('hour', start_time) as hour_start,
        warehouse_name,
        warehouse_size,
        user_name,
        role_name,
        query_type,
        query_tag,
        execution_status,
        query_parameterized_hash,
        query_text,
        total_elapsed_time / 1000.0 as elapsed_s,
        execution_time / 1000.0 as execution_s,
        compilation_time / 1000.0 as compilation_s,
        queued_overload_time / 1000.0 as queued_overload_s,
        queued_provisioning_time / 1000.0 as queued_provisioning_s,
        bytes_scanned,
        partitions_scanned,
        partitions_total,
        cast(partitions_scanned as double) / nullif(partitions_total, 0) as scan_ratio,
        bytes_spilled_to_local_storage,
        bytes_spilled_to_remote_storage,
        credits_used_cloud_services
    from raw.query_history
    where warehouse_name is not null
)
select
    q.*,
    w.team,
    h.credits_attributed_compute_queries * q.execution_s
        / nullif(sum(q.execution_s) over (partition by q.warehouse_name, q.hour_start), 0)
        as attributed_compute_credits,
    1.0 / {{ query_sample_rate }} as sample_weight
from q
left join core.dim_warehouse as w on w.warehouse_name = q.warehouse_name
left join core.fct_warehouse_hourly as h
    on h.warehouse_name = q.warehouse_name
    and h.hour_start = q.hour_start;
