"""Spend Desk: cloud spend copilot for Finance BI. Streamlit in Snowflake or local."""

from __future__ import annotations

import json

import altair as alt
import pandas as pd
import streamlit as st

from data import IN_SNOWFLAKE, metrics_spec, q

st.set_page_config(page_title="Spend Desk", page_icon=":bar_chart:", layout="wide")

TEAMS = [
    "Data Platform",
    "ML Training",
    "Product Analytics",
    "Finance BI",
    "GTM Ops",
    "Customer Data",
]
# Fixed categorical order, one hue per team, never re-assigned by rank or filter.
TEAM_COLORS = ["#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4", "#008300"]
TEAM_SCALE = alt.Scale(domain=TEAMS, range=TEAM_COLORS)
INK, MUTED, BAND, NEUTRAL = "#52514e", "#8a8984", "#b7d3f6", "#c3c2b7"

st.markdown(
    """
<style>
  [data-testid="stMetricValue"] { font-size: 1.6rem; }
  .sd-note { color: #8a8984; font-size: 0.85rem; }
</style>""",
    unsafe_allow_html=True,
)

assume = q("select name, value, note from mart.assumptions").set_index("name")
PRICE = float(assume.loc["dollars_per_credit", "value"])
fvb = q("select * from mart.forecast_vs_budget")
AS_OF = pd.Timestamp(fvb.as_of.iloc[0]).date()
QUARTER = fvb.fiscal_quarter.iloc[0]


def usd(x: float) -> str:
    # Escaped, because Streamlit renders two dollar signs on one line as LaTeX math.
    return f"\\${x:,.0f}"


def md_safe(text: str) -> str:
    return text.replace("$", "\\$")


def disclaimer():
    st.markdown(
        f"<div class='sd-note'>Synthetic data shaped like Snowflake ACCOUNT_USAGE views, as of "
        f"{AS_OF}. Dollars assume ${PRICE:.2f} per credit, an assumption, not a contract "
        f"price.</div>",
        unsafe_allow_html=True,
    )


# ---------------------------------------------------------------------------- pages


def overview():
    st.title("Overview")
    disclaimer()
    allt = fvb.set_index("team").loc["All teams"]
    c = st.columns(4)
    c[0].metric(f"{QUARTER} budget", usd(allt.budget_dollars))
    c[1].metric("Quarter to date", usd(allt.qtd_dollars))
    c[2].metric(
        "Projected quarter end",
        usd(allt.projected_dollars),
        f"{usd(allt.variance_dollars)} vs budget",
        delta_color="inverse",
    )
    c[3].metric(
        "Projected range",
        f"{usd(allt.projected_low_dollars)} to {usd(allt.projected_high_dollars)}",
    )

    st.subheader("Forecast vs budget by team")
    teams = fvb[fvb.team != "All teams"].copy()
    base = alt.Chart(teams).encode(y=alt.Y("team:N", sort="-x", title=None))
    bars = base.mark_bar(height=16, cornerRadiusEnd=4).encode(
        x=alt.X("projected_dollars:Q", title="Projected quarter end (dollars)"),
        color=alt.Color("team:N", scale=TEAM_SCALE, legend=None),
        tooltip=[
            alt.Tooltip("team:N"),
            alt.Tooltip("budget_dollars:Q", format="$,.0f", title="Budget"),
            alt.Tooltip("qtd_dollars:Q", format="$,.0f", title="Quarter to date"),
            alt.Tooltip("projected_dollars:Q", format="$,.0f", title="Projected"),
            alt.Tooltip("variance_dollars:Q", format="$,.0f", title="Variance"),
            alt.Tooltip("variance_pct:Q", format="+.1%", title="Variance %"),
            alt.Tooltip("model:N", title="Forecast model"),
        ],
    )
    rng = base.mark_rule(color=INK, strokeWidth=2).encode(
        x="projected_low_dollars:Q", x2="projected_high_dollars:Q"
    )
    tick = base.mark_tick(color="#0b0b0b", thickness=3, size=26).encode(x="budget_dollars:Q")
    st.altair_chart((bars + rng + tick).properties(height=260), use_container_width=True)
    st.caption(
        "Bar: projected quarter end. Black tick: budget. Line: projected range, which "
        "adds daily 80 percent bounds and is therefore a conservative band."
    )

    st.subheader("Daily spend and 90 day forecast")
    choice = st.selectbox("Team", ["All teams", *TEAMS])
    hist = q(
        "select usage_date, team, dollars from mart.daily_team_spend "
        "where usage_date >= date '2026-03-01'"
    )
    hist["usage_date"] = pd.to_datetime(hist.usage_date)
    actual = (
        hist.groupby("usage_date", as_index=False).dollars.sum()
        if choice == "All teams"
        else hist[hist.team == choice]
    )
    fc = q(
        f"select usage_date, forecast_dollars, lower_dollars, upper_dollars, model "
        f"from mart.forecast_daily where team = '{choice}'"
    )
    fc["usage_date"] = pd.to_datetime(fc.usage_date)
    band = (
        alt.Chart(fc)
        .mark_area(color=BAND, opacity=0.6)
        .encode(
            x=alt.X("usage_date:T", title=None),
            y=alt.Y("lower_dollars:Q", title="Dollars per day"),
            y2="upper_dollars:Q",
            tooltip=[
                alt.Tooltip("usage_date:T", title="Date"),
                alt.Tooltip("lower_dollars:Q", format="$,.0f", title="Low (80%)"),
                alt.Tooltip("upper_dollars:Q", format="$,.0f", title="High (80%)"),
            ],
        )
    )
    fline = (
        alt.Chart(fc)
        .mark_line(color="#2a78d6", strokeWidth=2, strokeDash=[4, 3])
        .encode(
            x="usage_date:T",
            y="forecast_dollars:Q",
            tooltip=[
                alt.Tooltip("usage_date:T", title="Date"),
                alt.Tooltip("forecast_dollars:Q", format="$,.0f", title="Forecast"),
            ],
        )
    )
    aline = (
        alt.Chart(actual)
        .mark_line(color="#0b0b0b", strokeWidth=2)
        .encode(
            x="usage_date:T",
            y="dollars:Q",
            tooltip=[
                alt.Tooltip("usage_date:T", title="Date"),
                alt.Tooltip("dollars:Q", format="$,.0f", title="Actual"),
            ],
        )
    )
    st.altair_chart((band + aline + fline).properties(height=320), use_container_width=True)
    st.caption(
        f"Black: actual. Dashed blue: forecast from the {fc.model.iloc[0]} model, chosen "
        "by backtest. Shaded: 80 percent interval."
    )


