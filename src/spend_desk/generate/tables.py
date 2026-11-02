"""Turn simulation results into tables shaped like SNOWFLAKE.ACCOUNT_USAGE views, plus ground truth.

Column names match Snowflake's documentation. Only the columns Spend Desk uses are produced.
Timestamps are account local time (America/Los_Angeles) without an offset.
"""

from __future__ import annotations

import hashlib
import re

import numpy as np
import pandas as pd

from spend_desk.config import Config
from spend_desk.generate.catalog import SIZES, TEAM_SLUG, Warehouse
from spend_desk.generate.engine import Calendar, SimResult, _rng, simulate
from spend_desk.generate.scenario import Truth

HOUR = pd.Timedelta(hours=1)
_TABLE_RE = re.compile(r"\b([A-Z_]+)\.([A-Z_]+)\.([A-Z_]+)\b")


def _ln1(rng: np.random.Generator, sigma: float, size: int) -> np.ndarray:
    """Lognormal noise with mean 1."""
    return rng.lognormal(-sigma * sigma / 2, sigma, size)


def _role_for(user: str, wh: Warehouse) -> str:
    return f"{user.removeprefix('SVC_')}_ROLE" if user.startswith("SVC_") else wh.role_name


def metering(whs: list[Warehouse], sims: dict[str, SimResult], cal: Calendar) -> pd.DataFrame:
    frames = []
    for wh in whs:
        r = sims[wh.name]
        keep = r.credits_used > 0
        frames.append(
            pd.DataFrame(
                {
                    "START_TIME": cal.ts[keep],
                    "END_TIME": cal.ts[keep] + HOUR,
                    "WAREHOUSE_ID": wh.warehouse_id,
                    "WAREHOUSE_NAME": wh.name,
                    "CREDITS_USED": (r.credits_compute + r.credits_cloud)[keep],
                    "CREDITS_USED_COMPUTE": r.credits_compute[keep],
                    "CREDITS_USED_CLOUD_SERVICES": r.credits_cloud[keep],
                    "CREDITS_ATTRIBUTED_COMPUTE_QUERIES": r.credits_attributed[keep],
                }
            )
        )
    df = pd.concat(frames, ignore_index=True)
    for c in df.columns:
        if c.startswith("CREDITS"):
            df[c] = df[c].round(9)
    return df.sort_values(["START_TIME", "WAREHOUSE_ID"], ignore_index=True)


def _query_ids(rng: np.random.Generator, n: int) -> list[str]:
    a = rng.integers(0, 2**63, n, dtype=np.int64)
    b = rng.integers(0, 2**63, n, dtype=np.int64)
    out = []
    for x, y in zip(a.tolist(), b.tolist(), strict=True):
        h = f"{x:016x}{y:016x}"
        out.append(f"{h[:8]}-{h[8:12]}-{h[12:16]}-{h[16:20]}-{h[20:32]}")
    return out


