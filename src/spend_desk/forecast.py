"""Daily credit forecasts per team and in total, chosen per team by a rolling-origin backtest.

Models:
  seasonal_naive  repeat the last observed week (the baseline)
  ets             exponential smoothing on log credits, damped trend, weekly seasonality
  snowflake_ml    SNOWFLAKE.ML.FORECAST, trained in Snowflake. Results are cached in results/
                  so the local pipeline can score them without a Snowflake account.

Every number here is computed in code. The agent only reads the stored outputs.
"""

from __future__ import annotations

import json
import time
import warnings
from collections.abc import Callable
from datetime import date, timedelta

import numpy as np
import pandas as pd
from scipy.special import ndtri

from spend_desk.config import ROOT, Config, load_config
from spend_desk.models import Target, open_target, render, template_vars
from spend_desk.sf import split_statements

TOTAL = "All teams"
SF_CACHE = ROOT / "results" / "snowflake_ml_forecast.csv"
VIEW_DIR = ROOT / "sql" / "views"


# ---------------------------------------------------------------- models


def seasonal_naive(y: np.ndarray, h: int, level: float) -> tuple[np.ndarray, ...]:
    """Repeat the last 7 days. Interval width grows with the square root of weeks ahead."""
    fc = np.resize(y[-7:], h)
    resid = (y[7:] - y[:-7])[-182:]
    sigma = np.std(resid, ddof=1)
    weeks = np.arange(h) // 7 + 1
    z = ndtri(0.5 + level / 2)
    return fc, np.maximum(fc - z * sigma * np.sqrt(weeks), 0), fc + z * sigma * np.sqrt(weeks)


def ets(y: np.ndarray, h: int, level: float) -> tuple[np.ndarray, ...]:
    """ETS(A, Ad, A) on log credits, so seasonality and errors scale with the level."""
    from statsmodels.tsa.exponential_smoothing.ets import ETSModel

    ly = pd.Series(np.log(np.maximum(y, 1e-3)))
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        res = ETSModel(
            ly, error="add", trend="add", damped_trend=True, seasonal="add", seasonal_periods=7
        ).fit(disp=False, maxiter=300)
        frame = res.get_prediction(start=len(ly), end=len(ly) + h - 1).summary_frame(
            alpha=1 - level
        )
    return (
        np.exp(frame["mean"].to_numpy()),
        np.exp(frame["pi_lower"].to_numpy()),
        np.exp(frame["pi_upper"].to_numpy()),
    )


LOCAL_MODELS: dict[str, Callable] = {"seasonal_naive": seasonal_naive, "ets": ets}


# ---------------------------------------------------------------- data


def load_series(target: Target) -> pd.DataFrame:
    rows = target.fetchall(
        "select usage_date, team, credits from mart.daily_team_spend order by usage_date"
    )
    df = pd.DataFrame(rows, columns=["usage_date", "team", "credits"])
    df["usage_date"] = pd.to_datetime(df["usage_date"])
    df["credits"] = df["credits"].astype(float)
    wide = df.pivot(index="usage_date", columns="team", values="credits").sort_index()
    wide[TOTAL] = wide.sum(axis=1)
    return wide


def backtest_origins(cfg: Config, last: date) -> list[date]:
    fc = cfg.raw["forecast"]
    h, folds, step = fc["horizon_days"], fc["backtest_folds"], fc["backtest_step_days"]
    return [last - timedelta(days=h + step * k) for k in reversed(range(folds))]


def _frame(team, model, origin, dates, actual, f, lo, hi) -> pd.DataFrame:
    return pd.DataFrame(
        {
            "team": team,
            "model": model,
            "origin": origin,
            "usage_date": dates,
            "horizon": np.arange(1, len(dates) + 1),
            "actual_credits": actual,
            "forecast_credits": f,
            "lower_credits": lo,
            "upper_credits": hi,
        }
    )


def backtest_local(series: pd.DataFrame, origins: list[date], h: int, level: float) -> pd.DataFrame:
    out = []
    for team in series.columns:
        y = series[team]
        for origin in origins:
            train = y[y.index <= pd.Timestamp(origin)].to_numpy()
            test = y[(y.index > pd.Timestamp(origin))].iloc[:h]
            for name, fn in LOCAL_MODELS.items():
                f, lo, hi = fn(train, h, level)
                out.append(_frame(team, name, origin, test.index, test.to_numpy(), f, lo, hi))
    return pd.concat(out, ignore_index=True)


# ---------------------------------------------------------------- Snowflake ML


def _series_name(v) -> str:
    s = str(v)
    return json.loads(s) if s.startswith('"') else s


