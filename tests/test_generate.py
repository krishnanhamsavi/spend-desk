import numpy as np
import pandas as pd
import pytest
from scipy.stats import poisson

from spend_desk.config import load_config
from spend_desk.generate.documented_columns import DOCUMENTED
from spend_desk.generate.engine import make_calendar, poisson_inv, simulate
from spend_desk.generate.scenario import build_world

CFG = load_config()
SEED = CFG.raw["data"]["seed"]


@pytest.fixture(scope="module")
def world():
    cal = make_calendar(CFG.history_start, CFG.as_of, CFG.fiscal_year_start_month)
    whs, truth = build_world()
    return cal, {w.name: w for w in whs}, truth


def test_poisson_inverse_matches_scipy_below_30():
    rng = np.random.default_rng(1)
    u = rng.random(50_000)
    lam = rng.choice([0.0, 0.05, 1.0, 7.5, 29.0], 50_000)
    expected = np.where(lam > 0, poisson.ppf(u, np.maximum(lam, 1e-12)), 0)
    assert (poisson_inv(u, lam) == expected).all()


def test_simulation_is_reproducible(world):
    cal, whs, _ = world
    a = simulate(whs["PA_BI_WH"], cal, SEED)
    b = simulate(whs["PA_BI_WH"], cal, SEED)
    assert np.array_equal(a.credits_used, b.credits_used)


def test_counterfactual_only_changes_the_planted_window(world):
    cal, whs, truth = world
    t = next(t for t in truth if t.truth_id == "A02")
    wh = whs[t.warehouse_name]
    actual = simulate(wh, cal, SEED)
    without = simulate(wh, cal, SEED, disabled=frozenset({"A02"}))
    delta = actual.credits_used - without.credits_used
    window = cal.mask(t.start, (pd.Timestamp(t.end) + pd.Timedelta(hours=2)).isoformat())
    assert delta[window].sum() > 50
    assert np.abs(delta[~window]).max() < 1e-9


def test_planted_truth_meets_the_brief(world):
    _, _, truth = world
    anomalies = [t for t in truth if t.category == "anomaly"]
    waste = [t for t in truth if t.category == "waste"]
    assert len(anomalies) >= 12 and len(waste) >= 8
    assert {
        "runaway_query_loop",
        "weekend_left_running",
        "upsized_never_downsized",
        "backfill_spike",
        "cloud_services_spike",
        "legit_step_change",
    } <= {t.truth_type for t in anomalies}
    assert {
        "idle_long_autosuspend",
        "oversized_low_load",
        "undersized_queue_spill",
        "poor_pruning_repeated",
        "unused_scheduled_output",
    } <= {t.truth_type for t in waste}


@pytest.mark.parametrize("table", sorted(DOCUMENTED))
def test_raw_columns_are_documented(raw_db, table):
    cols = {r[0] for r in raw_db.execute(f"DESCRIBE RAW.{table}").fetchall()}
    assert cols <= DOCUMENTED[table], cols - DOCUMENTED[table]


def test_metering_invariants(raw_db):
    bad = raw_db.execute("""
        SELECT
          COUNT(*) FILTER (WHERE CREDITS_USED < 0 OR CREDITS_USED_COMPUTE < 0
                           OR CREDITS_USED_CLOUD_SERVICES < 0),
          COUNT(*) FILTER (WHERE ABS(CREDITS_USED - CREDITS_USED_COMPUTE
                                     - CREDITS_USED_CLOUD_SERVICES) > 1e-6),
          COUNT(*) FILTER (WHERE CREDITS_ATTRIBUTED_COMPUTE_QUERIES
                                 > CREDITS_USED_COMPUTE + 1e-6)
        FROM RAW.WAREHOUSE_METERING_HISTORY""").fetchone()
    assert bad == (0, 0, 0)
    dupes = raw_db.execute("""
        SELECT COUNT(*) FROM (SELECT WAREHOUSE_ID, START_TIME FROM RAW.WAREHOUSE_METERING_HISTORY
                              GROUP BY ALL HAVING COUNT(*) > 1)""").fetchone()[0]
    assert dupes == 0


def test_every_warehouse_is_tagged(raw_db):
    untagged = raw_db.execute("""
        SELECT COUNT(DISTINCT m.WAREHOUSE_NAME) FROM RAW.WAREHOUSE_METERING_HISTORY m
        LEFT JOIN RAW.TAG_REFERENCES t ON t.OBJECT_NAME = m.WAREHOUSE_NAME
             AND t.DOMAIN = 'WAREHOUSE' AND t.TAG_NAME = 'COST_CENTER'
        WHERE t.OBJECT_NAME IS NULL""").fetchone()[0]
    assert untagged == 0


def test_ground_truth_patterns_appear_in_query_history(raw_db):
    missing = raw_db.execute("""
        SELECT g.TRUTH_ID FROM RAW.GROUND_TRUTH g
        WHERE g.QUERY_PARAMETERIZED_HASH IS NOT NULL
          AND NOT EXISTS (SELECT 1 FROM RAW.QUERY_HISTORY q
                          WHERE q.QUERY_PARAMETERIZED_HASH = g.QUERY_PARAMETERIZED_HASH)
        """).fetchall()
    assert missing == []


def test_unused_outputs_are_never_read(raw_db):
    reads = raw_db.execute("""
        SELECT COUNT(*) FROM RAW.QUERY_HISTORY
        WHERE QUERY_TYPE = 'SELECT'
          AND (QUERY_TEXT ILIKE '%LEGACY_GL_DAILY%' OR QUERY_TEXT ILIKE '%CRM_ACCOUNTS_SNAP%')
        """).fetchone()[0]
    assert reads == 0