def query_history(
    whs: list[Warehouse], sims: dict[str, SimResult], cal: Calendar, cfg: Config
) -> pd.DataFrame:
    rate = cfg.raw["data"]["query_sample_rate"]
    seed = cfg.raw["data"]["seed"]
    frames = []
    for wh in whs:
        r = sims[wh.name]
        for ph in r.patterns:
            p = ph.pattern
            rng = _rng(seed, wh.name, p.key, "sample")
            k = rng.binomial(ph.n.astype(np.int64), rate)
            hours = np.repeat(np.arange(len(cal)), k)
            m = len(hours)
            if m == 0:
                continue
            exec_ms = ph.exec_s[hours] * _ln1(rng, 0.35, m) * 1000
            heavy_compile = p.cs_per_query >= 1e-4
            compile_ms = rng.lognormal(np.log(1800 if heavy_compile else 120), 0.5, m)
            queued_ms = exec_ms * r.overload[hours] * rng.uniform(0.5, 1.5, m)
            prov_ms = np.where(rng.random(m) < 0.05, rng.uniform(200, 1500, m), 0.0)
            elapsed_ms = compile_ms + queued_ms + prov_ms + exec_ms
            start = cal.ts[hours] + pd.to_timedelta(rng.uniform(0, 3600, m), unit="s")
            literal = rng.integers(1, 400, m)
            months = cal.months[hours]
            parts_total = np.round(p.partitions_total * (1.012**months)).astype(np.int64)
            parts_scanned = np.minimum(
                np.round(parts_total * ph.prune * rng.uniform(0.9, 1.1, m)), parts_total
            ).astype(np.int64)
            bytes_scanned = (
                p.gb_scanned * 1e9 * _ln1(rng, 0.3, m) * (ph.prune / p.prune if p.prune else 1)
            ).astype(np.int64)
            cache = rng.uniform(0.2, 0.8, m) if p.kind == "interactive" else rng.uniform(0, 0.1, m)
            users = np.array(p.users)[rng.integers(0, len(p.users), m)]
            match = _TABLE_RE.search(p.writes or p.sql.replace("{n}", "1"))
            failing = "retry" in p.key
            status = np.where(failing & (rng.random(m) < 0.85), "FAIL", "SUCCESS")
            frames.append(
                pd.DataFrame(
                    {
                        "QUERY_ID": _query_ids(rng, m),
                        "QUERY_TEXT": [p.sql.replace("{n}", str(v)) for v in literal],
                        "DATABASE_NAME": match.group(1) if match else None,
                        "SCHEMA_NAME": match.group(2) if match else None,
                        "QUERY_TYPE": p.query_type,
                        "SESSION_ID": [
                            int(hashlib.md5(f"{u}{d}".encode()).hexdigest()[:12], 16)
                            for u, d in zip(users, cal.day_idx[hours], strict=True)
                        ],
                        "USER_NAME": users,
                        "ROLE_NAME": [_role_for(u, wh) for u in users],
                        "WAREHOUSE_ID": wh.warehouse_id,
                        "WAREHOUSE_NAME": wh.name,
                        "WAREHOUSE_SIZE": np.array(SIZES)[r.size_idx[hours]],
                        "WAREHOUSE_TYPE": "STANDARD",
                        "CLUSTER_NUMBER": 1,
                        "QUERY_TAG": p.query_tag,
                        "EXECUTION_STATUS": status,
                        "START_TIME": start,
                        "END_TIME": start + pd.to_timedelta(elapsed_ms, unit="ms"),
                        "TOTAL_ELAPSED_TIME": elapsed_ms.round().astype(np.int64),
                        "BYTES_SCANNED": bytes_scanned,
                        "PERCENTAGE_SCANNED_FROM_CACHE": cache.round(4),
                        "ROWS_PRODUCED": (rng.lognormal(7, 2, m)).astype(np.int64),
                        "PARTITIONS_SCANNED": parts_scanned,
                        "PARTITIONS_TOTAL": parts_total,
                        "BYTES_SPILLED_TO_LOCAL_STORAGE": (
                            ph.local_gb[hours] * 1e9 * _ln1(rng, 0.3, m)
                        ).astype(np.int64),
                        "BYTES_SPILLED_TO_REMOTE_STORAGE": (
                            ph.remote_gb[hours] * 1e9 * _ln1(rng, 0.3, m)
                        ).astype(np.int64),
                        "COMPILATION_TIME": compile_ms.round().astype(np.int64),
                        "EXECUTION_TIME": exec_ms.round().astype(np.int64),
                        "QUEUED_PROVISIONING_TIME": prov_ms.round().astype(np.int64),
                        "QUEUED_OVERLOAD_TIME": queued_ms.round().astype(np.int64),
                        "CREDITS_USED_CLOUD_SERVICES": (p.cs_per_query * _ln1(rng, 0.3, m)).round(
                            9
                        ),
                        "QUERY_HASH": [
                            hashlib.md5(f"{p.key}:{v}".encode()).hexdigest() for v in literal
                        ],
                        "QUERY_PARAMETERIZED_HASH": p.param_hash,
                    }
                )
            )
    df = pd.concat(frames, ignore_index=True)
    return df.sort_values("START_TIME", ignore_index=True)


