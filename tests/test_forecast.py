from datetime import date

import numpy as np
import pandas as pd
import pytest

from spend_desk import forecast as fc
from spend_desk.config import load_config
from spend_desk.models import Target

CFG = load_config()


def test_seasonal_naive_repeats_last_week_and_widens():
    y = np.tile([10.0, 12, 11, 13, 9, 4, 3], 30) + np.random.default_rng(0).normal(0, 0.5, 210)
    f, lo, hi = fc.seasonal_naive(y, 21, 0.8)
    assert np.allclose(f[:7], y[-7:]) and np.allclose(f[7:14], y[-7:])
    width = hi - lo
    assert width[14] > width[7] > width[0]


def test_ets_returns_ordered_positive_intervals():
    t = np.arange(300)
    y = (
        100
        * 1.001**t
        * (1 + 0.2 * (t % 7 < 5))
        * np.exp(np.random.default_rng(1).normal(0, 0.05, 300))
    )
    f, lo, hi = fc.ets(y, 30, 0.8)
    assert len(f) == 30 and (lo > 0).all() and (lo <= f).all() and (f <= hi).all()


def test_backtest_origins_leave_a_full_horizon_before_as_of():
    origins = fc.backtest_origins(CFG, CFG.as_of)
    h = CFG.raw["forecast"]["horizon_days"]
    assert len(origins) == CFG.raw["forecast"]["backtest_folds"]
    assert origins == sorted(origins)
    assert (CFG.as_of - origins[-1]).days == h


def test_selection_needs_every_fold_and_picks_lowest_mape():
    rows = []
    for model, err, folds in [("a", 0.10, 3), ("b", 0.05, 3), ("c", 0.01, 2)]:
        for origin in range(folds):
            rows.append(
                {
                    "team": "T",
                    "model": model,
                    "origin": origin,
                    "usage_date": 1,
                    "actual_credits": 100.0,
                    "forecast_credits": 100 * (1 + err),
                    "lower_credits": 90.0,
                    "upper_credits": 120.0,
                }
            )
    s = fc.score(pd.DataFrame(rows), n_folds=3)
    assert s[s.selected].model.tolist() == ["b"]


@pytest.fixture(scope="module")
def forecasted(db):
    fc.run(Target("duckdb", db), CFG)
    return db


def test_forecast_covers_every_team_and_the_horizon(forecasted):
    n_teams, n_rows, first, last = forecasted.execute("""
        select count(distinct team), count(*), min(usage_date), max(usage_date)
        from mart.forecast_daily""").fetchone()
    h = CFG.raw["forecast"]["horizon_days"]
    assert n_teams == 7 and n_rows == 7 * h
    assert pd.Timestamp(first).date() == date(2026, 9, 16)
    assert (pd.Timestamp(last).date() - pd.Timestamp(first).date()).days == h - 1


def test_every_team_beats_or_matches_the_baseline_choice(forecasted):
    s = forecasted.execute(
        "select team, model, mape, selected from mart.forecast_model_scores"
    ).df()
    for team, g in s.groupby("team"):
        chosen = g[g.selected].mape.iloc[0]
        assert chosen <= g[g.model == "seasonal_naive"].mape.iloc[0], team


def test_forecast_vs_budget_adds_up(forecasted):
    df = forecasted.execute("select * from mart.forecast_vs_budget").df().set_index("team")
    assert len(df) == 7 and set(df.fiscal_quarter) == {"Q3 FY27"}
    assert np.allclose(df.projected_credits, df.qtd_credits + df.remaining_credits)
    assert np.allclose(df.variance_credits, df.projected_credits - df.budget_credits)
    teams = df.drop(index="All teams")
    assert np.isclose(df.loc["All teams", "budget_credits"], teams.budget_credits.sum())
    assert np.isclose(df.loc["All teams", "qtd_credits"], teams.qtd_credits.sum())
    assert (df.projected_low_credits <= df.projected_credits).all()
    assert (df.projected_credits <= df.projected_high_credits).all()


def test_budget_uses_only_data_known_at_plan_date(forecasted):
    base = forecasted.execute("""select team, base_daily_credits from mart.budget_quarterly
                                 where fiscal_quarter = 'Q1 FY27' and team <> 'All teams'""").df()
    expected = forecasted.execute("""
        select team, avg(credits) from mart.daily_team_spend
        where usage_date between date '2025-11-03' and date '2026-01-31' group by team""").df()
    merged = base.merge(expected, on="team")
    assert np.allclose(merged.iloc[:, 1], merged.iloc[:, 2])
