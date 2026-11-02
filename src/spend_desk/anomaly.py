"""Daily spend anomaly detection per team, deterministic attribution, and evaluation.

Detector, for each team and day:
  expected  median of the same weekday over the last 4 reference weeks. Reference days skip
            flagged days and month end close days. On close days the median is scaled by the
            team's own historical close day uplift.
  z         robust z score of log(actual / expected) against the median and MAD of the
            last 120 days.
  flag      z >= threshold and the dollar excess >= minimum.

Flagged days leave the reference set, so a sustained shift keeps being flagged until an owner
confirms it in APP.ANNOTATIONS. From the confirmation date the shift's days rejoin the reference
set and the shift becomes the new baseline. Thresholds are tuned on the validation window and
reported on the held-out window.
"""

from __future__ import annotations

import itertools
import json
import time
from dataclasses import asdict, dataclass
from datetime import date

import numpy as np
import pandas as pd

from spend_desk.config import ROOT, Config, load_config
from spend_desk.forecast import write_table
from spend_desk.models import Target, open_target

SEEDS = ROOT / "seeds" / "annotations.csv"
SF_CACHE = ROOT / "results" / "snowflake_ml_anomaly.csv"
TUNING_PATH = ROOT / "results" / "anomaly_tuning.json"

Z_GRID = [3.0, 3.5, 4.0, 4.5, 5.0, 6.0, 7.0, 8.0]
DOLLAR_GRID = [25, 50, 100, 200, 400]
PERCENTILE_GRID = [0.95, 0.975, 0.99, 0.995, 0.999]
UNMATCHED_TRUTH_WINDOW_DAYS = 30  # ongoing truth items are matched within 30 days of start


@dataclass(frozen=True)
class Params:
    z: float = 3.5
    min_dollars: float = 50.0
    ref_weeks: int = 4
    lookback_weeks: int = 26
    mad_window: int = 120
    warmup_days: int = 56
    shift_z: float = 4.0  # level shift test: z of the 7 day median log ratio
    shift_min_pct: float = 0.15  # and the 7 day median must sit at least 15 percent above expected
    # A shift is judged on its 7 day excess (at least 3 x min_dollars), not on any single day.


# ---------------------------------------------------------------- calendar


def close_days(dates: pd.DatetimeIndex) -> np.ndarray:
    """Month end close: last 2 and first 4 weekdays of each month."""
    df = pd.DataFrame({"d": dates, "wk": dates.dayofweek < 5})
    df["ym"] = df.d.dt.to_period("M")
    out = np.zeros(len(dates), dtype=bool)
    for _, g in df[df.wk].groupby("ym"):
        idx = g.index.to_numpy()
        out[idx[-2:]] = True
        out[idx[:4]] = True
    return out


# ---------------------------------------------------------------- detector


@dataclass
class TeamResult:
    expected: np.ndarray
    z: np.ndarray
    shift_z: np.ndarray
    flagged: np.ndarray
    kind: np.ndarray  # "", "spike", or "shift" per day
    factor: np.ndarray  # baseline multiplier: close day uplift x confirmed shift ratio
    refs: list


