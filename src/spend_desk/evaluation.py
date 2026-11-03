"""`make eval`: rebuild everything deterministic, then collect every README number in one place.

The agent eval calls a paid API, so it is not rerun here. Its last results are read from
results/agent_eval.json (refresh with `python -m spend_desk agent-eval`).
"""

from __future__ import annotations

import json
import subprocess
import sys

from spend_desk import anomaly, forecast, recommend
from spend_desk.config import ROOT, load_config
from spend_desk.models import build, open_target, run_dq

SUMMARY = ROOT / "results" / "eval_summary.json"


def test_count() -> int:
    out = subprocess.run(
        [sys.executable, "-m", "pytest", "--collect-only", "-q"],
        cwd=ROOT,
        capture_output=True,
        text=True,
    ).stdout
    line = [x for x in out.splitlines() if "tests collected" in x or "test collected" in x]
    return int(line[-1].split()[0]) if line else 0


def main() -> int:
    cfg = load_config()
    target = open_target("duckdb")
    try:
        build(target, cfg, verbose=False)
        dq = run_dq(target)
        scores = forecast.run(target, cfg)
        anomaly.run(target, cfg)
        recs = recommend.run(target, cfg)
        ev = target.con.execute("select * from mart.anomaly_eval").df()
    finally:
        target.close()

    sel = scores[scores.selected]
    base = scores[scores.model == "seasonal_naive"].set_index("team")
    fc = {
        r.team: {
            "model": r.model,
            "mape": round(r.mape, 4),
            "baseline_mape": round(base.loc[r.team, "mape"], 4),
            "coverage_80": round(r.coverage, 3),
        }
        for r in sel.itertuples()
    }
    teams = [t for t in fc if t != "All teams"]
    anom = {
        f"{r.method}_{r.eval_window}": {
            "precision": round(r.precision, 3),
            "recall": round(r.recall, 3),
            "detected": int(r.detected),
            "truths": int(r.truths),
            "false_positive_episodes": int(r.false_positive_episodes),
            "median_days_to_detect": r.median_days_to_detect,
            "threshold": r.threshold,
            "min_dollars": r.min_dollars,
        }
        for r in ev.itertuples()
    }
    agent_path = ROOT / "results" / "agent_eval.json"
    agent = json.loads(agent_path.read_text())["summary"] if agent_path.exists() else None
    summary = {
        "as_of": str(cfg.as_of),
        "data_quality_checks": {"total": len(dq), "passing": sum(v == 0 for v in dq.values())},
        "forecast": {
            "total": fc["All teams"],
            "teams_beating_baseline": sum(fc[t]["mape"] < fc[t]["baseline_mape"] for t in teams),
            "teams": len(teams),
            "mean_team_mape": round(sum(fc[t]["mape"] for t in teams) / len(teams), 4),
            "mean_team_baseline_mape": round(
                sum(fc[t]["baseline_mape"] for t in teams) / len(teams), 4
            ),
            "by_team": fc,
        },
        "anomaly": anom,
        "recommender": recs["metrics"],
        "agent": agent,
        "tests": test_count(),
    }
    SUMMARY.write_text(json.dumps(summary, indent=2, default=float), encoding="utf-8")
    print(json.dumps(summary, indent=2, default=float))
    return 0
