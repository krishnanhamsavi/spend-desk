-- Fails on any warehouse with more than one metering row for the same hour.
select warehouse_name, hour_start, count(*) as n
from core.fct_warehouse_hourly
group by warehouse_name, hour_start
having count(*) > 1
