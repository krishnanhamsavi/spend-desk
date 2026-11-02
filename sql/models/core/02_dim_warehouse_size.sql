-- Snowflake standard warehouse sizes and their credits per hour. These are published rates, not assumptions.
create or replace table core.dim_warehouse_size as
select warehouse_size, sql_size, size_rank, credits_per_hour
from (values
    ('X-Small', 'XSMALL', 1, 1),
    ('Small', 'SMALL', 2, 2),
    ('Medium', 'MEDIUM', 3, 4),
    ('Large', 'LARGE', 4, 8),
    ('X-Large', 'XLARGE', 5, 16),
    ('2X-Large', 'XXLARGE', 6, 32)
) as s (warehouse_size, sql_size, size_rank, credits_per_hour);