def detect_team(
    y: np.ndarray,
    is_close: np.ndarray,
    confirmations: list[tuple[int, int]],
    p: Params,
    price: float,
) -> TeamResult:
    n = len(y)
    expected = np.full(n, np.nan)
    base = np.full(n, np.nan)  # same weekday median before multipliers
    r = np.full(n, np.nan)
    z = np.full(n, np.nan)
    shift_z = np.full(n, np.nan)
    factor = np.ones(n)
    flagged = np.zeros(n, dtype=bool)
    kind = np.full(n, "", dtype=object)
    excluded = np.zeros(n, dtype=bool)
    refs: list = [None] * n
    confirm_at = {c: s for s, c in confirmations}
    floor = None  # start of the latest confirmed shift
    for t in range(n):
        if t in confirm_at:
            floor = confirm_at[t]
            excluded[floor:t] = False
        cands = []
        for k in range(1, p.lookback_weeks + 1):
            j = t - 7 * k
            if j < 0:
                break
            if not excluded[j] and not is_close[j]:
                cands.append(j)
                if len(cands) == p.ref_weeks:
                    break
        if t < p.warmup_days or len(cands) < 2:
            continue
        ratio = 1.0
        if floor is not None:
            post = [c for c in cands if c >= floor]
            if len(post) >= 2:
                cands = post
            else:
                # Too few reference days since the confirmed shift. Scale the old baseline by
                # the observed size of the shift until enough new reference days exist.
                seen = [j for j in range(floor, t) if base[j] > 0 and not excluded[j]]
                if seen:
                    ratio = float(np.median(y[seen] / base[seen]))
        b = float(np.median(y[cands]))
        base[t] = b
        f = 1.0
        if is_close[t]:
            past = [
                c
                for c in range(max(0, t - 180), t)
                if is_close[c] and not excluded[c] and base[c] > 0
            ]
            if len(past) >= 4:
                f = float(np.median(y[past] / base[past]))
        factor[t] = f * ratio
        refs[t] = cands
        expected[t] = b * f * ratio
        r[t] = np.log(max(y[t], 1e-6) / max(expected[t], 1e-6))
        lo = max(0, t - p.mad_window)
        hist = r[lo:t][~np.isnan(r[lo:t]) & ~excluded[lo:t]]
        if len(hist) < 20:
            continue
        med = float(np.median(hist))
        mad = max(1.4826 * float(np.median(np.abs(hist - med))), 0.02)
        z[t] = (r[t] - med) / mad
        excess = (y[t] - expected[t]) * price
        if z[t] >= p.z and excess >= p.min_dollars:
            flagged[t], kind[t] = True, "spike"
            excluded[t] = True
            continue
        # Level shift test: a sustained rise that no single day makes obvious.
        w = np.arange(max(0, t - 6), t + 1)
        w = w[~np.isnan(r[w])]
        if len(w) >= 5:
            m7 = float(np.median(r[w]))
            shift_z[t] = (m7 - med) / (1.2533 * mad / np.sqrt(len(w)))
            window_excess = float(np.nansum(y[w] - expected[w])) * price
            if (
                shift_z[t] >= p.shift_z
                and m7 - med >= np.log(1 + p.shift_min_pct)
                and r[t] - med >= np.log(1 + p.shift_min_pct)
                and window_excess >= 3 * p.min_dollars
            ):
                flagged[t], kind[t] = True, "shift"
                excluded[w[r[w] > med]] = True
    return TeamResult(expected, z, shift_z, flagged, kind, factor, refs)


def load_team_daily(target: Target, as_of: date) -> pd.DataFrame:
    rows = target.fetchall(
        f"select usage_date, team, credits from mart.daily_team_spend "
        f"where usage_date <= date '{as_of}'"
    )
    df = pd.DataFrame(rows, columns=["usage_date", "team", "credits"])
    df["usage_date"] = pd.to_datetime(df["usage_date"])
    df["credits"] = df["credits"].astype(float)
    return df.pivot(index="usage_date", columns="team", values="credits").sort_index()


def load_annotations() -> pd.DataFrame:
    return pd.read_csv(SEEDS, parse_dates=["start_date", "confirmed_on"])


def _confirmations(ann: pd.DataFrame, team: str, dates: pd.DatetimeIndex) -> list[tuple[int, int]]:
    out = []
    for a in ann[ann.team == team].itertuples():
        if a.start_date in dates and a.confirmed_on in dates:
            out.append((dates.get_loc(a.start_date), dates.get_loc(a.confirmed_on)))
    return out


def run_detector(wide: pd.DataFrame, ann: pd.DataFrame, p: Params, price: float):
    is_close = close_days(wide.index)
    results = {}
    for team in wide.columns:
        results[team] = detect_team(
            wide[team].to_numpy(), is_close, _confirmations(ann, team, wide.index), p, price
        )
    return results


