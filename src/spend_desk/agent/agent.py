"""Spend Desk agent: plans, calls tools over several steps, drafts, and must pass the provenance
guard before anything is shown. Two providers behind one interface:

  anthropic  Claude through the Anthropic API, with native tool use. Used for local development.
  cortex     Claude through Snowflake Cortex AI_COMPLETE, for use inside Snowflake. Cortex runs
             a fixed tool plan in code and asks the model to write from those outputs.
"""

from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass, field

from spend_desk.agent.provenance import GuardResult, check
from spend_desk.agent.tools import TOOL_SPECS, Tools
from spend_desk.config import ROOT

MODEL = "claude-opus-5-5"
MAX_TOOL_TURNS = 14
MAX_DRAFTS = 3
LOG_PATH = ROOT / "results" / "provenance_log.jsonl"

SYSTEM = """You are Spend Desk, a cloud spend analyst for a Finance BI team. You answer questions \
and write QBR material about Snowflake spend for Engineering and Finance leaders.

How you work:
1. Plan which tools you need, then call them. You can only see data through the tools. Call \
several tools when a question needs them, and call more if the first results raise questions.
2. Every number you write must be copied from a tool output exactly as the tool returned it, \
or rounded from it in the obvious way (for example 22589 written as $22,589 or $22.6k). Never \
add, subtract, multiply, divide, average, or convert numbers yourself. If a figure you want is \
not in a tool output, say what the tools do show instead.
3. Each tool result carries a citation_id such as T3. End every sentence or bullet that \
contains a number with the citation ids of the tool calls the numbers came from, like [T3]. A \
line with a number and no citation will be rejected.
4. Dollar figures use an assumed price per credit and the data is synthetic. Say so once.
5. Writing style: plain, direct sentences for busy executives. Never use em dashes, en dashes, \
or hyphens as sentence breaks or connectors. Use full stops or commas instead.

An automated checker verifies every number against the cited tool outputs. If it rejects a \
draft, fix exactly what it lists and return the full corrected answer."""

MEMO_TASK = """Write a one page QBR memo on cloud spend for Engineering and Finance leaders, \
for the current fiscal quarter, using the tools. Use these sections, in this order, as \
markdown headings:

## Headline
Two or three sentences: projected quarter end spend against budget for the company, and the \
main reason.
## Quarter to date and projection
Quarter to date spend against budget, projected quarter end with its range, and the teams \
furthest over and under budget.
## Top 3 anomalies this quarter
The three largest anomalies by dollars that overlap the current quarter or the 60 days before \
it, each with its root cause from explain_anomaly (warehouse, cost type, user or query \
pattern) and its status.
## Top 5 savings opportunities
The five largest by expected monthly savings, each with the exact action and the \
conservative to expected range. Mention any upsize separately as a cost with its benefit.
## Risks and asks
Three to five bullets: risks to the forecast and concrete asks of named teams.

Keep it under 600 words."""


@dataclass
class AgentResult:
    run_id: str
    task: str
    text: str
    calls: dict
    guard_results: list[GuardResult]
    passed: bool
    first_draft_passed: bool
    provider: str
    model: str
    input_tokens: int = 0
    output_tokens: int = 0
    seconds: float = 0.0
    events: list = field(default_factory=list)


def _log(run_id: str, attempt: int, kind: str, g: GuardResult) -> None:
    LOG_PATH.parent.mkdir(exist_ok=True)
    with open(LOG_PATH, "a", encoding="utf-8") as f:
        f.write(
            json.dumps(
                {
                    "run_id": run_id,
                    "attempt": attempt,
                    "kind": kind,
                    "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
                    **g.to_dict(),
                }
            )
            + "\n"
        )


def _tool_defs() -> list[dict]:
    return [{**t, "strict": True} for t in TOOL_SPECS]


