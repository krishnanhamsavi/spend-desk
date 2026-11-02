"""Hourly warehouse simulation.

For each warehouse and hour: query counts per pattern, then busy time, idle time kept up by
AUTO_SUSPEND, resumes with the 60 second minimum, and credits. Random draws are tied to
(warehouse, pattern), so a run with one planted item switched off reuses every other draw.
That makes "actual minus counterfactual" the exact impact of the item.
"""

from __future__ import annotations

import zlib
from dataclasses import dataclass, field
from datetime import date

import numpy as np
import pandas as pd
from scipy.special import ndtri

from spend_desk.generate.catalog import SIZE_CREDITS, SIZES, TEAM_GROWTH, Pattern, Warehouse

# Share of a weekday's interactive queries in each hour of the day.
_INTERACTIVE = np.array(
    [
        0.02,
        0.01,
        0.01,
        0.01,
        0.01,
        0.02,
        0.05,
        0.25,
        0.70,
        1.00,
        1.00,
        0.95,
        0.75,
        0.95,
        1.00,
        0.95,
        0.80,
        0.55,
        0.30,
        0.15,
        0.10,
        0.07,
        0.04,
        0.03,
    ]
)
_INTERACTIVE = _INTERACTIVE / _INTERACTIVE.sum()

US_HOLIDAYS = {
    "2025-05-26",
    "2025-07-04",
    "2025-09-01",
    "2025-11-27",
    "2025-11-28",
    "2025-12-24",
    "2025-12-25",
    "2025-12-31",
    "2026-01-01",
    "2026-01-19",
    "2026-02-16",
    "2026-05-25",
    "2026-07-03",
    "2026-09-07",
}
MEMORY_GB_PER_CREDIT = 16


@dataclass
class Calendar:
    ts: pd.DatetimeIndex
    hour: np.ndarray
    dow: np.ndarray
    day_idx: np.ndarray
    days: pd.DatetimeIndex
    months: np.ndarray
    holiday: np.ndarray
    close: np.ndarray  # finance close multiplier per hour

    def __len__(self) -> int:
        return len(self.ts)

    def mask(self, start: str | None, end: str | None) -> np.ndarray:
        m = np.ones(len(self.ts), dtype=bool)
        if start:
            m &= self.ts >= pd.Timestamp(start)
        if end:
            m &= self.ts < pd.Timestamp(end)
        return m


def make_calendar(start: date, end: date, fiscal_start_month: int) -> Calendar:
    ts = pd.date_range(
        pd.Timestamp(start), pd.Timestamp(end) + pd.Timedelta(days=1), freq="h", inclusive="left"
    )
    days = pd.date_range(pd.Timestamp(start), pd.Timestamp(end), freq="D")
    day_idx = ((ts.normalize() - days[0]) / pd.Timedelta(days=1)).astype(int).to_numpy()
    holiday_day = days.strftime("%Y-%m-%d").isin(US_HOLIDAYS)

    # Month end close: last 2 and first 4 business days of each month. Quarter ends are heavier.
    business = (days.dayofweek < 5) & ~holiday_day
    close_day = np.ones(len(days))
    frame = pd.DataFrame({"d": days, "b": business})
    frame["ym"] = frame.d.dt.to_period("M")
    for _, g in frame[frame.b].groupby("ym"):
        idx = g.index.to_numpy()
        month = g.d.iloc[0].month
        quarter_end = (month - fiscal_start_month) % 3 == 2
        close_day[idx[-2:]] = 2.2 * (1.3 if quarter_end else 1.0)
        close_day[idx[:4]] = 2.0
    months = ((ts - ts[0]) / pd.Timedelta(days=30.4375)).to_numpy()
    return Calendar(
        ts,
        ts.hour.to_numpy(),
        ts.dayofweek.to_numpy(),
        day_idx,
        days,
        months,
        holiday_day[day_idx],
        close_day[day_idx],
    )


def poisson_inv(u: np.ndarray, lam: np.ndarray) -> np.ndarray:
    """Inverse Poisson CDF. Exact recurrence below 30, normal approximation above.

    Monotone in u for a fixed lam, so shared uniforms give shared counts across runs.
    """
    out = np.zeros_like(lam)
    big = lam >= 30
    if big.any():
        lb = lam[big]
        out[big] = np.maximum(np.floor(lb + np.sqrt(lb) * ndtri(u[big]) + 0.5), 0)
    small = (lam > 0) & ~big
    if small.any():
        us, ls = u[small], lam[small]
        pmf = np.exp(-ls)
        cdf = pmf.copy()
        k = np.zeros_like(ls)
        for i in range(1, 120):
            todo = us > cdf
            if not todo.any():
                break
            k[todo] += 1
            pmf = pmf * ls / i
            cdf = cdf + pmf
        out[small] = k
    return out


def _rng(seed: int, *parts: str) -> np.random.Generator:
    return np.random.default_rng([seed] + [zlib.crc32(p.encode()) for p in parts])


def base_rate(p: Pattern, cal: Calendar) -> np.ndarray:
    """Expected queries per hour before growth, noise, and modifiers."""
    if p.kind == "interactive":
        lam = p.runs_per_day * _INTERACTIVE[cal.hour]
        lam = np.where(cal.holiday, lam * 0.2, lam)
    elif p.kind == "batch":
        lam = np.where(np.isin(cal.hour, p.hours), p.runs_per_day / len(p.hours), 0.0)
    else:
        lam = np.full(len(cal), p.runs_per_day / 24.0)
    return np.where(cal.dow >= 5, lam * p.weekend, lam)


@dataclass
class PatternHours:
    pattern: Pattern
    n: np.ndarray  # underlying query count per hour
    exec_s: np.ndarray  # mean execution seconds per hour
    prune: float
    local_gb: np.ndarray
    remote_gb: np.ndarray


