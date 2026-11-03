# Spend Desk: 2 minute demo script

Screen recording of the Streamlit app, running in Snowflake or locally. Speak at a normal pace. Each block is about 20 seconds.

**0:00 Overview.** "This is Spend Desk, a cloud spend copilot built on Snowflake for a Finance BI team. The data is synthetic, shaped like ACCOUNT_USAGE, with problems planted on purpose so every result can be scored. Top line: Q3 is projected to finish about 14 thousand dollars over a 192 thousand dollar budget." Point at the forecast vs budget chart. "ML Training drives it. The black tick is budget, the bar is the projection."

**0:20 Forecast.** Pick ML Training in the team selector. "Each team's forecast model was chosen by a rolling backtest. Snowflake ML won for ML Training, ETS won for others. The shaded band is the 80 percent interval."

**0:40 Anomalies.** "The detector scores daily spend with a robust z score and a level shift test." Pick AN008. "This is a 29 day Data Platform episode: a backfill, then a warehouse left at X-Large. Attribution puts almost all of the gap on DP_TRANSFORM_WH." Pick AN011. "This one was accepted as a new baseline because the owner confirmed a launch, so it stopped alerting."

**1:00 Optimize.** "Eleven recommendations, each with evidence, a conservative and expected monthly saving, and the exact SQL." Open R01. "Downsize the finance reporting warehouse. Scored against the planted waste, the recommender found all 11 items with no false recommendations." Open R10. "Upsizing is shown as a cost range next to the queueing it removes."

**1:20 QBR memo.** "Claude wrote this memo using six read only tools. It never calculates. Every number cites the tool call it came from." Open the citations expander. "A provenance guard checks every number against those tool outputs and rejects any draft that does not match. Across a 15 question eval, every final answer passed."

**1:40 Methodology.** "Everything here is measured against ground truth: forecast error against a naive baseline, anomaly precision and recall on a held out window, savings found against savings planted, and the agent's accuracy. The limitations are on this page and in the README. That's Spend Desk."
