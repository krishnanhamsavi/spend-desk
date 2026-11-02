-- Size of each warehouse over time. Starting size comes from its earliest query, then every RESIZE event.
create or replace table core.warehouse_size_history as
with first_seen as (
    select warehouse_name, min_by(warehouse_size, start_time) as warehouse_size
    from raw.query_history
    where warehouse_name is not null
    group by warehouse_name
),
changes as (
    select warehouse_name, cast('1900-01-01' as timestamp) as valid_from, warehouse_size
    from first_seen
    union all
    select warehouse_name, "TIMESTAMP" as valid_from, size as warehouse_size
    from raw.warehouse_events_history
    where event_name = 'RESIZE_WAREHOUSE'
)
select
    warehouse_name,
    valid_from,
    coalesce(
        lead(valid_from) over (partition by warehouse_name order by valid_from),
        cast('2100-01-01' as timestamp)
    ) as valid_to,
    warehouse_size
from changes;
