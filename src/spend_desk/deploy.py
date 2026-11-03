"""Deploy the app to Streamlit in Snowflake, and sync locally generated agent outputs.

sync    copies APP.QBR_MEMOS, MART.AGENT_EVAL, and APP.PROVENANCE_LOG from the local DuckDB
        mirror to Snowflake. On a trial account Cortex is blocked, so memos and agent evals are
        generated locally through the Anthropic API and shown in the deployed app.
deploy  uploads app files to a stage and creates the Streamlit object on SPEND_DESK_WH.
"""

from __future__ import annotations

import shutil
import tempfile
from pathlib import Path

import duckdb

from spend_desk.config import ROOT
from spend_desk.forecast import write_table
from spend_desk.generate.build import DUCKDB_PATH
from spend_desk.models import open_target

SYNC_TABLES = ["app.qbr_memos", "mart.agent_eval", "app.provenance_log"]
STAGE = "SPEND_DESK.APP.STREAMLIT_STAGE"
APP_FILES = ["streamlit_app.py", "data.py"]
ENVIRONMENT = """name: spend_desk
channels:
  - snowflake
dependencies:
  - altair
  - pandas
  - pyyaml
"""


def sync() -> None:
    local = duckdb.connect(str(DUCKDB_PATH), read_only=True)
    target = open_target("snowflake")
    try:
        target.execute("create schema if not exists app")
        for t in SYNC_TABLES:
            df = local.execute(f"select * from {t}").df()
            write_table(target, t, df)
            print(f"synced {t}: {len(df)} rows")
    finally:
        target.close()


def deploy() -> None:
    target = open_target("snowflake")
    cur = target.con.cursor()
    try:
        cur.execute(f"create stage if not exists {STAGE} directory = (enable = true)")
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            for f in APP_FILES:
                shutil.copy(ROOT / "app" / f, tmp / f)
            shutil.copy(ROOT / "metrics.yaml", tmp / "metrics.yaml")
            (tmp / "environment.yml").write_text(ENVIRONMENT, encoding="utf-8")
            for f in sorted(tmp.iterdir()):
                cur.execute(
                    f"put 'file://{f.as_posix()}' @{STAGE}/spend_desk "
                    "auto_compress = false overwrite = true"
                )
                print(f"uploaded {f.name}")
        try:
            cur.execute(f"""create or replace streamlit SPEND_DESK.APP.SPEND_DESK
                from '@{STAGE}/spend_desk' main_file = 'streamlit_app.py'
                query_warehouse = SPEND_DESK_WH title = 'Spend Desk'""")
        except Exception:
            cur.execute(f"""create or replace streamlit SPEND_DESK.APP.SPEND_DESK
                root_location = '@{STAGE}/spend_desk' main_file = 'streamlit_app.py'
                query_warehouse = SPEND_DESK_WH title = 'Spend Desk'""")
        cur.execute("show streamlits like 'SPEND_DESK' in schema SPEND_DESK.APP")
        cols = [c[0] for c in cur.description]
        row = dict(zip(cols, cur.fetchone(), strict=False))
        print(f"created streamlit {row.get('name')} (url id {row.get('url_id')})")
    finally:
        target.close()
