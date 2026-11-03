"""The only way the agent touches data. Six read-only tools over MART, no raw SQL from the model.

Every number is computed here and rounded the way it should be quoted: dollars to whole
dollars, credits to one decimal, percentages to one decimal. The agent quotes, never computes.
"""

from __future__ import annotations

import json
import re
from datetime import date, timedelta

import pandas as pd
import yaml

from spend_desk.config import ROOT, Config
from spend_desk.models import Target

SEVERITY_RANK = {"low": 0, "medium": 1, "high": 2}

TOOL_SPECS = [
    {
        "name": "get_spend_summary",
        "description": (
            "Spend for a period, optionally for one team, compared with the period of equal "
            "length just before it. With no team: totals and a by team breakdown. With a team: "
            "by warehouse, by cost type (execution, idle, cloud services), and the users and "
            "query patterns whose spend changed most. Periods: a fiscal quarter like 'Q3 FY27', "
            "a month like '2026-08', an ISO week like '2026-W32', 'qtd', 'last_30_days', or a "
            "range 'YYYY-MM-DD:YYYY-MM-DD'."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "period": {"type": "string"},
                "team": {"type": ["string", "null"], "description": "Team name or null for all"},
            },
            "required": ["period", "team"],
            "additionalProperties": False,
        },
    },
    {
        "name": "get_forecast_vs_budget",
        "description": (
            "Budget against actual and projected spend for a fiscal quarter such as 'Q3 FY27'. "
            "For the current quarter: quarter to date actual, forecast for the rest of the "
            "quarter, projected quarter end with a range, variance to budget, and the model "
            "used per team. For a closed quarter: actual against budget."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"quarter": {"type": "string"}},
            "required": ["quarter"],
            "additionalProperties": False,
        },
    },
    {
        "name": "list_anomalies",
        "description": (
            "Detected spend anomalies that overlap a period, at or above a severity "
            "('low', 'medium', 'high'). Same period formats as get_spend_summary, or 'all'."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "period": {"type": "string"},
                "min_severity": {"type": "string", "enum": ["low", "medium", "high"]},
            },
            "required": ["period", "min_severity"],
            "additionalProperties": False,
        },
    },
    {
        "name": "explain_anomaly",
        "description": (
            "Root cause of one anomaly by id (for example 'AN008'): the gap between actual and "
            "expected spend split by warehouse, cost type, query pattern, and user, with each "
            "driver's share of the gap, plus any owner annotation."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"anomaly_id": {"type": "string"}},
            "required": ["anomaly_id"],
            "additionalProperties": False,
        },
    },
    {
        "name": "list_recommendations",
        "description": (
            "Optimization recommendations with expected monthly dollar savings at or above "
            "min_savings, each with a conservative and expected estimate, the exact action, "
            "confidence, and evidence. Upsize recommendations are costs and are always included."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"min_savings": {"type": "number"}},
            "required": ["min_savings"],
            "additionalProperties": False,
        },
    },
    {
        "name": "get_metric_definition",
        "description": "The governed definition of a metric: formula, grain, source, caveats.",
        "input_schema": {
            "type": "object",
            "properties": {"name": {"type": "string"}},
            "required": ["name"],
            "additionalProperties": False,
        },
    },
]


def _usd(x) -> float:
    return float(round(float(x)))


def _pct(x) -> float | None:
    return None if x is None or pd.isna(x) else float(round(float(x) * 100, 1))


def _cr(x) -> float:
    return float(round(float(x), 1))


