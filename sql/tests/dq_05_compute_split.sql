-- Fails where total credits do not equal compute plus cloud services.
select warehouse_name, hour_start
from core.fct_warehouse_hourly
where abs(credits_used - credits_used_compute - credits_used_cloud_services) > 0.000001
