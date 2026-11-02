-- Every assumption the dollar figures depend on, so the app can show it next to any number.
create or replace table mart.assumptions as
select name, value, note
from (values
    ('dollars_per_credit', cast({{ dollars_per_credit }} as double),
     'ASSUMPTION: list price per credit. Real rates vary by edition, region, and contract.'),
    ('query_sample_rate', cast({{ query_sample_rate }} as double),
     'QUERY_HISTORY is a uniform sample. Query counts are scaled by 1 / rate.')
) as a (name, value, note);