class Tools:
    def __init__(self, target: Target, cfg: Config):
        self.target = target
        self.cfg = cfg
        self.price = cfg.dollars_per_credit
        self.rate = float(cfg.raw["data"]["query_sample_rate"])
        self.as_of = cfg.as_of

    # ------------------------------------------------------------ plumbing

    def df(self, sql: str) -> pd.DataFrame:
        if self.target.name == "duckdb":
            out = self.target.con.execute(sql).df()
        else:
            cur = self.target.con.cursor().execute(sql)
            out = pd.DataFrame(cur.fetchall(), columns=[c[0] for c in cur.description])
        out.columns = [c.lower() for c in out.columns]
        return out

    def assumptions(self) -> dict:
        return {
            "dollars_per_credit": self.price,
            "note": "Dollars use an assumed list price per credit. Data is synthetic.",
        }

    def period(self, p: str) -> tuple[date, date, str]:
        p = p.strip()
        a = self.as_of
        if p.lower() in ("qtd", "quarter_to_date"):
            q = self.cfg.quarter_of(a)
            return q[1], a, f"{q[0]} to date"
        if p.lower() in ("last_30_days", "last 30 days"):
            return a - timedelta(days=29), a, "last 30 days"
        if m := re.fullmatch(r"Q([1-4])\s*FY(\d{2})", p, re.I):
            q = self.cfg.fiscal_quarters(int(m.group(2)))[int(m.group(1)) - 1]
            return q[1], min(q[2], a), q[0] + (" to date" if q[2] > a else "")
        if m := re.fullmatch(r"(\d{4})-(\d{2})", p):
            start = date(int(m.group(1)), int(m.group(2)), 1)
            end = (pd.Timestamp(start) + pd.offsets.MonthEnd(0)).date()
            return start, min(end, a), p
        if m := re.fullmatch(r"(\d{4})-W(\d{1,2})", p, re.I):
            start = date.fromisocalendar(int(m.group(1)), int(m.group(2)), 1)
            return start, min(start + timedelta(days=6), a), p.upper()
        if m := re.fullmatch(r"(\d{4}-\d{2}-\d{2})\s*:\s*(\d{4}-\d{2}-\d{2})", p):
            return date.fromisoformat(m.group(1)), date.fromisoformat(m.group(2)), p
        if p.lower() == "all":
            return self.cfg.history_start, a, "all history"
        raise ValueError(
            f"Unrecognized period '{p}'. Use 'Q3 FY27', '2026-08', '2026-W32', "
            "'qtd', 'last_30_days', 'all', or 'YYYY-MM-DD:YYYY-MM-DD'."
        )

    def call(self, name: str, args: dict) -> dict:
        fn = getattr(self, name, None)
        if name not in {t["name"] for t in TOOL_SPECS} or fn is None:
            return {"error": f"Unknown tool {name}"}
        try:
            return fn(**args)
        except (ValueError, KeyError) as e:
            return {"error": str(e)}

    # ------------------------------------------------------------ tools

    def get_spend_summary(self, period: str, team: str | None = None) -> dict:
        start, end, label = self.period(period)
        days = (end - start).days + 1
        p_end = start - timedelta(days=1)
        p_start = p_end - timedelta(days=days - 1)
        team_filter = f"and team = '{team}'" if team else ""

        def total(a, b):
            r = self.df(f"""select coalesce(sum(credits), 0) as c from mart.daily_team_spend
                            where usage_date between date '{a}' and date '{b}' {team_filter}""")
            return float(r.c[0])

        cur, prev = total(start, end), total(p_start, p_end)
        out = {
            "period": label,
            "start": str(start),
            "end": str(end),
            "days": days,
            "team": team or "All teams",
            "credits": _cr(cur),
            "dollars": _usd(cur * self.price),
            "avg_daily_dollars": _usd(cur * self.price / days),
            "prior_period": {
                "start": str(p_start),
                "end": str(p_end),
                "dollars": _usd(prev * self.price),
            },
            "change_dollars": _usd((cur - prev) * self.price),
            "change_pct": _pct((cur - prev) / prev) if prev else None,
            "assumptions": self.assumptions(),
        }

        def breakdown(dim, table, value, extra=""):
            q = self.df(f"""
                select {dim} as k,
                       sum(case when usage_date between date '{start}' and date '{end}'
                           then {value} else 0 end) as cur,
                       sum(case when usage_date between date '{p_start}' and date '{p_end}'
                           then {value} else 0 end) as prev
                from {table}
                where usage_date between date '{p_start}' and date '{end}' {team_filter} {extra}
                group by {dim}""")
            q["chg"] = q.cur - q.prev
            return q

        if not team:
            b = breakdown("team", "mart.daily_team_spend", "credits")
            out["by_team"] = [
                {
                    "team": r.k,
                    "dollars": _usd(r.cur * self.price),
                    "change_dollars": _usd(r.chg * self.price),
                    "change_pct": _pct(r.chg / r.prev) if r.prev else None,
                }
                for r in b.sort_values("cur", ascending=False).itertuples()
            ]
            return out

        b = breakdown("warehouse_name", "mart.daily_warehouse_efficiency", "credits_used")
        out["by_warehouse"] = [
            {
                "warehouse": r.k,
                "dollars": _usd(r.cur * self.price),
                "change_dollars": _usd(r.chg * self.price),
                "change_pct": _pct(r.chg / r.prev) if r.prev else None,
            }
            for r in b.sort_values("chg", ascending=False).itertuples()
        ]
        ct = self.df(f"""
            select sum(case when usage_date between date '{start}' and date '{end}'
                        then attributed_credits else 0 end) as exec_cur,
                   sum(case when usage_date between date '{start}' and date '{end}'
                        then idle_credits else 0 end) as idle_cur,
                   sum(case when usage_date between date '{start}' and date '{end}'
                        then cloud_services_credits else 0 end) as cs_cur,
                   sum(case when usage_date < date '{start}' then attributed_credits else 0 end)
                       as exec_prev,
                   sum(case when usage_date < date '{start}' then idle_credits else 0 end)
                       as idle_prev,
                   sum(case when usage_date < date '{start}' then cloud_services_credits
                       else 0 end) as cs_prev
            from mart.daily_warehouse_efficiency
            where usage_date between date '{p_start}' and date '{end}' {team_filter}""").iloc[0]
        out["by_cost_type"] = [
            {
                "cost_type": n,
                "dollars": _usd(ct[f"{k}_cur"] * self.price),
                "change_dollars": _usd((ct[f"{k}_cur"] - ct[f"{k}_prev"]) * self.price),
            }
            for n, k in (("execution", "exec"), ("idle", "idle"), ("cloud_services", "cs"))
        ]
        for dim, key in (
            ("user_name", "top_user_changes"),
            ("query_parameterized_hash", "top_query_pattern_changes"),
        ):
            q = self.df(f"""
                select {dim} as k, max(warehouse_name) as wh, min_by(query_text, start_time) as ex,
                       sum(case when usage_date >= date '{start}' then attributed_compute_credits
                           + credits_used_cloud_services / {self.rate} else 0 end) as cur,
                       sum(case when usage_date < date '{start}' then attributed_compute_credits
                           + credits_used_cloud_services / {self.rate} else 0 end) as prev,
                       sum(case when usage_date >= date '{start}' then 1 else 0 end) / {self.rate}
                           as q_cur,
                       sum(case when usage_date < date '{start}' then 1 else 0 end) / {self.rate}
                           as q_prev
                from core.fct_query
                where usage_date between date '{p_start}' and date '{end}' {team_filter}
                group by {dim}""")
            q["chg"] = q.cur - q.prev
            rows = []
            for r in q.sort_values("chg", ascending=False).head(5).itertuples():
                row = {
                    "warehouse": r.wh,
                    "query_dollars": _usd(r.cur * self.price),
                    "change_dollars": _usd(r.chg * self.price),
                    "est_queries": int(round(r.q_cur)),
                    "est_queries_prior": int(round(r.q_prev)),
                }
                row = {"user": r.k, **row} if dim == "user_name" else {"pattern": r.ex[:100], **row}
                rows.append(row)
            out[key] = rows
        out["note"] = (
            "Query and user dollars are execution plus cloud services only. Idle "
            "time belongs to the warehouse."
        )
        return out

    def get_forecast_vs_budget(self, quarter: str) -> dict:
        m = re.fullmatch(r"Q([1-4])\s*FY(\d{2})", quarter.strip(), re.I)
        if not m:
            raise ValueError("Use a fiscal quarter like 'Q3 FY27'.")
        label, first, last, _ = self.cfg.fiscal_quarters(int(m.group(2)))[int(m.group(1)) - 1]
        current = self.cfg.quarter_of(self.as_of)[0]
        if label == current:
            f = self.df("select * from mart.forecast_vs_budget")
            rows = [
                {
                    "team": r.team,
                    "model": r.model,
                    "budget_dollars": _usd(r.budget_dollars),
                    "qtd_dollars": _usd(r.qtd_dollars),
                    "projected_dollars": _usd(r.projected_dollars),
                    "projected_low_dollars": _usd(r.projected_low_dollars),
                    "projected_high_dollars": _usd(r.projected_high_dollars),
                    "variance_dollars": _usd(r.variance_dollars),
                    "variance_pct": _pct(r.variance_pct),
                }
                for r in f.sort_values("variance_dollars", ascending=False).itertuples()
            ]
            return {
                "quarter": label,
                "as_of": str(self.as_of),
                "status": "in progress",
                "days_elapsed": (self.as_of - first).days + 1,
                "days_in_quarter": (last - first).days + 1,
                "teams": rows,
                "range_note": "The projected range adds daily 80 percent bounds, so it is a "
                "conservative band.",
                "assumptions": self.assumptions(),
            }
        if first > self.as_of:
            raise ValueError(f"{label} has not started as of {self.as_of}.")
        b = self.df(
            f"select team, budget_dollars from mart.budget_quarterly "
            f"where fiscal_quarter = '{label}'"
        )
        a = self.df(f"""select team, sum(credits) as c from mart.daily_team_spend
                        where usage_date between date '{first}' and date '{last}' group by team""")
        a = pd.concat([a, pd.DataFrame({"team": ["All teams"], "c": [a.c.sum()]})])
        j = b.merge(a, on="team", how="left")
        rows = [
            {
                "team": r.team,
                "budget_dollars": _usd(r.budget_dollars),
                "actual_dollars": _usd(r.c * self.price),
                "variance_dollars": _usd(r.c * self.price - r.budget_dollars),
                "variance_pct": _pct((r.c * self.price - r.budget_dollars) / r.budget_dollars),
            }
            for r in j.itertuples()
        ]
        if not len(rows):
            raise ValueError(f"No budget exists for {label}. Budgets cover FY27 only.")
        return {
            "quarter": label,
            "status": "closed",
            "teams": rows,
            "assumptions": self.assumptions(),
        }

    def list_anomalies(self, period: str, min_severity: str = "low") -> dict:
        start, end, label = self.period(period)
        a = self.df(f"""select * from mart.anomalies
                        where start_date <= date '{end}' and end_date >= date '{start}'
                        order by start_date""")
        a = a[a.severity.map(SEVERITY_RANK) >= SEVERITY_RANK[min_severity]]
        return {
            "period": label,
            "min_severity": min_severity,
            "count": len(a),
            "anomalies": [
                {
                    "anomaly_id": r.anomaly_id,
                    "team": r.team,
                    "start_date": str(r.start_date)[:10],
                    "end_date": str(r.end_date)[:10],
                    "flagged_days": int(r.flagged_days),
                    "excess_dollars": _usd(r.excess_dollars),
                    "kind": r.kind,
                    "status": r.status,
                    "severity": r.severity,
                    "top_warehouse": r.top_warehouse,
                    "top_warehouse_share_pct": _pct(r.top_warehouse_share),
                }
                for r in a.itertuples()
            ],
            "assumptions": self.assumptions(),
        }

    def explain_anomaly(self, anomaly_id: str) -> dict:
        a = self.df(f"select * from mart.anomalies where anomaly_id = '{anomaly_id}'")
        if not len(a):
            raise ValueError(f"No anomaly {anomaly_id}.")
        r = a.iloc[0]
        att = self.df(f"""select * from mart.anomaly_attribution
                          where anomaly_id = '{anomaly_id}' and rank <= 3
                          order by dimension, rank""")
        drivers = {}
        for dim, g in att.groupby("dimension"):
            drivers[dim] = [
                {
                    "rank": int(x.rank),
                    "driver": x.label if dim == "query_pattern" else x.key,
                    "contribution_dollars": _usd(x.contribution_dollars),
                    "share_of_gap_pct": _pct(x.share_of_gap),
                    **(
                        {"change_in_est_queries": int(round(x.delta_est_queries))}
                        if x.delta_est_queries is not None and not pd.isna(x.delta_est_queries)
                        else {}
                    ),
                }
                for x in g.itertuples()
            ]
        ann = self.df(f"""select * from app.annotations where team = '{r.team}'
                          and start_date between date '{str(r.start_date)[:10]}' - interval '3 days'
                                             and date '{str(r.end_date)[:10]}'""")
        return {
            "anomaly_id": anomaly_id,
            "team": r.team,
            "start_date": str(r.start_date)[:10],
            "end_date": str(r.end_date)[:10],
            "kind": r.kind,
            "status": r.status,
            "excess_dollars": _usd(r.excess_dollars),
            "flagged_days": int(r.flagged_days),
            "drivers": drivers,
            "owner_annotation": None
            if not len(ann)
            else {
                "owner": ann.owner.iloc[0],
                "note": ann.note.iloc[0],
                "confirmed_on": str(ann.confirmed_on.iloc[0])[:10],
            },
            "method": "Gap = actual minus expected on flagged days. Expected uses the same "
            "weekday reference days for every component, so the parts add up.",
            "assumptions": self.assumptions(),
        }

    def list_recommendations(self, min_savings: float = 0) -> dict:
        r = self.df("select * from mart.recommendations order by monthly_dollars_expected desc")
        r = r[(r.rule == "upsize") | (r.monthly_dollars_expected >= min_savings)]
        return {
            "min_savings": min_savings,
            "count": len(r),
            "recommendations": [
                {
                    "rec_id": x.rec_id,
                    "rule": x.rule,
                    "warehouse": x.warehouse_name,
                    "team": x.team,
                    "title": x.title,
                    "action": x.action,
                    "confidence": x.confidence,
                    "monthly_dollars_conservative": _usd(x.monthly_dollars_conservative),
                    "monthly_dollars_expected": _usd(x.monthly_dollars_expected),
                    "method": x.method,
                    "evidence": {
                        k: (round(v, 3) if isinstance(v, float) else v)
                        for k, v in json.loads(x.evidence).items()
                    },
                }
                for x in r.itertuples()
            ],
            "note": "Upsize rows are a cost: conservative is the worst case monthly increase.",
            "assumptions": self.assumptions(),
        }

    def get_metric_definition(self, name: str) -> dict:
        spec = yaml.safe_load((ROOT / "metrics.yaml").read_text(encoding="utf-8"))["metrics"]
        key = name.strip().lower().replace(" ", "_")
        if key not in spec:
            hits = [k for k, m in spec.items() if key in k or name.lower() in m["label"].lower()]
            if not hits:
                raise ValueError(f"No metric '{name}'. Known: {', '.join(spec)}")
            key = hits[0]
        return {"metric": key, **spec[key]}