def run_snowflake_ml(
    target: Target, origins: list[date], final_origin: date, h: int, level: float
) -> pd.DataFrame:
    """Train SNOWFLAKE.ML.FORECAST once per origin (all series in one model) and cache results."""
    cur = target.con.cursor()
    cur.execute("""
        create or replace table mart.forecast_input as
        select cast(usage_date as timestamp_ntz) as ts, team as series, credits
        from mart.daily_team_spend
        union all
        select cast(usage_date as timestamp_ntz), 'All teams', sum(credits)
        from mart.daily_team_spend group by usage_date""")
    frames = []
    for origin in [*origins, final_origin]:
        t = time.time()
        ref = (
            "SYSTEM$QUERY_REFERENCE('select series, ts, credits "
            "from SPEND_DESK.MART.FORECAST_INPUT "
            f"where ts <= ''{origin}''::timestamp_ntz')"
        )
        cur.execute(f"""create or replace snowflake.ml.forecast app.spend_desk_forecast_tmp(
            INPUT_DATA => {ref}, SERIES_COLNAME => 'SERIES', TIMESTAMP_COLNAME => 'TS',
            TARGET_COLNAME => 'CREDITS')""")
        cur.execute(f"""call app.spend_desk_forecast_tmp!FORECAST(
            FORECASTING_PERIODS => {h}, CONFIG_OBJECT => {{'prediction_interval': {level}}})""")
        cols = [c[0].upper() for c in cur.description]
        df = pd.DataFrame(cur.fetchall(), columns=cols)
        frames.append(
            pd.DataFrame(
                {
                    "kind": "final" if origin == final_origin else "backtest",
                    "origin": origin,
                    "team": df["SERIES"].map(_series_name),
                    "usage_date": pd.to_datetime(df["TS"]).dt.date,
                    "forecast_credits": df["FORECAST"].astype(float),
                    "lower_credits": df["LOWER_BOUND"].astype(float),
                    "upper_credits": df["UPPER_BOUND"].astype(float),
                }
            )
        )
        print(f"  snowflake_ml origin {origin}: {time.time() - t:.0f}s")
    cur.execute("drop snowflake.ml.forecast if exists app.spend_desk_forecast_tmp")
    out = pd.concat(frames, ignore_index=True)
    SF_CACHE.parent.mkdir(exist_ok=True)
    out.to_csv(SF_CACHE, index=False)
    return out


def load_snowflake_ml_cache() -> pd.DataFrame | None:
    if not SF_CACHE.exists():
        return None
    df = pd.read_csv(SF_CACHE, parse_dates=["usage_date"])
    df["origin"] = pd.to_datetime(df["origin"]).dt.date
    return df


def backtest_from_snowflake_ml(sf: pd.DataFrame, series: pd.DataFrame) -> pd.DataFrame:
    bt = sf[sf["kind"] == "backtest"].copy()
    bt["usage_date"] = pd.to_datetime(bt["usage_date"])
    long = series.stack().rename("actual_credits").reset_index()
    long.columns = ["usage_date", "team", "actual_credits"]
    bt = bt.merge(long, on=["usage_date", "team"], how="inner")
    bt["model"] = "snowflake_ml"
    bt["horizon"] = (bt["usage_date"] - pd.to_datetime(bt["origin"])).dt.days
    return bt[
        [
            "team",
            "model",
            "origin",
            "usage_date",
            "horizon",
            "actual_credits",
            "forecast_credits",
            "lower_credits",
            "upper_credits",
        ]
    ]


# ---------------------------------------------------------------- scoring


def score(bt: pd.DataFrame, n_folds: int) -> pd.DataFrame:
    bt = bt.assign(
        ape=(bt.actual_credits - bt.forecast_credits).abs() / bt.actual_credits,
        covered=(bt.actual_credits >= bt.lower_credits) & (bt.actual_credits <= bt.upper_credits),
    )
    fold_totals = bt.groupby(["team", "model", "origin"]).agg(
        a=("actual_credits", "sum"), f=("forecast_credits", "sum")
    )
    fold_totals["total_ape"] = (fold_totals.a - fold_totals.f).abs() / fold_totals.a
    s = bt.groupby(["team", "model"]).agg(
        folds=("origin", "nunique"),
        mape=("ape", "mean"),
        coverage=("covered", "mean"),
        n_days=("ape", "size"),
    )
    s["horizon_total_ape"] = fold_totals.groupby(["team", "model"])["total_ape"].mean()
    s = s.reset_index()
    eligible = s[s.folds == n_folds]
    best = eligible.loc[eligible.groupby("team")["mape"].idxmin(), ["team", "model"]]
    s["selected"] = s.set_index(["team", "model"]).index.isin(
        best.set_index(["team", "model"]).index
    )
    return s.sort_values(["team", "mape"], ignore_index=True)


