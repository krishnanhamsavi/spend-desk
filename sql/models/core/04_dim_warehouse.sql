-- One row per metered warehouse with its owning team and current size.
-- Left joins on purpose: a warehouse without a COST_CENTER tag keeps a null team and fails the coverage test.
create or replace table core.dim_warehouse as
with wh as (
    select
        warehouse_id,
        warehouse_name,
        min(start_time) as first_metered_hour,
        max(start_time) as last_metered_hour
    from raw.warehouse_metering_history
    group by warehouse_id, warehouse_name
)
select
    wh.warehouse_id,
    wh.warehouse_name,
    tr.tag_value as cost_center,
    t.team,
    cur.warehouse_size as current_size,
    s.credits_per_hour as current_credits_per_hour,
    wh.first_metered_hour,
    wh.last_metered_hour
from wh
left join raw.tag_references as tr
    on tr.object_name = wh.warehouse_name
    and tr.domain = 'WAREHOUSE'
    and tr.tag_name = 'COST_CENTER'
    and tr.object_deleted is null
left join core.dim_team as t on t.cost_center = tr.tag_value
left join core.warehouse_size_history as cur
    on cur.warehouse_name = wh.warehouse_name
    and cur.valid_to = cast('2100-01-01' as timestamp)
left join core.dim_warehouse_size as s on s.warehouse_size = cur.warehouse_size;