def daily_frame(wide: pd.DataFrame, results: dict, price: float) -> pd.DataFrame:
    frames = []
    for team, r in results.items():
        y = wide[team].to_numpy()
        frames.append(
            pd.DataFrame(
                {
                    "team": team,
                    "usage_date": wide.index,
                    "actual_credits": y,
                    "expected_credits": r.expected,
                    "excess_credits": y - r.expected,
                    "excess_dollars": (y - r.expected) * price,
                    "z": r.z,
                    "shift_z": r.shift_z,
                    "flagged": r.flagged,
                    "flag_kind": r.kind,
                    "baseline_multiplier": r.factor,
                }
            )
        )
    return pd.concat(frames, ignore_index=True)


def episodes(daily: pd.DataFrame, ann: pd.DataFrame, p: Params, as_of: date) -> pd.DataFrame:
    """Group flagged days (gaps of one day allowed) into anomaly records."""
    rows = []
    for team, g in daily[daily.flagged].groupby("team"):
        g = g.sort_values("usage_date")
        gid = (g.usage_date.diff().dt.days.fillna(1) > 2).cumsum()
        for _, e in g.groupby(gid):
            start, end = e.usage_date.min(), e.usage_date.max()
            kind = "level_shift" if (e.flag_kind == "shift").any() else "spike"
            confirmed = ann[
                (ann.team == team)
                & (ann.start_date >= start - pd.Timedelta(days=3))
                & (ann.start_date <= end)
            ]
            status = (
                "accepted_as_baseline"
                if len(confirmed)
                else "open"
                if end.date() >= as_of
                else "closed"
            )
            peak = e.loc[e.excess_dollars.idxmax()]
            dollars = e.excess_dollars.sum()
            rows.append(
                {
                    "team": team,
                    "start_date": start.date(),
                    "end_date": end.date(),
                    "peak_date": peak.usage_date.date(),
                    "flagged_days": len(e),
                    "max_z": round(float(e.z.max()), 2),
                    "excess_credits": float(e.excess_credits.sum()),
                    "excess_dollars": float(dollars),
                    "kind": kind,
                    "status": status,
                    "severity": "high"
                    if dollars >= 1000
                    else "medium"
                    if dollars >= 250
                    else "low",
                }
            )
    df = pd.DataFrame(rows).sort_values(["start_date", "team"], ignore_index=True)
    df.insert(0, "anomaly_id", [f"AN{i + 1:03d}" for i in range(len(df))])
    return df


# ---------------------------------------------------------------- attribution


def _contrib(matrix: pd.DataFrame, days: list[int], results: TeamResult) -> pd.Series:
    """Sum over days of actual minus the mean of the same reference days, times the close factor."""
    m = matrix.to_numpy()
    total = np.zeros(m.shape[1])
    for d in days:
        refs = results.refs[d]
        total += m[d] - m[refs].mean(axis=0) * results.factor[d]
    return pd.Series(total, index=matrix.columns)