# ---------------------------------------------------------------- final forecast


def final_forecast(
    series: pd.DataFrame,
    scores: pd.DataFrame,
    sf: pd.DataFrame | None,
    as_of: date,
    h: int,
    level: float,
    price: float,
) -> pd.DataFrame:
    dates = pd.date_range(pd.Timestamp(as_of) + pd.Timedelta(days=1), periods=h, freq="D")
    out = []
    for team in series.columns:
        model = scores[(scores.team == team) & scores.selected]["model"].iloc[0]
        if model == "snowflake_ml":
            rows = sf[(sf.kind == "final") & (sf.team == team)].sort_values("usage_date")
            f, lo, hi = (
                rows[c].to_numpy() for c in ("forecast_credits", "lower_credits", "upper_credits")
            )
        else:
            y = series[team][series.index <= pd.Timestamp(as_of)].to_numpy()
            f, lo, hi = LOCAL_MODELS[model](y, h, level)
        out.append(
            pd.DataFrame(
                {
                    "team": team,
                    "model": model,
                    "as_of": as_of,
                    "usage_date": dates.date,
                    "horizon": np.arange(1, h + 1),
                    "forecast_credits": f,
                    "lower_credits": np.maximum(lo, 0),
                    "upper_credits": hi,
                    "interval_level": level,
                    "forecast_dollars": f * price,
                    "lower_dollars": np.maximum(lo, 0) * price,
                    "upper_dollars": hi * price,
                }
            )
        )
    return pd.concat(out, ignore_index=True)


# ---------------------------------------------------------------- writing


def write_table(target: Target, table: str, df: pd.DataFrame) -> None:
    df = df.copy()
    for c in df.columns:
        if df[c].dtype == object and len(df) and isinstance(df[c].iloc[0], date):
            df[c] = pd.to_datetime(df[c])
    if target.name == "duckdb":
        target.con.register("_df", df)
        target.con.execute(f"create or replace table {table} as select * from _df")
        target.con.unregister("_df")
    else:
        from snowflake.connector.pandas_tools import write_pandas

        schema, name = table.upper().split(".")
        df.columns = [c.upper() for c in df.columns]
        write_pandas(
            target.con,
            df,
            name,
            schema=schema,
            auto_create_table=True,
            overwrite=True,
            use_logical_type=True,
        )


def run_views(target: Target, cfg: Config) -> None:
    variables = template_vars(cfg)
    for f in sorted(VIEW_DIR.glob("*.sql")):
        for stmt in split_statements(render(f.read_text(encoding="utf-8"), variables)):
            target.execute(stmt)


def run(target: Target, cfg: Config | None = None, snowflake_ml: bool = False) -> pd.DataFrame:
    cfg = cfg or load_config()
    fc = cfg.raw["forecast"]
    h, level, folds = fc["horizon_days"], fc["interval_level"], fc["backtest_folds"]
    series = load_series(target)
    series = series[series.index <= pd.Timestamp(cfg.as_of)]
    origins = backtest_origins(cfg, cfg.as_of)

    bt = backtest_local(series, origins, h, level)
    sf = None
    if snowflake_ml:
        if target.name != "snowflake":
            raise SystemExit("--snowflake-ml needs --target snowflake")
        sf = run_snowflake_ml(target, origins, cfg.as_of, h, level)
    else:
        sf = load_snowflake_ml_cache()
    if sf is not None:
        bt = pd.concat([bt, backtest_from_snowflake_ml(sf, series)], ignore_index=True)

    scores = score(bt, folds)
    forecast = final_forecast(series, scores, sf, cfg.as_of, h, level, cfg.dollars_per_credit)
    write_table(target, "mart.forecast_backtest", bt)
    write_table(target, "mart.forecast_model_scores", scores)
    write_table(target, "mart.forecast_daily", forecast)
    run_views(target, cfg)
    return scores


def main(target_name: str, snowflake_ml: bool = False) -> int:
    target = open_target(target_name)
    try:
        scores = run(target, snowflake_ml=snowflake_ml)
    finally:
        target.close()
    view = scores.assign(
        mape=scores.mape.round(3),
        coverage=scores.coverage.round(3),
        horizon_total_ape=scores.horizon_total_ape.round(3),
    )
    print(
        view[
            ["team", "model", "folds", "mape", "coverage", "horizon_total_ape", "selected"]
        ].to_string(index=False)
    )
    return 0
