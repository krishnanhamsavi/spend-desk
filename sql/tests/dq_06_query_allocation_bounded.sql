-- Fails where credits allocated to sampled queries exceed the hour's attributed compute.
select q.warehouse_name, q.hour_start
from core.fct_query as q
join core.fct_warehouse_hourly as h
    on h.warehouse_name = q.warehouse_name and h.hour_start = q.hour_start
group by q.warehouse_name, q.hour_start
having sum(q.attributed_compute_credits) > max(h.credits_attributed_compute_queries) + 0.000001