def attribution_inputs(target: Target, rate: float) -> dict[str, pd.DataFrame]:
    def q(sql, cols):
        df = pd.DataFrame(target.fetchall(sql), columns=cols)
        df["usage_date"] = pd.to_datetime(df["usage_date"])
        return df

    wh = q(
        """select usage_date, team, warehouse_name, credits_used, attributed_credits,
                     idle_credits, cloud_services_credits
              from mart.daily_warehouse_efficiency""",
        ["usage_date", "team", "key", "credits", "execution", "idle", "cloud_services"],
    )
    pat = q(
        f"""select usage_date, team, query_parameterized_hash,
                       sum(attributed_compute_credits) + sum(credits_used_cloud_services) / {rate},
                       count(*) / {rate}
                from core.fct_query group by 1, 2, 3""",
        ["usage_date", "team", "key", "credits", "queries"],
    )
    usr = q(
        f"""select usage_date, team, user_name,
                       sum(attributed_compute_credits) + sum(credits_used_cloud_services) / {rate},
                       count(*) / {rate}
                from core.fct_query group by 1, 2, 3""",
        ["usage_date", "team", "key", "credits", "queries"],
    )
    labels = pd.DataFrame(
        target.fetchall("""
        select query_parameterized_hash, max(warehouse_name), min_by(query_text, start_time)
        from core.fct_query group by 1"""),
        columns=["key", "warehouse", "text"],
    )
    for c in ("credits", "execution", "idle", "cloud_services"):
        if c in wh:
            wh[c] = wh[c].astype(float)
    for df in (pat, usr):
        df["credits"] = df["credits"].astype(float)
        df["queries"] = df["queries"].astype(float)
    return {"warehouse": wh, "pattern": pat, "user": usr, "labels": labels}


def attribute(
    anoms: pd.DataFrame,
    daily: pd.DataFrame,
    wide: pd.DataFrame,
    results: dict,
    inputs: dict,
    price: float,
    top_n: int = 10,
) -> pd.DataFrame:
    dates = wide.index
    rows = []
    labels = inputs["labels"].set_index("key")
    for a in anoms.itertuples():
        r = results[a.team]
        flagged = daily[
            (daily.team == a.team)
            & daily.flagged
            & (daily.usage_date >= pd.Timestamp(a.start_date))
            & (daily.usage_date <= pd.Timestamp(a.end_date))
        ]
        days = [dates.get_loc(d) for d in flagged.usage_date]
        team_y = wide[[a.team]]
        gap = float(_contrib(team_y, days, r).iloc[0])

        def add(dimension, contrib, key_label=lambda k: k, queries=None, aid=a.anomaly_id, gap=gap):
            ranked = contrib.sort_values(ascending=False).head(top_n)
            for rank, (key, c) in enumerate(ranked.items(), start=1):
                rows.append(
                    {
                        "anomaly_id": aid,
                        "dimension": dimension,
                        "rank": rank,
                        "key": key,
                        "label": key_label(key),
                        "contribution_credits": float(c),
                        "contribution_dollars": float(c) * price,
                        "share_of_gap": float(c) / gap if gap else None,
                        "delta_est_queries": None
                        if queries is None
                        else float(queries.get(key, 0)),
                        "gap_credits": gap,
                    }
                )

        wh = inputs["warehouse"][inputs["warehouse"].team == a.team]
        mat = (
            wh.pivot_table(index="usage_date", columns="key", values="credits", aggfunc="sum")
            .reindex(dates, fill_value=0)
            .fillna(0)
        )
        add("warehouse", _contrib(mat, days, r))
        cost = wh.groupby("usage_date")[["execution", "idle", "cloud_services"]].sum()
        add("cost_type", _contrib(cost.reindex(dates, fill_value=0).fillna(0), days, r))
        for dim, key_label in (
            ("pattern", lambda k: f"{labels.loc[k, 'warehouse']}: {labels.loc[k, 'text'][:90]}"),
            ("user", lambda k: k),
        ):
            src = inputs[dim][inputs[dim].team == a.team]
            cred = (
                src.pivot_table(index="usage_date", columns="key", values="credits", aggfunc="sum")
                .reindex(dates, fill_value=0)
                .fillna(0)
            )
            qry = (
                src.pivot_table(index="usage_date", columns="key", values="queries", aggfunc="sum")
                .reindex(dates, fill_value=0)
                .fillna(0)
            )
            add(
                "query_pattern" if dim == "pattern" else "user",
                _contrib(cred, days, r),
                key_label,
                _contrib(qry, days, r),
            )
    return pd.DataFrame(rows)


# ---------------------------------------------------------------- evaluation


