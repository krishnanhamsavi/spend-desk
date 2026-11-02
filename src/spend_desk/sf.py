"""Snowflake connection helpers. Credentials come from .env, never from code."""

import os
import re

from dotenv import load_dotenv

from spend_desk.config import ROOT

load_dotenv(ROOT / ".env")


def connect(role: str | None = None):
    import snowflake.connector

    return snowflake.connector.connect(
        account=os.environ["SNOWFLAKE_ACCOUNT"],
        # Login name, which can differ from the user object name.
        user=os.environ["SNOWFLAKE_USER"],
        private_key_file=os.environ["SNOWFLAKE_PRIVATE_KEY_PATH"],
        private_key_file_pwd=os.environ.get("SNOWFLAKE_PRIVATE_KEY_PASSPHRASE") or None,
        role=role or os.environ.get("SNOWFLAKE_ROLE"),
        warehouse=os.environ.get("SNOWFLAKE_WAREHOUSE"),
        database=os.environ.get("SNOWFLAKE_DATABASE"),
    )


def split_statements(sql: str) -> list[str]:
    """Split a script on semicolons, dropping comments and blanks. No semicolons in literals."""
    body = re.sub(r"--[^\n]*", "", sql)
    return [s.strip() for s in body.split(";") if s.strip()]


def run_bootstrap() -> None:
    sql = (ROOT / "sql" / "bootstrap" / "00_bootstrap.sql").read_text(encoding="utf-8")
    con = connect(role="ACCOUNTADMIN")
    cur = con.cursor()
    user = cur.execute("SELECT CURRENT_USER()").fetchone()[0]
    sql = sql.replace("{{SNOWFLAKE_USER}}", '"' + user.replace('"', '""') + '"')
    for stmt in split_statements(sql):
        print(">", stmt.splitlines()[0][:90])
        rows = cur.execute(stmt).fetchall()
        if stmt.upper().startswith("SHOW"):
            for r in rows:
                print("   ", r[:4])
    con.close()


def load_raw() -> None:
    """Load the generated Parquet files into SPEND_DESK.RAW, replacing existing tables."""
    import time

    import pandas as pd
    from snowflake.connector.pandas_tools import write_pandas

    raw_dir = ROOT / "data" / "raw"
    files = sorted(raw_dir.glob("*.parquet"))
    if not files:
        raise SystemExit("No Parquet files in data/raw. Run `python -m spend_desk data` first.")
    con = connect()
    cur = con.cursor()
    cur.execute("USE SCHEMA SPEND_DESK.RAW")
    try:
        for f in files:
            t = time.time()
            df = pd.read_parquet(f)
            table = f.stem.upper()
            ok, _, nrows, _ = write_pandas(
                con, df, table, auto_create_table=True, overwrite=True, use_logical_type=True
            )
            print(f"RAW.{table:28s} {nrows:>10,} rows  {time.time() - t:5.1f}s  ok={ok}")
    finally:
        suspend(cur)
        con.close()


def suspend(cur) -> None:
    """Suspend the project warehouse now instead of waiting for AUTO_SUSPEND."""
    from snowflake.connector.errors import ProgrammingError

    try:
        cur.execute("ALTER WAREHOUSE SPEND_DESK_WH SUSPEND")
    except ProgrammingError:
        pass  # already suspended