def warehouse_events(
    whs: list[Warehouse],
    sims: dict[str, SimResult],
    cal: Calendar,
    seed: int,
    resize_users: dict[str, str],
) -> pd.DataFrame:
    frames = []
    for wh in whs:
        r = sims[wh.name]
        rng = _rng(seed, wh.name, "events")
        k = np.floor(r.resumes + rng.random(len(cal))).astype(np.int64)
        k = np.where((r.n_total > 0) & (r.resumes >= 1), np.maximum(k, 1), k)
        hrs = np.repeat(np.arange(len(cal)), k)
        if len(hrs):
            per = r.uptime_s[hrs] / k[hrs]
            offset = rng.uniform(0, np.maximum(3600 - per, 1))
            resume_ts = cal.ts[hrs] + pd.to_timedelta(offset, unit="s")
            suspend_ts = resume_ts + pd.to_timedelta(np.maximum(per, 60), unit="s")
            size = np.array(SIZES)[r.size_idx[hrs]]
            for ts, name, reason in (
                (resume_ts, "RESUME_WAREHOUSE", "WAREHOUSE_AUTORESUME"),
                (suspend_ts, "SUSPEND_WAREHOUSE", "WAREHOUSE_AUTOSUSPEND"),
            ):
                frames.append(
                    pd.DataFrame(
                        {
                            "TIMESTAMP": ts,
                            "WAREHOUSE_ID": wh.warehouse_id,
                            "WAREHOUSE_NAME": wh.name,
                            "CLUSTER_NUMBER": 1,
                            "EVENT_NAME": name,
                            "EVENT_REASON": reason,
                            "EVENT_STATE": "COMPLETED",
                            "USER_NAME": None,
                            "ROLE_NAME": None,
                            "QUERY_ID": None,
                            "SIZE": size,
                            "CLUSTER_COUNT": "1",
                            "WAREHOUSE_TYPE": "STANDARD",
                        }
                    )
                )
        # Resizes are user requests.
        change = np.flatnonzero(np.diff(r.size_idx)) + 1
        if len(change):
            users = []
            for i in change:
                m = next(
                    (
                        m
                        for m in wh.modifiers
                        if m.kind == "size"
                        and m.role == "plant"
                        and (
                            pd.Timestamp(m.start) == cal.ts[i]
                            or (m.end and pd.Timestamp(m.end) == cal.ts[i])
                        )
                    ),
                    None,
                )
                users.append(None if m is None else resize_users.get(m.truth_id))
            frames.append(
                pd.DataFrame(
                    {
                        "TIMESTAMP": cal.ts[change] + pd.Timedelta(seconds=17),
                        "WAREHOUSE_ID": wh.warehouse_id,
                        "WAREHOUSE_NAME": wh.name,
                        "CLUSTER_NUMBER": None,
                        "EVENT_NAME": "RESIZE_WAREHOUSE",
                        "EVENT_REASON": "WAREHOUSE_RESIZE",
                        "EVENT_STATE": "COMPLETED",
                        "USER_NAME": users,
                        "ROLE_NAME": [None if u is None else wh.role_name for u in users],
                        "QUERY_ID": None,
                        "SIZE": np.array(SIZES)[r.size_idx[change]],
                        "CLUSTER_COUNT": "1",
                        "WAREHOUSE_TYPE": "STANDARD",
                    }
                )
            )
    df = pd.concat(frames, ignore_index=True)
    return df.sort_values(["TIMESTAMP", "WAREHOUSE_ID"], ignore_index=True)