def eval_windows(cfg: Config) -> dict[str, tuple[pd.Timestamp, pd.Timestamp]]:
    ev = cfg.raw["evaluation"]
    v0, h0 = pd.Timestamp(ev["validation_start"]), pd.Timestamp(ev["holdout_start"])
    return {"validation": (v0, h0 - pd.Timedelta(days=1)), "holdout": (h0, pd.Timestamp(cfg.as_of))}


def load_truth(target: Target) -> pd.DataFrame:
    rows = target.fetchall("""select truth_id, truth_type, team, warehouse_name, start_time,
                                     end_time, eval_window
                              from raw.ground_truth where category = 'anomaly'""")
    t = pd.DataFrame(
        rows, columns=["truth_id", "truth_type", "team", "warehouse", "start", "end", "window"]
    )
    t["start_date"] = pd.to_datetime(t.start).dt.normalize()
    end = pd.to_datetime(t.end)
    t["match_end"] = end.dt.normalize().fillna(
        t.start_date + pd.Timedelta(days=UNMATCHED_TRUTH_WINDOW_DAYS)
    )
    return t


def load_warehouse_daily(target: Target) -> dict[str, pd.DataFrame]:
    """Daily credits per warehouse, one frame per team."""
    df = pd.DataFrame(
        target.fetchall(
            "select usage_date, team, warehouse_name, credits_used "
            "from mart.daily_warehouse_efficiency"
        ),
        columns=["usage_date", "team", "warehouse", "credits"],
    )
    df["usage_date"] = pd.to_datetime(df["usage_date"])
    df["credits"] = df["credits"].astype(float)
    return {
        team: g.pivot_table(
            index="usage_date", columns="warehouse", values="credits", aggfunc="sum"
        ).fillna(0)
        for team, g in df.groupby("team")
    }


def episode_drivers(wh_daily: pd.DataFrame, days: pd.Series) -> pd.Series:
    """Evaluator side check, independent of any detector: each warehouse's increase on the
    flagged days over the median of the same weekday in the 4 prior weeks, as a share."""
    inc = pd.Series(0.0, index=wh_daily.columns)
    for d in days:
        prior = [d - pd.Timedelta(days=7 * k) for k in range(1, 5)]
        prior = [x for x in prior if x in wh_daily.index]
        if d in wh_daily.index and prior:
            inc += wh_daily.loc[d] - wh_daily.loc[prior].median()
    pos = inc.clip(lower=0)
    return (pos / pos.sum()).sort_values(ascending=False) if pos.sum() > 0 else pos


