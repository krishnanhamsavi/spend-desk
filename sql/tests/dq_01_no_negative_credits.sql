-- Fails on any warehouse hour with a negative credit value.
select warehouse_name, hour_start
from core.fct_warehouse_hourly
where credits_used < 0
    or credits_used_compute < 0
    or credits_used_cloud_services < 0
    or credits_attributed_compute_queries < 0
    or idle_credits < 0
    or idle_excl_min_billing_credits < 0
