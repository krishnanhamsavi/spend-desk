-- Fails on any warehouse hour whose size could not be resolved to a credit rate.
select warehouse_name, hour_start
from core.fct_warehouse_hourly
where credits_per_hour is null