def evaluate(
    eps: pd.DataFrame,
    daily: pd.DataFrame,
    truth: pd.DataFrame,
    ann: pd.DataFrame,
    window: tuple[pd.Timestamp, pd.Timestamp],
    wh_daily: dict[str, pd.DataFrame],
) -> tuple[dict, pd.DataFrame]:
    w0, w1 = window
    tr = truth[(truth.start_date >= w0) & (truth.start_date <= w1)]
    ep = eps[(pd.to_datetime(eps.start_date) >= w0) & (pd.to_datetime(eps.start_date) <= w1)]
    detail, matched_eps = [], set()
    for t in tr.itertuples():
        near = ep[
            (ep.team == t.team)
            & (pd.to_datetime(ep.end_date) >= t.start_date)
            & (pd.to_datetime(ep.start_date) <= t.match_end + pd.Timedelta(days=1))
        ]
        keep = []
        for e in near.itertuples():
            fdays = daily[
                (daily.team == e.team)
                & daily.flagged
                & (daily.usage_date >= pd.Timestamp(e.start_date))
                & (daily.usage_date <= pd.Timestamp(e.end_date))
                & (daily.usage_date >= t.start_date)
                & (daily.usage_date <= t.match_end + pd.Timedelta(days=1))
            ].usage_date
            share = episode_drivers(wh_daily[t.team], fdays)
            top2 = share.head(2)
            if len(share) and (
                share.index[0] == t.warehouse
                or (t.warehouse in top2.index and top2[t.warehouse] >= 0.25)
            ):
                keep.append((e.anomaly_id, fdays.min()))
        hits = pd.DataFrame(keep, columns=["anomaly_id", "first"])
        matched_eps |= set(hits.anomaly_id)
        first = hits["first"].min() if len(hits) else None
        detail.append(
            {
                "truth_id": t.truth_id,
                "truth_type": t.truth_type,
                "team": t.team,
                "detected": len(hits) > 0,
                "days_to_detect": (first - t.start_date).days if len(hits) else None,
                "anomaly_ids": ",".join(hits.anomaly_id),
            }
        )
    detail = pd.DataFrame(detail)
    # Legit step changes must stop being flagged once confirmed.
    after = 0
    for a in ann.itertuples():
        if w0 <= a.start_date <= w1:
            after += int(
                daily[
                    (daily.team == a.team)
                    & daily.flagged
                    & (daily.usage_date >= a.confirmed_on)
                    & (daily.usage_date <= a.confirmed_on + pd.Timedelta(days=60))
                    & ~daily.usage_date.isin(_other_truth_days(truth, a))
                ].shape[0]
            )
    tp_eps = len(matched_eps)
    precision = tp_eps / len(ep) if len(ep) else 0.0
    recall = detail.detected.mean() if len(detail) else 0.0
    f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
    dtd = detail.days_to_detect.dropna()
    return {
        "truths": len(tr),
        "detected": int(detail.detected.sum()),
        "episodes": len(ep),
        "false_positive_episodes": len(ep) - tp_eps,
        "precision": precision,
        "recall": recall,
        "f1": f1,
        "median_days_to_detect": float(dtd.median()) if len(dtd) else None,
        "legit_days_flagged_after_confirmation": after,
    }, detail


def _other_truth_days(truth: pd.DataFrame, a) -> pd.DatetimeIndex:
    """Days covered by other planted anomalies on the same team, which may rightly be flagged."""
    days = []
    for t in truth[(truth.team == a.team) & (truth.start_date != a.start_date)].itertuples():
        days.extend(pd.date_range(t.start_date, t.match_end))
    return pd.DatetimeIndex(days)


def tune(wide, ann, truth, cfg, price, wh_daily) -> tuple[Params, pd.DataFrame]:
    windows = eval_windows(cfg)
    grid = []
    for zt, md in itertools.product(Z_GRID, DOLLAR_GRID):
        p = Params(z=zt, min_dollars=md)
        res = run_detector(wide, ann, p, price)
        daily = daily_frame(wide, res, price)
        eps = episodes(daily, ann, p, cfg.as_of)
        for wname, w in windows.items():
            m, _ = evaluate(eps, daily, truth, ann, w, wh_daily)
            grid.append(
                {
                    "method": "robust_z",
                    "threshold": zt,
                    "min_dollars": md,
                    "eval_window": wname,
                    **m,
                }
            )
    grid = pd.DataFrame(grid)
    val = grid[grid.eval_window == "validation"].sort_values(
        ["f1", "recall", "median_days_to_detect"], ascending=[False, False, True]
    )
    best = val.iloc[0]
    return Params(z=float(best.threshold), min_dollars=float(best.min_dollars)), grid


# ---------------------------------------------------------------- Snowflake ML benchmark


