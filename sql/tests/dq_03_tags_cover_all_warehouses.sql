-- Fails on any metered warehouse without a COST_CENTER tag that maps to a known team.
select warehouse_name, cost_center
from core.dim_warehouse
where team is null
