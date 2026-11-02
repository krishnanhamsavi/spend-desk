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