def anomalies():
    st.title("Anomalies")
    disclaimer()
    a = q("select * from mart.anomalies order by start_date")
    a["start_date"] = pd.to_datetime(a.start_date)
    a["end_date"] = pd.to_datetime(a.end_date)
    kinds = alt.Scale(domain=["spike", "level_shift"], range=["#2a78d6", "#eb6834"])
    chart = (
        alt.Chart(a)
        .mark_bar(height=14, cornerRadius=4)
        .encode(
            x=alt.X("start_date:T", title=None),
            x2="end_date:T",
            y=alt.Y("team:N", sort=TEAMS, title=None),
            color=alt.Color("kind:N", scale=kinds, title="Kind"),
            tooltip=[
                "anomaly_id:N",
                "team:N",
                alt.Tooltip("start_date:T", title="Start"),
                alt.Tooltip("end_date:T", title="End"),
                alt.Tooltip("excess_dollars:Q", format="$,.0f", title="Excess"),
                "kind:N",
                "status:N",
                "top_warehouse:N",
            ],
        )
    )
    dots = (
        alt.Chart(a)
        .mark_circle(size=90, stroke="#fcfcfb", strokeWidth=2)
        .encode(
            x="start_date:T",
            y=alt.Y("team:N", sort=TEAMS),
            color=alt.Color("kind:N", scale=kinds),
            tooltip=["anomaly_id:N"],
        )
    )
    st.altair_chart((chart + dots).properties(height=260), use_container_width=True)

    show = a.assign(
        start_date=a.start_date.dt.date,
        end_date=a.end_date.dt.date,
        excess_dollars=a.excess_dollars.round(0),
        top_warehouse_share=(a.top_warehouse_share * 100).round(1),
    )
    st.dataframe(
        show[
            [
                "anomaly_id",
                "team",
                "start_date",
                "end_date",
                "flagged_days",
                "excess_dollars",
                "kind",
                "status",
                "severity",
                "top_warehouse",
                "top_warehouse_share",
            ]
        ].iloc[::-1],
        hide_index=True,
        use_container_width=True,
    )

    st.subheader("Attribution")
    pick = st.selectbox(
        "Anomaly",
        a.anomaly_id.iloc[::-1],
        format_func=lambda i: (
            f"{i}  {a.set_index('anomaly_id').loc[i, 'team']}"
            f"  {a.set_index('anomaly_id').loc[i, 'start_date'].date()}"
        ),
    )
    att = q(f"select * from mart.anomaly_attribution where anomaly_id = '{pick}' and rank <= 5")
    r = a.set_index("anomaly_id").loc[pick]
    st.markdown(
        f"**{pick}**: {r.team}, {r.start_date.date()} to {r.end_date.date()}, "
        f"{usd(r.excess_dollars)} above expected over {int(r.flagged_days)} flagged "
        f"days. Kind: {r.kind}. Status: {r.status}."
    )
    cols = st.columns(2)
    for i, (dim, title) in enumerate(
        [
            ("warehouse", "By warehouse"),
            ("cost_type", "By cost type"),
            ("query_pattern", "By query pattern"),
            ("user", "By user"),
        ]
    ):
        d = att[att.dimension == dim].copy()
        if not len(d):
            continue
        d["share"] = d.share_of_gap
        d["name"] = d.label.str.slice(0, 70)
        ch = (
            alt.Chart(d)
            .mark_bar(height=14, cornerRadiusEnd=4, color="#2a78d6")
            .encode(
                x=alt.X("share:Q", axis=alt.Axis(format="%"), title="Share of gap"),
                y=alt.Y("name:N", sort="-x", title=None),
                tooltip=[
                    alt.Tooltip("label:N", title="Driver"),
                    alt.Tooltip("contribution_dollars:Q", format="$,.0f", title="Dollars"),
                    alt.Tooltip("share:Q", format=".1%", title="Share of gap"),
                ],
            )
        )
        with cols[i % 2]:
            st.markdown(f"**{title}**")
            st.altair_chart(ch.properties(height=40 + 26 * len(d)), use_container_width=True)
    st.caption(
        "Gap = actual minus expected on flagged days. Every component uses the same "
        "reference days, so warehouse and cost type shares add up to the gap. Query "
        "pattern and user shares cover execution and cloud services only."
    )