def run_snowflake_ml(target: Target, cfg: Config, block_days: int = 61) -> pd.DataFrame:
    """Unsupervised SNOWFLAKE.ML.ANOMALY_DETECTION, retrained before each block of days."""
    cur = target.con.cursor()
    cur.execute("""create or replace table mart.anomaly_input as
                   select cast(usage_date as timestamp_ntz) as ts, team as series, credits
                   from mart.daily_team_spend""")
    v0 = pd.Timestamp(cfg.raw["evaluation"]["validation_start"])
    end = pd.Timestamp(cfg.as_of)
    starts = list(pd.date_range(v0, end, freq=f"{block_days}D"))
    frames = []
    for b0 in starts:
        b1 = min(b0 + pd.Timedelta(days=block_days - 1), end)
        t = time.time()
        src = "SPEND_DESK.MART.ANOMALY_INPUT"
        train = (
            f"SYSTEM$QUERY_REFERENCE('select series, ts, credits from {src} "
            f"where ts < ''{b0.date()}''::timestamp_ntz')"
        )
        test = (
            f"SYSTEM$QUERY_REFERENCE('select series, ts, credits from {src} "
            f"where ts between ''{b0.date()}''::timestamp_ntz and ''{b1.date()}''::timestamp_ntz')"
        )
        cur.execute(f"""create or replace snowflake.ml.anomaly_detection app.spend_desk_ad_tmp(
            INPUT_DATA => {train}, SERIES_COLNAME => 'SERIES', TIMESTAMP_COLNAME => 'TS',
            TARGET_COLNAME => 'CREDITS', LABEL_COLNAME => '')""")
        cur.execute(f"""call app.spend_desk_ad_tmp!DETECT_ANOMALIES(
            INPUT_DATA => {test}, SERIES_COLNAME => 'SERIES', TIMESTAMP_COLNAME => 'TS',
            TARGET_COLNAME => 'CREDITS', CONFIG_OBJECT => {{'prediction_interval': 0.99}})""")
        cols = [c[0].upper() for c in cur.description]
        df = pd.DataFrame(cur.fetchall(), columns=cols)
        frames.append(
            pd.DataFrame(
                {
                    "team": df["SERIES"].map(
                        lambda v: json.loads(v) if str(v).startswith('"') else v
                    ),
                    "usage_date": pd.to_datetime(df["TS"]).dt.date,
                    "actual_credits": df["Y"].astype(float),
                    "forecast_credits": df["FORECAST"].astype(float),
                    "percentile": df["PERCENTILE"].astype(float),
                    "block_start": b0.date(),
                }
            )
        )
        print(f"  snowflake_ml block {b0.date()} to {b1.date()}: {time.time() - t:.0f}s")
    cur.execute("drop snowflake.ml.anomaly_detection if exists app.spend_desk_ad_tmp")
    out = pd.concat(frames, ignore_index=True)
    SF_CACHE.parent.mkdir(exist_ok=True)
    out.to_csv(SF_CACHE, index=False)
    return out


def snowflake_ml_daily(sf: pd.DataFrame, pct: float, min_dollars: float, price: float):
    d = sf.copy()
    d["usage_date"] = pd.to_datetime(d.usage_date)
    d["expected_credits"] = d.forecast_credits
    d["excess_credits"] = d.actual_credits - d.forecast_credits
    d["excess_dollars"] = d.excess_credits * price
    d["z"] = d.percentile
    d["flagged"] = (d.percentile >= pct) & (d.excess_dollars >= min_dollars)
    d["flag_kind"] = np.where(d.flagged, "spike", "")
    return d


def tune_snowflake_ml(
    sf, ann, truth, cfg, price, wh_daily
) -> tuple[tuple[float, float], pd.DataFrame]:
    windows = eval_windows(cfg)
    grid = []
    for pct, md in itertools.product(PERCENTILE_GRID, DOLLAR_GRID):
        daily = snowflake_ml_daily(sf, pct, md, price)
        eps = episodes(daily, ann, Params(), cfg.as_of)
        for wname, w in windows.items():
            m, _ = evaluate(eps, daily, truth, ann, w, wh_daily)
            grid.append(
                {
                    "method": "snowflake_ml",
                    "threshold": pct,
                    "min_dollars": md,
                    "eval_window": wname,
                    **m,
                }
            )
    grid = pd.DataFrame(grid)
    best = (
        grid[grid.eval_window == "validation"]
        .sort_values(["f1", "recall", "median_days_to_detect"], ascending=[False, False, True])
        .iloc[0]
    )
    return (float(best.threshold), float(best.min_dollars)), grid


