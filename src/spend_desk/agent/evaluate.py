"""Agent eval: 15 questions whose expected answers come from ground truth and the MART tables
at run time. An answer is correct when it names every required fact (any listed spelling
counts) and states each expected number at its displayed precision. Provenance is scored
separately: did the final answer pass the guard, and did the first draft pass.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field

import pandas as pd

from spend_desk.agent.agent import MEMO_TASK, AnthropicAgent, render_memo
from spend_desk.agent.provenance import _matches, extract_numbers
from spend_desk.agent.tools import Tools
from spend_desk.config import ROOT, load_config
from spend_desk.forecast import write_table
from spend_desk.models import open_target

RESULTS = ROOT / "results" / "agent_eval.json"
MEMO_PATH = ROOT / "docs" / "sample_qbr_memo.md"


@dataclass
class Question:
    qid: str
    text: str
    must_mention: list[list[str]] = field(default_factory=list)  # every group, any spelling
    numbers: list[float] = field(default_factory=list)
    source: str = ""


def build_questions(tools: Tools) -> list[Question]:
    gt = tools.df("select * from raw.ground_truth").set_index("truth_id")
    fvb = {
        r["team"]: r
        for r in tools.get_forecast_vs_budget(tools.cfg.quarter_of(tools.as_of)[0])["teams"]
    }
    recs = tools.list_recommendations(0)["recommendations"]
    aug = tools.get_spend_summary("2026-08", None)

    def truth(tid, *extra):
        t = gt.loc[tid]
        groups = [[t.warehouse_name]]
        if extra:
            groups.append(list(extra))
        return groups, f"ground truth {tid} ({t.truth_type})"

    q = []
    g, s = truth("A08", "retry", "SVC_MLFLOW", "RECS_TRAIN", "loop")
    q.append(Question("Q01", "Why did ML Training spend spike in ISO week 32 of 2026?", g, [], s))
    g, s = truth("A04", "X-Large", "XLARGE", "upsiz", "resiz", "backfill")
    q.append(
        Question("Q02", "What caused the Data Platform spend increase in December 2025?", g, [], s)
    )
    g, s = truth("A05", "cloud services", "cloud_services")
    q.append(
        Question(
            "Q03",
            "Which warehouse drove the Product Analytics anomaly in late January "
            "2026, and was the extra cost query execution, idle time, or cloud "
            "services?",
            g,
            [],
            s,
        )
    )
    g, s = truth("A02", "idle", "heartbeat", "SVC_HIGHTOUCH", "never suspended", "weekend")
    q.append(
        Question(
            "Q04", "What happened to GTM Ops spend over the weekend of November 8, 2025?", g, [], s
        )
    )
    t = gt.loc["A07"]
    q.append(
        Question(
            "Q05",
            "Who caused the Customer Data spike in late February 2026, and on which warehouse?",
            [[t.user_name], [t.warehouse_name]],
            [],
            "ground truth A07 (runaway_query_loop)",
        )
    )
    q.append(
        Question(
            "Q06",
            "How far over budget will ML Training finish Q3 FY27, in dollars?",
            [["ML Training"]],
            [fvb["ML Training"]["variance_dollars"]],
            "mart.forecast_vs_budget",
        )
    )
    under = min(
        (r for r in fvb.values() if r["team"] != "All teams"), key=lambda r: r["variance_dollars"]
    )
    q.append(
        Question(
            "Q07",
            "Which team is furthest under budget for Q3 FY27?",
            [[under["team"]]],
            [],
            "mart.forecast_vs_budget",
        )
    )
    top = max(
        (r for r in recs if r["rule"] != "upsize"), key=lambda r: r["monthly_dollars_expected"]
    )
    q.append(
        Question(
            "Q08",
            "What is the single largest savings opportunity, and what is it worth per month?",
            [[top["warehouse"]]],
            [top["monthly_dollars_expected"]],
            "mart.recommendations",
        )
    )
    idle = gt[gt.truth_type == "idle_long_autosuspend"].warehouse_name.tolist()
    q.append(
        Question(
            "Q09",
            "Which warehouses have AUTO_SUSPEND set too long, and what setting do you recommend?",
            [[w] for w in idle] + [["60"]],
            [],
            "ground truth W01, W02",
        )
    )
    unused = gt[gt.truth_type == "unused_scheduled_output"]
    q.append(
        Question(
            "Q10",
            "Is any scheduled job producing a table that nobody reads?",
            [["LEGACY_GL_DAILY"], ["CRM_ACCOUNTS_SNAP"]],
            [],
            "ground truth " + ", ".join(unused.index),
        )
    )
    small = gt[gt.truth_type == "undersized_queue_spill"].warehouse_name.tolist()
    q.append(
        Question(
            "Q11",
            "Which warehouses are undersized, and what would upsizing them cost in the worst case?",
            [[w] for w in small],
            [],
            "ground truth W05, W06",
        )
    )
    q.append(
        Question(
            "Q12",
            "Was the jump in Customer Data spend in June 2026 expected, and "
            "should it still be treated as an anomaly?",
            [["expected", "planned", "launch", "confirmed", "accepted"], ["baseline"]],
            [],
            "ground truth A13 (legit_step_change)",
        )
    )
    g, s = truth("A11", "PREYES", "backfill", "restatement", "GL_ENTRIES")
    q.append(
        Question("Q13", "What caused the Finance BI spike around June 8 to 10, 2026?", g, [], s)
    )
    q.append(
        Question(
            "Q14",
            "How is idle credits defined, and what are its caveats?",
            [
                ["CREDITS_ATTRIBUTED_COMPUTE_QUERIES", "attributed"],
                ["60 second", "60-second", "60 seconds", "60s"],
            ],
            [],
            "metrics.yaml",
        )
    )
    q.append(
        Question(
            "Q15",
            "What did the company spend in August 2026, and how did that compare with July?",
            [["August"]],
            [aug["dollars"]],
            "mart.daily_team_spend",
        )
    )
    return q


def grade(q: Question, answer: str) -> dict:
    low = answer.lower()
    missing = [grp for grp in q.must_mention if not any(s.lower() in low for s in grp)]
    found = extract_numbers(answer)
    missing_nums = [n for n in q.numbers if not any(_matches(f, [n]) for f in found)]
    return {
        "correct": not missing and not missing_nums,
        "missing_mentions": missing,
        "missing_numbers": missing_nums,
    }


def run_eval(target_name: str = "duckdb", memo: bool = True) -> dict:
    cfg = load_config()
    target = open_target(target_name)
    try:
        tools = Tools(target, cfg)
        agent = AnthropicAgent(tools)
        rows, cost_in, cost_out = [], 0, 0
        for q in build_questions(tools):
            r = agent.run(q.text, kind="eval")
            g = grade(q, r.text)
            cost_in += r.input_tokens
            cost_out += r.output_tokens
            rows.append(
                {
                    "qid": q.qid,
                    "question": q.text,
                    "expected_source": q.source,
                    "correct": g["correct"],
                    "missing": json.dumps(g),
                    "provenance_passed": r.passed,
                    "first_draft_passed": r.first_draft_passed,
                    "drafts": len(r.guard_results),
                    "tool_calls": len(r.calls),
                    "numbers_checked": r.guard_results[-1].numbers_checked
                    if r.guard_results
                    else 0,
                    "answer": r.text,
                }
            )
            print(
                f"{q.qid} correct={g['correct']} provenance={r.passed} "
                f"drafts={len(r.guard_results)} calls={len(r.calls)}"
            )
        df = pd.DataFrame(rows)
        summary = {
            "questions": len(df),
            "answer_accuracy": float(df.correct.mean()),
            "provenance_pass_rate": float(df.provenance_passed.mean()),
            "first_draft_provenance_pass_rate": float(df.first_draft_passed.mean()),
            "numbers_checked": int(df.numbers_checked.sum()),
            "model": agent.model,
            "input_tokens": cost_in,
            "output_tokens": cost_out,
        }
        out = {"summary": summary, "questions": rows}
        if memo:
            m = agent.run(MEMO_TASK, kind="memo")
            MEMO_PATH.parent.mkdir(exist_ok=True)
            MEMO_PATH.write_text(render_memo(m, cfg), encoding="utf-8")
            out["memo"] = {
                "passed": m.passed,
                "drafts": len(m.guard_results),
                "numbers_checked": m.guard_results[-1].numbers_checked,
                "tool_calls": len(m.calls),
            }
            memo_row = pd.DataFrame(
                [
                    {
                        "run_id": m.run_id,
                        "as_of": str(cfg.as_of),
                        "model": m.model,
                        "provider": m.provider,
                        "passed": m.passed,
                        "drafts": len(m.guard_results),
                        "markdown": render_memo(m, cfg),
                        "calls": json.dumps(m.calls, default=str),
                    }
                ]
            )
            write_table(target, "app.qbr_memos", memo_row)
            summary["input_tokens"] += m.input_tokens
            summary["output_tokens"] += m.output_tokens
        summary["est_cost_usd"] = round(
            summary["input_tokens"] * 4e-6 + summary["output_tokens"] * 20e-6, 2
        )
        RESULTS.write_text(json.dumps(out, indent=2, default=str), encoding="utf-8")
        write_table(target, "mart.agent_eval", df)
        log = pd.read_json(ROOT / "results" / "provenance_log.jsonl", lines=True)
        log = log.assign(
            **{
                c: log[c].map(json.dumps)
                for c in ("unmatched", "uncited_lines", "style_violations")
            }
        )
        write_table(target, "app.provenance_log", log)
        return out
    finally:
        target.close()