def optimize():
    st.title("Optimize")
    disclaimer()
    r = q("select * from mart.recommendations order by monthly_dollars_expected desc")
    sav = r[r.rule != "upsize"]
    c = st.columns(3)
    c[0].metric("Monthly savings, expected", usd(sav.monthly_dollars_expected.sum()))
    c[1].metric("Monthly savings, conservative", usd(sav.monthly_dollars_conservative.sum()))
    c[2].metric("Recommendations", len(r))
    for x in r.itertuples():
        upsize = x.rule == "upsize"
        head = (
            f"{x.title}, worst case cost {usd(-x.monthly_dollars_conservative)} per month"
            if upsize
            else f"{x.title}, {usd(x.monthly_dollars_conservative)} to "
            f"{usd(x.monthly_dollars_expected)} per month"
        )
        with st.expander(f"{x.rec_id}  {head}"):
            st.code(x.action, language="sql")
            st.markdown(f"**Confidence:** {x.confidence}. **Method:** {x.method}")
            ev = json.loads(x.evidence)
            st.dataframe(
                pd.DataFrame({"evidence": list(ev), "value": [str(v) for v in ev.values()]}),
                hide_index=True,
                use_container_width=True,
            )


def memo_page():
    st.title("QBR Memo")
    disclaimer()
    memos = q("select * from app.qbr_memos")
    if IN_SNOWFLAKE:
        st.info(
            "Live generation inside Snowflake uses Cortex AI_COMPLETE, which this trial "
            "account does not allow. Showing the latest memo generated locally through the "
            "Anthropic API with the same tools and the same provenance guard."
        )
    else:
        if st.button("Generate a new memo"):
            with st.spinner("Planning, calling tools, drafting, checking every number..."):
                m = _local_agent().run(_memo_task(), kind="memo")
            st.session_state["memo"] = m
    m = st.session_state.get("memo")
    if m is not None:
        from spend_desk.agent.agent import render_memo
        from spend_desk.config import load_config

        md, calls = render_memo(m, load_config()), m.calls
    elif len(memos):
        md, calls = memos.markdown.iloc[-1], json.loads(memos.calls.iloc[-1])
    else:
        st.warning("No memo yet.")
        return
    body, _, cites = md.partition("\n## Citations")
    st.markdown(md_safe(body))
    with st.expander("Citations: the tool call behind every [T#]"):
        st.dataframe(
            pd.DataFrame(
                [
                    {"id": k, "tool": v["name"], "input": json.dumps(v["input"])}
                    for k, v in calls.items()
                ]
            ),
            hide_index=True,
            use_container_width=True,
        )
    st.download_button("Download as markdown", md, file_name=f"spend_desk_qbr_{AS_OF}.md")