class AnthropicAgent:
    provider = "anthropic"

    def __init__(self, tools: Tools, model: str = MODEL, effort: str = "medium"):
        import anthropic
        from dotenv import load_dotenv

        load_dotenv(ROOT / ".env", override=False)
        self.client = anthropic.Anthropic()
        self.tools = tools
        self.model = model
        self.effort = effort

    def _create(self, messages):
        return self.client.beta.messages.create(
            model=self.model,
            max_tokens=16000,
            system=SYSTEM,
            messages=messages,
            tools=_tool_defs(),
            output_config={"effort": self.effort},
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
        )

    def run(self, task: str, kind: str = "ask") -> AgentResult:
        run_id = uuid.uuid4().hex[:12]
        t0 = time.time()
        messages = [{"role": "user", "content": task}]
        calls: dict[str, dict] = {}
        guards: list[GuardResult] = []
        tin = tout = 0
        text = ""
        for _ in range(MAX_TOOL_TURNS + MAX_DRAFTS):
            resp = self._create(messages)
            tin += resp.usage.input_tokens
            tout += resp.usage.output_tokens
            if resp.stop_reason == "refusal":
                raise RuntimeError(f"Model declined: {resp.stop_details}")
            messages.append({"role": "assistant", "content": resp.content})
            uses = [b for b in resp.content if b.type == "tool_use"]
            if uses:
                results = []
                for b in uses:
                    cid = f"T{len(calls) + 1}"
                    out = self.tools.call(b.name, dict(b.input))
                    calls[cid] = {"name": b.name, "input": dict(b.input), "output": out}
                    results.append(
                        {
                            "type": "tool_result",
                            "tool_use_id": b.id,
                            "content": json.dumps({"citation_id": cid, **out}, default=str),
                        }
                    )
                messages.append({"role": "user", "content": results})
                continue
            text = "".join(b.text for b in resp.content if b.type == "text").strip()
            g = check(text, {k: v["output"] for k, v in calls.items()})
            guards.append(g)
            _log(run_id, len(guards), kind, g)
            if (g.passed and not g.style_violations) or len(guards) >= MAX_DRAFTS:
                break
            messages.append(
                {
                    "role": "user",
                    "content": "The checker rejected this draft. "
                    + g.feedback()
                    + " Return the full corrected answer.",
                }
            )
        return AgentResult(
            run_id,
            task,
            text,
            calls,
            guards,
            bool(guards) and guards[-1].passed,
            bool(guards) and guards[0].passed,
            self.provider,
            self.model,
            tin,
            tout,
            time.time() - t0,
        )


class CortexAgent:
    """Inside Snowflake: a fixed tool plan in code, one AI_COMPLETE call to write, same guard."""

    provider = "cortex"
    plan = [
        ("get_forecast_vs_budget", lambda t: {"quarter": t.cfg.quarter_of(t.as_of)[0]}),
        ("get_spend_summary", lambda t: {"period": "qtd", "team": None}),
        ("list_anomalies", lambda t: {"period": "last_30_days", "min_severity": "medium"}),
        ("list_recommendations", lambda t: {"min_savings": 100}),
    ]

    def __init__(self, tools: Tools, session, model: str = MODEL):
        self.tools, self.session, self.model = tools, session, model

    def _complete(self, prompt: str) -> str:
        row = self.session.sql(
            "select ai_complete(?, ?) as r", params=[self.model, prompt]
        ).collect()[0]
        return str(row["R"])

    def run(self, task: str, kind: str = "memo") -> AgentResult:
        run_id = uuid.uuid4().hex[:12]
        t0 = time.time()
        calls = {}
        for name, args in self.plan:
            calls[f"T{len(calls) + 1}"] = {
                "name": name,
                "input": args(self.tools),
                "output": self.tools.call(name, args(self.tools)),
            }
        for a in calls["T3"]["output"].get("anomalies", [])[:3]:
            calls[f"T{len(calls) + 1}"] = {
                "name": "explain_anomaly",
                "input": {"anomaly_id": a["anomaly_id"]},
                "output": self.tools.call("explain_anomaly", {"anomaly_id": a["anomaly_id"]}),
            }
        context = "\n".join(
            json.dumps({"citation_id": k, "tool": v["name"], **v["output"]}, default=str)
            for k, v in calls.items()
        )
        prompt = f"{SYSTEM}\n\nTool outputs:\n{context}\n\nTask:\n{task}"
        guards, text = [], ""
        for _ in range(MAX_DRAFTS):
            text = self._complete(prompt).strip()
            g = check(text, {k: v["output"] for k, v in calls.items()})
            guards.append(g)
            _log(run_id, len(guards), kind, g)
            if g.passed and not g.style_violations:
                break
            prompt += f"\n\nYour previous draft:\n{text}\n\nThe checker rejected it. {g.feedback()}"
        return AgentResult(
            run_id,
            task,
            text,
            calls,
            guards,
            guards[-1].passed,
            guards[0].passed,
            self.provider,
            self.model,
            0,
            0,
            time.time() - t0,
        )


def render_memo(result: AgentResult, cfg) -> str:
    body = result.text
    if body.lstrip().startswith("# "):  # the renderer owns the title
        body = body.lstrip().split("\n", 1)[-1].lstrip()
    lines = [
        f"# Spend Desk QBR memo, {cfg.quarter_of(cfg.as_of)[0]}",
        "",
        f"*As of {cfg.as_of}. Synthetic data shaped like Snowflake ACCOUNT_USAGE. Dollars assume "
        f"${cfg.dollars_per_credit:.2f} per credit. Generated by {result.model} through the "
        f"{result.provider} provider. Provenance check: "
        f"{'passed' if result.passed else 'FAILED'} after {len(result.guard_results)} "
        f"draft(s), {result.guard_results[-1].numbers_checked} numbers verified.*",
        "",
        body,
        "",
        "---",
        "",
        "## Citations",
        "",
        "| Id | Tool | Input |",
        "|---|---|---|",
    ]
    for cid, c in result.calls.items():
        lines.append(f"| {cid} | `{c['name']}` | `{json.dumps(c['input'])}` |")
    return "\n".join(lines) + "\n"