# ---------------------------------------------------------------- pipeline


def run(target: Target, cfg: Config | None = None, snowflake_ml: bool = False) -> pd.DataFrame:
    cfg = cfg or load_config()
    price = cfg.dollars_per_credit
    rate = float(cfg.raw["data"]["query_sample_rate"])
    wide = load_team_daily(target, cfg.as_of)
    ann = load_annotations()
    truth = load_truth(target)
    windows = eval_windows(cfg)

    wh_daily = load_warehouse_daily(target)
    params, grid = tune(wide, ann, truth, cfg, price, wh_daily)
    results = run_detector(wide, ann, params, price)
    daily = daily_frame(wide, results, price)
    anoms = episodes(daily, ann, params, cfg.as_of)
    attr = attribute(anoms, daily, wide, results, attribution_inputs(target, rate), price)
    top = attr[(attr.dimension == "warehouse") & (attr["rank"] == 1)].set_index("anomaly_id")
    anoms["top_warehouse"] = anoms.anomaly_id.map(top["key"])
    anoms["top_warehouse_share"] = anoms.anomaly_id.map(top["share_of_gap"])

    evals, details = [], []
    for wname, w in windows.items():
        m, d = evaluate(anoms, daily, truth, ann, w, wh_daily)
        evals.append(
            {
                "method": "robust_z",
                "eval_window": wname,
                "threshold": params.z,
                "min_dollars": params.min_dollars,
                **m,
            }
        )
        details.append(d.assign(method="robust_z", eval_window=wname))

    if snowflake_ml:
        if target.name != "snowflake":
            raise SystemExit("--snowflake-ml needs --target snowflake")
        sf = run_snowflake_ml(target, cfg)
    else:
        sf = pd.read_csv(SF_CACHE) if SF_CACHE.exists() else None
    if sf is not None:
        (pct, md), sf_grid = tune_snowflake_ml(sf, ann, truth, cfg, price, wh_daily)
        grid = pd.concat([grid, sf_grid], ignore_index=True)
        sf_daily = snowflake_ml_daily(sf, pct, md, price)
        sf_eps = episodes(sf_daily, ann, Params(), cfg.as_of)
        for wname, w in windows.items():
            m, d = evaluate(sf_eps, sf_daily, truth, ann, w, wh_daily)
            evals.append(
                {
                    "method": "snowflake_ml",
                    "eval_window": wname,
                    "threshold": pct,
                    "min_dollars": md,
                    **m,
                }
            )
            details.append(d.assign(method="snowflake_ml", eval_window=wname))

    evals = pd.DataFrame(evals)
    TUNING_PATH.parent.mkdir(exist_ok=True)
    TUNING_PATH.write_text(json.dumps({"robust_z": asdict(params)}, indent=2))
    write_table(target, "app.annotations", ann)
    write_table(target, "mart.anomaly_daily", daily)
    write_table(target, "mart.anomalies", anoms)
    write_table(target, "mart.anomaly_attribution", attr)
    write_table(target, "mart.anomaly_eval", evals)
    write_table(target, "mart.anomaly_eval_detail", pd.concat(details, ignore_index=True))
    write_table(target, "mart.anomaly_tuning_grid", grid)
    return evals


def main(target_name: str, snowflake_ml: bool = False) -> int:
    target = open_target(target_name)
    try:
        target.execute("create schema if not exists app")
        evals = run(target, snowflake_ml=snowflake_ml)
    finally:
        target.close()
    cols = [
        "method",
        "eval_window",
        "threshold",
        "min_dollars",
        "truths",
        "detected",
        "episodes",
        "false_positive_episodes",
        "precision",
        "recall",
        "median_days_to_detect",
        "legit_days_flagged_after_confirmation",
    ]
    print(evals[cols].round(3).to_string(index=False))
    return 0