def ask_page():
    st.title("Ask")
    disclaimer()
    if IN_SNOWFLAKE:
        st.info(
            "Live questions need Cortex AI functions, which this trial account blocks. "
            "Below are evaluation questions answered locally by the same agent and guard."
        )
        ev = q("select qid, question, answer, correct, provenance_passed from mart.agent_eval")
        for e in ev.itertuples():
            with st.expander(f"{e.qid}  {e.question}"):
                st.markdown(md_safe(e.answer))
                st.caption(
                    f"Graded correct: {e.correct}. Provenance check passed: {e.provenance_passed}."
                )
        return
    for turn in st.session_state.setdefault("chat", []):
        with st.chat_message(turn["role"]):
            st.markdown(md_safe(turn["content"]))
    if prompt := st.chat_input("Ask about spend, anomalies, budgets, or savings"):
        st.session_state.chat.append({"role": "user", "content": prompt})
        with st.chat_message("user"):
            st.markdown(prompt)
        with st.chat_message("assistant"):
            with st.spinner("Calling tools and checking numbers..."):
                r = _local_agent().run(prompt)
            note = (
                f"\n\n*Provenance check {'passed' if r.passed else 'FAILED'}: "
                f"{r.guard_results[-1].numbers_checked if r.guard_results else 0} numbers "
                f"verified against {len(r.calls)} tool calls.*"
            )
            st.markdown(md_safe(r.text + note))
        st.session_state.chat.append({"role": "assistant", "content": r.text + note})


def methodology():
    st.title("Methodology")
    st.warning(
        "All data is synthetic. It is shaped like Snowflake ACCOUNT_USAGE views and was "
        "generated with known anomalies and waste so every method can be scored against "
        "the truth. It is not real customer or internal Snowflake data."
    )
    st.subheader("Forecast backtest")
    s = q("select * from mart.forecast_model_scores")
    st.dataframe(
        s.assign(
            mape=s.mape.round(3),
            coverage=s.coverage.round(3),
            horizon_total_ape=s.horizon_total_ape.round(3),
        ),
        hide_index=True,
        use_container_width=True,
    )
    st.caption(
        "Rolling origin backtest: 6 origins, 14 days apart, 90 day horizon. MAPE is daily. "
        "Coverage is the share of actual days inside the 80 percent interval."
    )
    st.subheader("Anomaly detection, scored against planted anomalies")
    st.dataframe(q("select * from mart.anomaly_eval"), hide_index=True, use_container_width=True)
    st.caption("Thresholds tuned on the validation window, reported on the held out window.")
    st.subheader("Recommender, scored against planted waste")
    st.dataframe(
        q("select * from mart.recommendation_eval").T.rename(columns={0: "value"}),
        use_container_width=True,
    )
    st.subheader("Agent evaluation")
    ev = q(
        "select qid, question, expected_source, correct, provenance_passed, "
        "first_draft_passed, drafts, tool_calls from mart.agent_eval"
    )
    if len(ev):
        c = st.columns(3)
        c[0].metric("Answer accuracy", f"{ev.correct.mean():.0%}")
        c[1].metric("Provenance pass rate", f"{ev.provenance_passed.mean():.0%}")
        c[2].metric("First draft pass rate", f"{ev.first_draft_passed.mean():.0%}")
        st.dataframe(ev, hide_index=True, use_container_width=True)
    st.subheader("Metric definitions")
    for name, m in metrics_spec().items():
        with st.expander(f"{m['label']}  ({name})"):
            st.markdown(
                f"**Formula:** {m['formula']}  \n**Grain:** {', '.join(m['grain'])}  \n"
                f"**Source:** {m['source']}"
            )
            for c in m["caveats"]:
                st.markdown(f"- {c}")


# ---------------------------------------------------------------------------- agent (local)


@st.cache_resource
def _local_agent():
    import duckdb

    from data import DUCKDB_PATH
    from spend_desk.agent.agent import AnthropicAgent
    from spend_desk.agent.tools import Tools
    from spend_desk.config import load_config
    from spend_desk.models import Target

    con = duckdb.connect(str(DUCKDB_PATH), read_only=True)
    return AnthropicAgent(Tools(Target("duckdb", con), load_config()))


def _memo_task():
    from spend_desk.agent.agent import MEMO_TASK

    return MEMO_TASK


PAGES = {
    "Overview": overview,
    "Anomalies": anomalies,
    "Optimize": optimize,
    "QBR Memo": memo_page,
    "Ask": ask_page,
    "Methodology": methodology,
}
with st.sidebar:
    st.markdown("### Spend Desk")
    st.caption(f"{QUARTER}, as of {AS_OF}")
    page = st.radio("Page", list(PAGES), label_visibility="collapsed")
    st.caption("Runs in Snowflake" if IN_SNOWFLAKE else "Running locally on DuckDB")
PAGES[page]()
