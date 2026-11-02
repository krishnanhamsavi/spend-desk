-- One row per team. COST_CENTER tag values on warehouses map to display names from config.yaml.
create or replace table core.dim_team as
select cost_center, team
from (values {{ team_values }}) as t (cost_center, team);
