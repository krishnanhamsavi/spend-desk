import duckdb
import pytest
import yaml

from spend_desk.config import ROOT
from spend_desk.models import Target, build, render, run_dq

REQUIRED_KEYS = {"label", "unit", "grain", "source", "formula", "caveats"}


def test_all_data_quality_checks_pass(db):
    results = run_dq(Target("duckdb", db))
    assert len(results) >= 4
    assert results == {name: 0 for name in results}


def test_data_quality_checks_catch_planted_defects(db):
    """Copy one day of RAW into memory, break it on purpose, and expect the right checks to fail."""
    mem = duckdb.connect()
    mem.execute("create schema raw")
    day = "date '2026-09-01'"
    for table, ts in [
        ("warehouse_metering_history", "start_time"),
        ("query_history", "start_time"),
        ("warehouse_events_history", '"TIMESTAMP"'),
    ]:
        df = db.execute(f"select * from raw.{table} where cast({ts} as date) = {day}").df()
        mem.register("df", df)
        mem.execute(f"create table raw.{table} as select * from df")
        mem.unregister("df")
    tags = db.execute("select * from raw.tag_references where object_name <> 'GTM_SYNC_WH'").df()
    mem.register("tags", tags)
    mem.execute("create table raw.tag_references as select * from tags")
    # A duplicated hour and a negative credit row.
    mem.execute("""insert into raw.warehouse_metering_history
                   select * from raw.warehouse_metering_history limit 1""")
    mem.execute("""update raw.warehouse_metering_history
                   set credits_used_cloud_services = -0.5,
                       credits_used = credits_used_compute - 0.5
                   where warehouse_name = 'PA_BI_WH' and hour(start_time) = 12""")

    build(Target("duckdb", mem), verbose=False)
    failed = {k for k, v in run_dq(Target("duckdb", mem)).items() if v > 0}
    assert {
        "dq_01_no_negative_credits",
        "dq_03_tags_cover_all_warehouses",
        "dq_04_no_duplicate_hours",
    } <= failed


def test_metric_definitions_are_complete(db):
    spec = yaml.safe_load((ROOT / "metrics.yaml").read_text(encoding="utf-8"))
    tables = {
        f"{s}.{t}".lower()
        for s, t in db.execute(
            "select table_schema, table_name from information_schema.tables"
        ).fetchall()
    }
    for name, m in spec["metrics"].items():
        assert REQUIRED_KEYS <= set(m), name
        for src in m["source"].split(","):
            assert src.strip().lower() in tables, f"{name} names missing model {src}"


def test_team_grid_is_complete(db):
    days, teams, rows = db.execute("""select count(distinct usage_date), count(distinct team),
                                      count(*) from mart.daily_team_spend""").fetchone()
    assert teams == 6 and rows == days * teams


def test_resize_history_follows_events(db):
    sizes = dict(
        db.execute("""
        select cast(valid_from as date)::varchar, warehouse_size
        from core.warehouse_size_history where warehouse_name = 'DP_TRANSFORM_WH'""").fetchall()
    )
    assert sizes["2025-12-01"] == "X-Large" and sizes["2025-12-29"] == "Medium"


def test_cloud_services_ratio_surfaces_planted_spike(db):
    ratio = dict(
        db.execute("""
        select usage_date between '2026-01-20' and '2026-01-27' as in_window,
               sum(cloud_services_credits) / sum(compute_credits)
        from mart.daily_warehouse_efficiency
        where warehouse_name = 'PA_BI_WH' and usage_date between '2025-12-20' and '2026-01-27'
        group by 1""").fetchall()
    )
    assert ratio[True] > 3 * ratio[False]


def test_idle_share_surfaces_planted_autosuspend_waste(db):
    share = dict(
        db.execute("""
        select warehouse_name, sum(idle_excl_min_billing_credits) / sum(compute_credits)
        from mart.daily_warehouse_efficiency where usage_date > date '2026-08-16'
        group by 1""").fetchall()
    )
    assert share["GTM_REPORTING_WH"] > 0.9 and share["PA_ADHOC_WH"] > 0.8


def test_poor_pruning_pattern_is_costly_and_unpruned(db):
    row = db.execute("""
        select q.avg_scan_ratio,
               rank() over (order by q.attributed_dollars desc) as cost_rank
        from mart.query_pattern_cost_monthly q
        join raw.ground_truth g on g.query_parameterized_hash = q.query_parameterized_hash
        where g.truth_id = 'W07' and q.usage_month = date '2026-08-01'""").fetchone()
    top = db.execute("""
        select query_parameterized_hash from mart.query_pattern_cost_monthly
        where usage_month = date '2026-08-01' and warehouse_name = 'PA_BI_WH'
        order by attributed_dollars desc limit 1""").fetchone()[0]
    w07 = db.execute(
        "select query_parameterized_hash from raw.ground_truth where truth_id = 'W07'"
    ).fetchone()[0]
    assert row[0] > 0.9 and top == w07


def test_render_rejects_unknown_variables():
    with pytest.raises(KeyError):
        render("select {{ nope }}", {"dollars_per_credit": "3.0"})
