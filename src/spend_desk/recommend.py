"""Optimization recommender. Each rule is a class with a detection query, evidence, a savings
formula with a conservative and an expected estimate, a confidence level, and an exact action.

All savings are monthly, computed in code from the last 30 days before the as-of date, and
scaled to an average month (30.4375 days). Positive savings mean a lower bill.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import timedelta

import numpy as np
import pandas as pd

from spend_desk.config import Config, load_config
from spend_desk.forecast import write_table
from spend_desk.models import Target, open_target

WINDOW_DAYS = 30
MONTH = 30.4375 / WINDOW_DAYS
SIZES = ["X-Small", "Small", "Medium", "Large", "X-Large", "2X-Large"]
SQL_SIZE = {
    "X-Small": "XSMALL",
    "Small": "SMALL",
    "Medium": "MEDIUM",
    "Large": "LARGE",
    "X-Large": "XLARGE",
    "2X-Large": "XXLARGE",
}
RATE = {s: 2**i for i, s in enumerate(SIZES)}
MIN_MONTHLY_DOLLARS = 50.0

RULE_TO_TRUTH = {
    "auto_suspend": "idle_long_autosuspend",
    "downsize": "oversized_low_load",
    "upsize": "undersized_queue_spill",
    "pruning": "poor_pruning_repeated",
    "unused_output": "unused_scheduled_output",
}


@dataclass
class Recommendation:
    rule: str
    warehouse_name: str
    team: str
    title: str
    action: str
    confidence: str
    method: str
    monthly_credits_conservative: float
    monthly_credits_expected: float
    evidence: dict = field(default_factory=dict)
    query_parameterized_hash: str | None = None


@dataclass
class Context:
    target: Target
    cfg: Config
    window_start: str
    as_of: str
    price: float
    sample_rate: float

    def df(self, sql: str) -> pd.DataFrame:
        if self.target.name == "duckdb":
            out = self.target.con.execute(sql).df()
        else:
            cur = self.target.con.cursor().execute(sql)
            out = pd.DataFrame(cur.fetchall(), columns=[c[0] for c in cur.description])
        out.columns = [c.lower() for c in out.columns]
        for c in out.columns:
            if out[c].dtype == object:
                try:
                    out[c] = pd.to_numeric(out[c])
                except (ValueError, TypeError):
                    pass
        return out


class Rule:
    name: str
    confidence: str

    def detect(self, ctx: Context) -> pd.DataFrame:
        raise NotImplementedError

    def recommend(self, ctx: Context) -> list[Recommendation]:
        raise NotImplementedError


class AutoSuspendRule(Rule):
    """Idle warehouses with a long AUTO_SUSPEND. Recommend 60 seconds."""

    name, confidence = "auto_suspend", "high"
    min_auto_suspend = 300

    def detect(self, ctx):
        return ctx.df(f"""
            select h.warehouse_name, max(h.team) as team, max(w.auto_suspend) as auto_suspend,
                   max(w.size) as size, max(h.credits_per_hour) as credits_per_hour,
                   sum(h.resumes) as resumes, sum(h.idle_credits) as idle_credits,
                   sum(h.idle_excl_min_billing_credits) as idle_excl_floor,
                   sum(h.credits_used_compute) as compute_credits
            from core.fct_warehouse_hourly h
            join raw.warehouses_snapshot w on w.name = h.warehouse_name
            where w.auto_suspend >= {self.min_auto_suspend}
              and h.usage_date between date '{ctx.window_start}' and date '{ctx.as_of}'
            group by h.warehouse_name""")

    def recommend(self, ctx):
        out = []
        for r in self.detect(ctx).itertuples():
            hourly = ctx.df(f"""
                select h.hour_start, h.idle_excl_min_billing_credits as idle,
                       coalesce(q.n, 0) / {ctx.sample_rate} as est_queries
                from core.fct_warehouse_hourly h
                left join (select hour_start, count(*) as n from core.fct_query
                           where warehouse_name = '{r.warehouse_name}' group by hour_start) q
                  on q.hour_start = h.hour_start
                where h.warehouse_name = '{r.warehouse_name}'
                  and h.usage_date between date '{ctx.window_start}' and date '{ctx.as_of}'""")
            # Conservative: every observed suspend was preceded by AUTO_SUSPEND seconds of idle.
            # At 60 seconds each of those tails shrinks by (AUTO_SUSPEND - 60).
            tail = r.resumes * (r.auto_suspend - 60) / 3600 * r.credits_per_hour
            conservative = min(tail, r.idle_excl_floor)
            # Expected: with Poisson arrivals at n queries per hour, the idle kept up by a timeout
            # of s seconds is proportional to 1 - exp(-n s / 3600). Scale observed idle by that ratio.
            n = hourly.est_queries.clip(lower=1).to_numpy()
            keep = (1 - np.exp(-n * 60 / 3600)) / (1 - np.exp(-n * r.auto_suspend / 3600))
            expected = float((hourly.idle.to_numpy() * (1 - keep)).sum())
            expected = max(expected, conservative)
            out.append(
                Recommendation(
                    self.name,
                    r.warehouse_name,
                    r.team,
                    f"Lower AUTO_SUSPEND on {r.warehouse_name} from {r.auto_suspend}s to 60s",
                    f"ALTER WAREHOUSE {r.warehouse_name} SET AUTO_SUSPEND = 60;",
                    self.confidence,
                    "Conservative: each observed suspend tail shortened to 60s. Expected: idle "
                    "rescaled by a Poisson arrival model at the observed hourly query rate.",
                    conservative * MONTH,
                    expected * MONTH,
                    {
                        "auto_suspend_s": int(r.auto_suspend),
                        "idle_share": r.idle_credits / r.compute_credits,
                        "resumes_30d": int(r.resumes),
                        "idle_credits_30d": r.idle_credits,
                    },
                )
            )
        return out


class DownsizeRule(Rule):
    """Oversized warehouses: (a) small fast queries, no queueing or spill, Medium or larger;
    (b) upsized recently with poor scaling, so the larger size buys little speed."""

    name, confidence = "downsize", "medium"

    def detect(self, ctx):
        return ctx.df(f"""
            with e as (
                select warehouse_name, max(team) as team, sum(credits_used) as credits,
                       sum(compute_credits) as compute_credits, sum(idle_credits) as idle_credits,
                       sum(attributed_credits) as attributed_credits,
                       avg(coalesce(queued_time_share, 0)) as queued_share,
                       max(coalesce(remote_spill_query_share, 0)) as remote_spill_share,
                       avg(coalesce(local_spill_query_share, 0)) as local_spill_share
                from mart.daily_warehouse_efficiency
                where usage_date between date '{ctx.window_start}' and date '{ctx.as_of}'
                group by warehouse_name
            ),
            q as (
                select warehouse_name,
                       percentile_cont(0.95) within group (order by elapsed_s) as p95_elapsed_s
                from core.fct_query
                where usage_date between date '{ctx.window_start}' and date '{ctx.as_of}'
                group by warehouse_name
            )
            select e.*, q.p95_elapsed_s, w.size, w.max_cluster_count
            from e join q using (warehouse_name)
            join raw.warehouses_snapshot w on w.name = e.warehouse_name""")

    def recommend(self, ctx):
        out = []
        df = self.detect(ctx)
        resized = ctx.df(f"""
            select warehouse_name, max("TIMESTAMP") as resized_at
            from raw.warehouse_events_history
            where event_name = 'RESIZE_WAREHOUSE'
              and "TIMESTAMP" >= date '{ctx.as_of}' - interval '180 days'
            group by warehouse_name""")
        for r in df.itertuples():
            rank = SIZES.index(r.size)
            # (a) low load
            if (
                rank >= 2
                and r.max_cluster_count == 1
                and r.queued_share < 0.01
                and r.remote_spill_share == 0
                and r.local_spill_share < 0.05
                and r.p95_elapsed_s < 30
            ):
                steps = min(2 if r.p95_elapsed_s < 10 else 1, rank)
                new = SIZES[rank - steps]
                keep = 0.5**steps
                out.append(
                    Recommendation(
                        self.name,
                        r.warehouse_name,
                        r.team,
                        f"Downsize {r.warehouse_name} from {r.size} to {new}",
                        f"ALTER WAREHOUSE {r.warehouse_name} SET WAREHOUSE_SIZE = '{SQL_SIZE[new]}';",
                        self.confidence,
                        "Conservative: only idle credits scale with size, query credits unchanged. "
                        "Expected: short queries do not slow down, so all compute scales with size.",
                        r.idle_credits * (1 - keep) * MONTH,
                        r.compute_credits * (1 - keep) * MONTH,
                        {
                            "p95_elapsed_s": r.p95_elapsed_s,
                            "queued_share": r.queued_share,
                            "remote_spill_share": r.remote_spill_share,
                            "idle_share": r.idle_credits / r.compute_credits,
                            "trigger": "low_load",
                        },
                    )
                )
                continue
            # (b) poor scaling after a recent upsize
            hit = resized[resized.warehouse_name == r.warehouse_name]
            if not len(hit):
                continue
            at = pd.Timestamp(hit.resized_at.iloc[0])
            prev = ctx.df(f"""
                select warehouse_size, avg(execution_s) as exec_s, count(*) as n
                from core.fct_query where warehouse_name = '{r.warehouse_name}'
                  and start_time between timestamp '{at - timedelta(days=28)}'
                                     and timestamp '{at + timedelta(days=28)}'
                group by warehouse_size""").set_index("warehouse_size")
            if r.size not in prev.index or len(prev) < 2:
                continue
            before = prev.drop(index=r.size).sort_values("n").index[-1]
            if RATE[before] >= RATE[r.size]:
                continue
            speedup = prev.loc[before, "exec_s"] / prev.loc[r.size, "exec_s"]
            efficiency = speedup / (RATE[r.size] / RATE[before])
            if efficiency >= 0.6:
                continue
            cost_ratio = (RATE[before] * prev.loc[r.size, "exec_s"] * speedup) / (
                RATE[r.size] * prev.loc[r.size, "exec_s"]
            )
            expected = r.attributed_credits * (1 - cost_ratio) + r.idle_credits * (
                1 - RATE[before] / RATE[r.size]
            )
            out.append(
                Recommendation(
                    self.name,
                    r.warehouse_name,
                    r.team,
                    f"Return {r.warehouse_name} from {r.size} to {before}",
                    f"ALTER WAREHOUSE {r.warehouse_name} SET WAREHOUSE_SIZE = '{SQL_SIZE[before]}';",
                    "high",
                    "Compares average query execution time in the 28 days before and after the "
                    "last resize. Expected: query credits at the old size and speed, idle credits "
                    "at the old rate. Conservative: 75 percent of expected.",
                    0.75 * expected * MONTH,
                    expected * MONTH,
                    {
                        "resized_at": str(at),
                        "previous_size": before,
                        "speedup": speedup,
                        "scaling_efficiency": efficiency,
                        "trigger": "poor_scaling_after_upsize",
                    },
                )
            )
        return out


class UpsizeRule(Rule):
    """Undersized warehouses: heavy queueing and remote spill. Upsizing costs more per hour,
    so the recommendation shows the cost range alongside the time recovered."""

    name, confidence = "upsize", "medium"

    def detect(self, ctx):
        return ctx.df(f"""
            select e.warehouse_name, max(e.team) as team, max(w.size) as size,
                   sum(e.compute_credits) as compute_credits,
                   avg(coalesce(e.queued_time_share, 0)) as queued_share,
                   avg(coalesce(e.remote_spill_query_share, 0)) as remote_spill_share,
                   avg(e.remote_spill_gb_per_query) as remote_spill_gb_per_query
            from mart.daily_warehouse_efficiency e
            join raw.warehouses_snapshot w on w.name = e.warehouse_name
            where e.usage_date between date '{ctx.window_start}' and date '{ctx.as_of}'
            group by e.warehouse_name
            having avg(coalesce(e.queued_time_share, 0)) >= 0.2
               and avg(coalesce(e.remote_spill_query_share, 0)) >= 0.3""")

    def recommend(self, ctx):
        out = []
        for r in self.detect(ctx).itertuples():
            queued_h = ctx.df(f"""
                select sum(queued_overload_s) / 3600.0 / {ctx.sample_rate} as h
                from core.fct_query where warehouse_name = '{r.warehouse_name}'
                  and usage_date between date '{ctx.window_start}' and date '{ctx.as_of}'""").h[0]
            rank = SIZES.index(r.size)
            steps = min(2 if r.remote_spill_share >= 0.9 else 1, len(SIZES) - 1 - rank)
            new = SIZES[rank + steps]
            out.append(
                Recommendation(
                    self.name,
                    r.warehouse_name,
                    r.team,
                    f"Upsize {r.warehouse_name} from {r.size} to {new}",
                    f"ALTER WAREHOUSE {r.warehouse_name} SET WAREHOUSE_SIZE = '{SQL_SIZE[new]}';",
                    self.confidence,
                    "Cost change is a range. Conservative: runtime does not improve, so compute "
                    f"rises {2**steps}x. Expected: spill bound jobs speed up in proportion to size, "
                    "so compute is roughly flat. Benefit: queued time removed.",
                    -r.compute_credits * (2**steps - 1) * MONTH,
                    0.0,
                    {
                        "queued_share": r.queued_share,
                        "remote_spill_share": r.remote_spill_share,
                        "remote_spill_gb_per_query": r.remote_spill_gb_per_query,
                        "queued_hours_per_month": queued_h * MONTH,
                    },
                )
            )
        return out


class PruningRule(Rule):
    """Repeated expensive query patterns that scan most partitions of a large table."""

    name, confidence = "pruning", "medium"

    def detect(self, ctx):
        return ctx.df(f"""
            with p as (
                select query_parameterized_hash, warehouse_name, max(team) as team,
                       count(*) / {ctx.sample_rate} as est_queries,
                       avg(scan_ratio) as scan_ratio, avg(partitions_total) as partitions_total,
                       sum(attributed_compute_credits) as attributed_credits,
                       min_by(query_text, start_time) as example
                from core.fct_query
                where usage_date between date '{ctx.window_start}' and date '{ctx.as_of}'
                group by query_parameterized_hash, warehouse_name
            ),
            w as (
                select warehouse_name, sum(idle_credits) / nullif(sum(compute_credits), 0)
                       as idle_share
                from mart.daily_warehouse_efficiency
                where usage_date between date '{ctx.window_start}' and date '{ctx.as_of}'
                group by warehouse_name
            )
            select p.*, w.idle_share from p join w using (warehouse_name)
            where p.scan_ratio >= 0.8 and p.partitions_total >= 1000 and p.est_queries >= 300
              and p.attributed_credits * {ctx.price} >= 100""")

    def recommend(self, ctx):
        out = []
        for r in self.detect(ctx).itertuples():
            # Pruning to about 10 percent of partitions removes most scan time. Only the share of
            # the warehouse that is not idle turns faster queries into a smaller bill.
            expected = r.attributed_credits * 0.8 * (1 - r.idle_share)
            out.append(
                Recommendation(
                    self.name,
                    r.warehouse_name,
                    r.team,
                    f"Fix pruning on a pattern that runs {r.est_queries:,.0f} times a month on "
                    f"{r.warehouse_name}",
                    "Rewrite the filter so it can prune (filter on the clustering column without "
                    "wrapping it in a function), add a clustering key, or materialize the result. "
                    f"Pattern: {r.example[:120]}",
                    self.confidence,
                    "Expected: 80 percent of the pattern's execution credits, times the share of "
                    "warehouse time that is not idle. Conservative: half of expected.",
                    0.5 * expected * MONTH,
                    expected * MONTH,
                    {
                        "scan_ratio": r.scan_ratio,
                        "partitions_total": r.partitions_total,
                        "est_queries_30d": r.est_queries,
                        "attributed_credits_30d": r.attributed_credits,
                        "warehouse_idle_share": r.idle_share,
                    },
                    query_parameterized_hash=r.query_parameterized_hash,
                )
            )
        return out


class UnusedOutputRule(Rule):
    """Scheduled jobs whose output table no query has read in 90 days."""

    name, confidence = "unused_output", "medium"
    lookback_days = 90

    def detect(self, ctx):
        writes = ctx.df(f"""
            select query_parameterized_hash, warehouse_name, max(team) as team,
                   min_by(query_text, start_time) as example, max(usage_date) as last_write,
                   count(*) / {ctx.sample_rate} as est_runs,
                   sum(case when usage_date >= date '{ctx.window_start}'
                       then attributed_compute_credits else 0 end) as attributed_credits_30d
            from core.fct_query
            where query_type in ('CREATE_TABLE_AS_SELECT', 'INSERT', 'MERGE')
              and usage_date >= date '{ctx.as_of}' - interval '{self.lookback_days} days'
            group by query_parameterized_hash, warehouse_name""")
        rows = []
        for w in writes.itertuples():
            words = w.example.split()
            key = "TABLE" if "TABLE" in words else "INTO"
            target = words[words.index(key) + 1]
            if pd.Timestamp(w.last_write) < pd.Timestamp(ctx.as_of) - pd.Timedelta(days=14):
                continue  # not scheduled any more
            reads = ctx.df(f"""
                select count(*) as n from core.fct_query
                where usage_date >= date '{ctx.as_of}' - interval '{self.lookback_days} days'
                  and query_text like '%{target}%'
                  and query_text not like '%TABLE {target} %'
                  and query_text not like '%INTO {target} %'""").n[0]
            if reads == 0:
                rows.append({**w._asdict(), "target": target})
        return pd.DataFrame(rows)

    def recommend(self, ctx):
        out = []
        df = self.detect(ctx)
        if not len(df):
            return out
        ratio = (
            ctx.df(f"""
            select warehouse_name, sum(compute_credits) / nullif(sum(attributed_credits), 0) as r
            from mart.daily_warehouse_efficiency
            where usage_date between date '{ctx.window_start}' and date '{ctx.as_of}'
            group by warehouse_name""")
            .set_index("warehouse_name")
            .r
        )
        for r in df.itertuples():
            uplift = min(float(ratio.get(r.warehouse_name, 1.0)), 2.0)
            out.append(
                Recommendation(
                    self.name,
                    r.warehouse_name,
                    r.team,
                    f"Pause the job that rebuilds {r.target}. No query has read it in "
                    f"{self.lookback_days} days",
                    f"Pause the scheduled job writing {r.target}, confirm with the owner, then "
                    f"DROP TABLE {r.target};",
                    self.confidence,
                    "Conservative: the job's execution credits. Expected: plus its share of warehouse "
                    "idle time (compute over execution credits, capped at 2x). QUERY_HISTORY is "
                    "sampled, so a rare reader could be missed. Confirm before dropping.",
                    r.attributed_credits_30d * MONTH,
                    r.attributed_credits_30d * uplift * MONTH,
                    {
                        "target_table": r.target,
                        "reads_90d_in_sample": 0,
                        "est_runs_90d": r.est_runs,
                        "last_write": str(r.last_write),
                    },
                    query_parameterized_hash=r.query_parameterized_hash,
                )
            )
        return out


RULES: list[Rule] = [
    AutoSuspendRule(),
    DownsizeRule(),
    UpsizeRule(),
    PruningRule(),
    UnusedOutputRule(),
]


def to_frame(recs: list[Recommendation], price: float) -> pd.DataFrame:
    df = pd.DataFrame([asdict(r) for r in recs])
    df["evidence"] = df.evidence.map(lambda e: json.dumps(e, default=float))
    df["monthly_dollars_conservative"] = df.monthly_credits_conservative * price
    df["monthly_dollars_expected"] = df.monthly_credits_expected * price
    keep = (df.rule == "upsize") | (df.monthly_dollars_expected >= MIN_MONTHLY_DOLLARS)
    df = df[keep].sort_values("monthly_dollars_expected", ascending=False, ignore_index=True)
    df.insert(0, "rec_id", [f"R{i + 1:02d}" for i in range(len(df))])
    return df


def evaluate(recs: pd.DataFrame, truth: pd.DataFrame) -> tuple[dict, pd.DataFrame]:
    waste = truth[truth.category == "waste"]
    matched_recs, detail = set(), []
    for t in waste.itertuples():
        hit = recs[
            (recs.warehouse_name == t.warehouse_name)
            & (recs.rule.map(RULE_TO_TRUTH) == t.truth_type)
        ]
        if t.query_parameterized_hash and t.truth_type in (
            "poor_pruning_repeated",
            "unused_scheduled_output",
        ):
            hit = hit[hit.query_parameterized_hash == t.query_parameterized_hash]
        matched_recs |= set(hit.rec_id)
        detail.append(
            {
                "truth_id": t.truth_id,
                "truth_type": t.truth_type,
                "warehouse_name": t.warehouse_name,
                "found": len(hit) > 0,
                "rec_ids": ",".join(hit.rec_id),
                "planted_monthly_dollars": t.monthly_dollar_impact,
                "rec_monthly_dollars_expected": float(hit.monthly_dollars_expected.sum()),
                "rec_monthly_dollars_conservative": float(hit.monthly_dollars_conservative.sum()),
            }
        )
    detail = pd.DataFrame(detail)
    # Savings metrics cover the rules that cut cost. Upsizes are a cost, reported separately.
    savings = detail[detail.truth_type != "undersized_queue_spill"]
    found = savings[savings.found]
    false_recs = recs[~recs.rec_id.isin(matched_recs)]
    upsizes = recs[recs.rule == "upsize"]
    return {
        "planted_waste": len(detail),
        "found": int(detail.found.sum()),
        "share_found": float(detail.found.mean()),
        "recommendations": len(recs),
        "false_recommendations": len(false_recs),
        "planted_savings_monthly": float(savings.planted_monthly_dollars.sum()),
        "planted_savings_found_monthly": float(found.planted_monthly_dollars.sum()),
        "estimated_savings_expected_monthly": float(found.rec_monthly_dollars_expected.sum()),
        "estimated_savings_conservative_monthly": float(
            found.rec_monthly_dollars_conservative.sum()
        ),
        "estimate_to_planted_ratio": float(
            found.rec_monthly_dollars_expected.sum() / found.planted_monthly_dollars.sum()
        )
        if len(found)
        else None,
        "median_abs_pct_error_per_item": float(
            (
                (found.rec_monthly_dollars_expected - found.planted_monthly_dollars).abs()
                / found.planted_monthly_dollars
            ).median()
        )
        if len(found)
        else None,
        "upsize_recommendations": len(upsizes),
        "upsize_worst_case_monthly_cost": float(-upsizes.monthly_dollars_conservative.sum()),
    }, detail


def run(target: Target, cfg: Config | None = None) -> dict:
    cfg = cfg or load_config()
    as_of = cfg.as_of
    ctx = Context(
        target,
        cfg,
        str(as_of - timedelta(days=WINDOW_DAYS - 1)),
        str(as_of),
        cfg.dollars_per_credit,
        float(cfg.raw["data"]["query_sample_rate"]),
    )
    recs = [rec for rule in RULES for rec in rule.recommend(ctx)]
    df = to_frame(recs, cfg.dollars_per_credit)
    truth = ctx.df("select * from raw.ground_truth")
    metrics, detail = evaluate(df, truth)
    write_table(target, "mart.recommendations", df)
    write_table(target, "mart.recommendation_eval", pd.DataFrame([metrics]))
    write_table(target, "mart.recommendation_eval_detail", detail)
    return {"recommendations": df, "metrics": metrics, "detail": detail}


def main(target_name: str) -> int:
    target = open_target(target_name)
    try:
        out = run(target)
    finally:
        target.close()
    df = out["recommendations"]
    print(
        df[
            [
                "rec_id",
                "rule",
                "warehouse_name",
                "monthly_dollars_conservative",
                "monthly_dollars_expected",
                "confidence",
            ]
        ]
        .round(0)
        .to_string(index=False)
    )
    print(out["detail"].round(0).to_string(index=False))
    print(json.dumps(out["metrics"], indent=2))
    return 0
