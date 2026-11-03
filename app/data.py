"""Data access for the app. Reads MART and APP only, never RAW. Cached for an hour.

Inside Streamlit in Snowflake it uses the active Snowpark session. Locally it reads the
DuckDB mirror, so the app runs without a Snowflake account.
"""

from __future__ import annotations

from pathlib import Path

import pandas as pd
import streamlit as st

HERE = Path(__file__).resolve().parent
DUCKDB_PATH = HERE.parent / "data" / "spend_desk.duckdb"


def _session():
    try:
        from snowflake.snowpark.context import get_active_session

        return get_active_session()
    except Exception:
        return None


SESSION = _session()
IN_SNOWFLAKE = SESSION is not None


@st.cache_resource
def _duck():
    import duckdb

    return duckdb.connect(str(DUCKDB_PATH), read_only=True)


@st.cache_data(ttl=3600, show_spinner=False)
def q(sql: str) -> pd.DataFrame:
    if IN_SNOWFLAKE:
        df = SESSION.sql(sql).to_pandas()
    else:
        df = _duck().execute(sql).df()
    df.columns = [c.lower() for c in df.columns]
    return df


def metrics_spec() -> dict:
    import yaml

    for p in (HERE / "metrics.yaml", HERE.parent / "metrics.yaml"):
        if p.exists():
            return yaml.safe_load(p.read_text(encoding="utf-8"))["metrics"]
    return {}