def tag_references(whs: list[Warehouse]) -> pd.DataFrame:
    return pd.DataFrame(
        {
            "TAG_DATABASE": "GOVERNANCE",
            "TAG_SCHEMA": "TAGS",
            "TAG_ID": 501,
            "TAG_NAME": "COST_CENTER",
            "TAG_VALUE": [TEAM_SLUG[w.team] for w in whs],
            "OBJECT_DATABASE": None,
            "OBJECT_SCHEMA": None,
            "OBJECT_ID": [w.warehouse_id for w in whs],
            "OBJECT_NAME": [w.name for w in whs],
            "OBJECT_DELETED": pd.NaT,
            "DOMAIN": "WAREHOUSE",
            "COLUMN_ID": None,
            "COLUMN_NAME": None,
            "APPLY_METHOD": "MANUAL",
        }
    )


def ground_truth(
    whs: list[Warehouse], truth: list[Truth], sims: dict[str, SimResult], cal: Calendar, cfg: Config
) -> tuple[pd.DataFrame, pd.DataFrame]:
    seed = cfg.raw["data"]["seed"]
    price = cfg.dollars_per_credit
    ev = cfg.raw["evaluation"]
    as_of_end = pd.Timestamp(cfg.as_of) + pd.Timedelta(days=1)
    last30 = cal.ts >= as_of_end - pd.Timedelta(days=30)
    by_name = {w.name: w for w in whs}
    rows, daily = [], []
    for t in truth:
        wh = by_name[t.warehouse_name]
        actual = sims[wh.name]
        if t.category == "waste":
            other = simulate(wh, cal, seed, fixes=frozenset({t.truth_id}))
        else:
            other = simulate(wh, cal, seed, disabled=frozenset({t.truth_id}))
        delta = actual.credits_used - other.credits_used
        window = cal.mask(t.start, t.end)
        d = pd.Series(delta, index=cal.ts).resample("D").sum()
        d = d[d.abs() > 1e-6]
        daily.append(
            pd.DataFrame(
                {
                    "TRUTH_ID": t.truth_id,
                    "USAGE_DATE": d.index.date,
                    "WAREHOUSE_NAME": wh.name,
                    "TEAM": wh.team,
                    "DELTA_CREDITS": d.values.round(6),
                }
            )
        )
        ongoing = t.end is None
        monthly = delta[last30].sum() * 30.4375 / 30 if ongoing else None
        runtime = (
            (
                (actual.demand_s - other.demand_s)[last30].sum()
                * wh.concurrency
                / 3600
                * 30.4375
                / 30
            )
            if t.category == "waste"
            else None
        )
        start = pd.Timestamp(t.start)
        eval_window = (
            "warmup"
            if start < pd.Timestamp(ev["validation_start"])
            else "validation"
            if start < pd.Timestamp(ev["holdout_start"])
            else "holdout"
        )
        p = next((p for p in wh.patterns if p.key == t.pattern_key), None)
        rows.append(
            {
                "TRUTH_ID": t.truth_id,
                "CATEGORY": t.category,
                "TRUTH_TYPE": t.truth_type,
                "TEAM": wh.team,
                "WAREHOUSE_NAME": wh.name,
                "START_TIME": start,
                "END_TIME": pd.Timestamp(t.end) if t.end else pd.NaT,
                "EVAL_WINDOW": eval_window,
                "USER_NAME": t.user_name,
                "QUERY_PARAMETERIZED_HASH": p.param_hash if p else None,
                "TRUE_CREDITS_IMPACT": round(float(delta[window].sum()), 3),
                "TRUE_DOLLAR_IMPACT": round(float(delta[window].sum()) * price, 2),
                "MONTHLY_CREDITS_IMPACT": None if monthly is None else round(float(monthly), 3),
                "MONTHLY_DOLLAR_IMPACT": None
                if monthly is None
                else round(float(monthly) * price, 2),
                "MONTHLY_RUNTIME_HOURS_IMPACT": None
                if runtime is None
                else round(float(runtime), 1),
                "EXPECTED_ACTION": t.expected_action,
                "DESCRIPTION": t.description,
                "LINKED_TRUTH_ID": t.linked_truth_id,
            }
        )
    return pd.DataFrame(rows), pd.concat(daily, ignore_index=True)