@dataclass
class SimResult:
    size_idx: np.ndarray
    n_total: np.ndarray
    busy_s: np.ndarray
    uptime_s: np.ndarray
    resumes: np.ndarray
    overload: np.ndarray  # demand above capacity, as a share of the hour
    demand_s: np.ndarray  # query runtime seconds needed, before the one hour cap
    credits_compute: np.ndarray
    credits_cloud: np.ndarray
    credits_attributed: np.ndarray
    patterns: list[PatternHours] = field(default_factory=list)

    @property
    def credits_used(self) -> np.ndarray:
        return self.credits_compute + self.credits_cloud


def _active(m, disabled: frozenset, fixes: frozenset) -> bool:
    if m.role in ("base", "plant"):
        return m.truth_id not in disabled
    return m.truth_id in fixes


def simulate(
    wh: Warehouse,
    cal: Calendar,
    seed: int,
    disabled: frozenset = frozenset(),
    fixes: frozenset = frozenset(),
    detail: bool = False,
) -> SimResult:
    hours = len(cal)
    size_idx = np.full(hours, SIZES.index(wh.size))
    auto_suspend = np.full(hours, float(wh.auto_suspend))
    scale: dict[str, np.ndarray] = {}
    prune_override: dict[str, float] = {}
    removed: set[str] = set()
    for m in wh.modifiers:
        if not _active(m, disabled, fixes):
            continue
        mask = cal.mask(m.start, m.end)
        if m.kind == "size":
            size_idx[mask] = SIZES.index(m.size)
        elif m.kind == "auto_suspend":
            auto_suspend[mask] = m.seconds
        elif m.kind == "scale":
            scale.setdefault(m.pattern, np.ones(hours))[mask] *= m.factor
        elif m.kind == "prune":
            prune_override[m.pattern] = m.factor
        elif m.kind == "remove":
            removed.add(m.pattern)

    credits_per_hour = np.array([SIZE_CREDITS[s] for s in SIZES], dtype=float)[size_idx]
    ref_rate = SIZE_CREDITS[wh.ref_size]
    memory = MEMORY_GB_PER_CREDIT * credits_per_hour

    wh_rng = _rng(seed, wh.name, "day")
    day_noise = wh_rng.lognormal(0.0, 0.10, len(cal.days))[cal.day_idx]
    cs_noise = wh_rng.lognormal(0.0, 0.20, hours)
    trend = (1 + TEAM_GROWTH[wh.team]) ** cal.months
    close = cal.close if wh.team == "Finance BI" else np.ones(hours)

    n_total = np.zeros(hours)
    demand = np.zeros(hours)
    cs_queries = np.zeros(hours)
    details = []
    for p in wh.patterns:
        if p.key in removed or (p.truth_id is not None and p.truth_id in disabled):
            continue
        lam = base_rate(p, cal) * scale.get(p.key, 1.0)
        if p.window:
            lam = lam * cal.mask(*p.window)
        if p.grows:
            lam = lam * trend * day_noise
            if p.kind == "interactive":
                lam = lam * close
            elif wh.team == "Finance BI":
                lam = lam * (1 + (close - 1) * 0.4)
        prng = _rng(seed, wh.name, p.key)
        u = prng.random(hours)
        z = prng.standard_normal(hours)
        n = poisson_inv(u, lam)

        ws = p.working_set_gb
        local = np.clip(ws - memory, 0, 3 * memory)
        remote = np.maximum(ws - 4 * memory, 0)
        spill = 1 + (0.6 * local + 2.5 * remote) / ws if ws > 0 else np.ones(hours)
        prune = prune_override.get(p.key, p.prune)
        prune_mult = (prune / p.prune) ** 0.8 if p.prune > 0 else 1.0
        exec_s = (
            p.exec_s
            * (ref_rate / credits_per_hour) ** p.elasticity
            * spill
            * prune_mult
            * np.exp(0.15 * z)
        )

        n_total += n
        demand += n * exec_s
        cs_queries += n * p.cs_per_query
        if detail:
            details.append(PatternHours(p, n, exec_s, prune, local, remote))

    demand /= wh.concurrency
    # Work beyond capacity queues into the next hour. Multi-cluster warehouses add clusters first.
    capacity = 3600.0 * wh.max_clusters
    done = np.zeros(hours)
    backlog = np.zeros(hours)
    carry = 0.0
    for h in range(hours):
        work = demand[h] + carry
        done[h] = min(work, capacity)
        carry = work - done[h]
        backlog[h] = carry
    busy = np.minimum(done, 3600.0)  # first cluster
    extra = done - busy  # seconds run on additional clusters
    overload = backlog / 3600.0
    active = (n_total > 0) | (done > 0)
    # Queries arrive roughly as a Poisson process. Each gap keeps the warehouse up for
    # min(gap, AUTO_SUSPEND), and each gap longer than AUTO_SUSPEND ends in a suspend and resume.
    gap_survive = np.exp(-n_total * auto_suspend / 3600.0)
    idle_up = (3600.0 - busy) * (1 - gap_survive)
    prev_idle = np.concatenate([[True], ~active[:-1]])
    resumes = np.where(active, n_total * gap_survive + (prev_idle & active), 0.0)
    uptime = np.where(active, np.minimum(3600.0, np.maximum(busy + idle_up, 60 * resumes)), 0)

    compute = (uptime + extra) / 3600.0 * credits_per_hour
    attributed = (busy + extra) / 3600.0 * credits_per_hour
    cloud = compute * wh.cs_ratio * cs_noise + cs_queries
    return SimResult(
        size_idx,
        n_total,
        busy,
        uptime,
        resumes,
        overload,
        demand,
        compute,
        cloud,
        attributed,
        details,
    )
