"""Build CORE and MART from plain SQL files, and run data quality tests, on DuckDB or Snowflake.

Models run in file order: sql/models/core/*.sql, then sql/models/mart/*.sql.
Each data quality test in sql/tests returns failing rows. Zero rows means pass.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path

from spend_desk.config import ROOT, Config, load_config
from spend_desk.sf import split_statements

MODEL_DIR = ROOT / "sql" / "models"
TEST_DIR = ROOT / "sql" / "tests"
LAYERS = ("core", "mart")


def template_vars(cfg: Config) -> dict[str, str]:
    teams = ", ".join(f"('{t.upper().replace(' ', '_')}', '{t}')" for t in cfg.teams)
    budget = cfg.raw["budget"]
    plan_date = date.fromisoformat(budget["plan_date"])
    quarters = ", ".join(
        f"('{label}', '{first}', '{last}', {idx}, {(last - first).days + 1})"
        for label, first, last, idx in cfg.fiscal_quarters(budget["fiscal_year"])
    )
    growth = ", ".join(f"('{t}', {g})" for t, g in budget["quarterly_growth"].items())
    current = cfg.quarter_of(cfg.as_of)
    return {
        "dollars_per_credit": repr(float(cfg.dollars_per_credit)),
        "query_sample_rate": repr(float(cfg.raw["data"]["query_sample_rate"])),
        "team_values": teams,
        "plan_date": str(plan_date),
        "base_start": str(plan_date - timedelta(days=budget["base_window_days"] - 1)),
        "fiscal_quarters": quarters,
        "budget_growth": growth,
        "as_of": str(cfg.as_of),
        "current_quarter": current[0],
        "quarter_start": str(current[1]),
        "quarter_end": str(current[2]),
    }


def render(sql: str, variables: dict[str, str]) -> str:
    def sub(m: re.Match) -> str:
        key = m.group(1)
        if key not in variables:
            raise KeyError(f"Unknown template variable {{{{ {key} }}}}")
        return variables[key]

    return re.sub(r"\{\{\s*(\w+)\s*\}\}", sub, sql)


def model_files() -> list[Path]:
    return [f for layer in LAYERS for f in sorted((MODEL_DIR / layer).glob("*.sql"))]


@dataclass
class Target:
    """A thin wrapper so the same SQL runs on DuckDB and Snowflake."""

    name: str
    con: object

    def execute(self, sql: str) -> None:
        if self.name == "duckdb":
            self.con.execute(sql)
        else:
            self.con.cursor().execute(sql)

    def fetchall(self, sql: str) -> list[tuple]:
        if self.name == "duckdb":
            return self.con.execute(sql).fetchall()
        return self.con.cursor().execute(sql).fetchall()

    def close(self) -> None:
        if self.name == "snowflake":
            from spend_desk.sf import suspend

            suspend(self.con.cursor())
        self.con.close()


def open_target(name: str, duckdb_path: Path | None = None) -> Target:
    if name == "duckdb":
        import duckdb

        from spend_desk.generate.build import DUCKDB_PATH

        return Target("duckdb", duckdb.connect(str(duckdb_path or DUCKDB_PATH)))
    from spend_desk.sf import connect

    con = connect()
    con.cursor().execute("USE DATABASE SPEND_DESK")
    return Target("snowflake", con)


def build(target: Target, cfg: Config | None = None, verbose: bool = True) -> None:
    variables = template_vars(cfg or load_config())
    for layer in LAYERS:
        target.execute(f"create schema if not exists {layer}")
    for f in model_files():
        for stmt in split_statements(render(f.read_text(encoding="utf-8"), variables)):
            target.execute(stmt)
        if verbose:
            print(f"built {f.parent.name}.{f.stem[3:]}")


def run_dq(target: Target) -> dict[str, int]:
    results = {}
    for f in sorted(TEST_DIR.glob("dq_*.sql")):
        (stmt,) = split_statements(f.read_text(encoding="utf-8"))
        n = target.fetchall(f"select count(*) from ({stmt}) as failing")[0][0]
        results[f.stem] = int(n)
    return results


def main(target_name: str) -> int:
    target = open_target(target_name)
    try:
        build(target)
        results = run_dq(target)
    finally:
        target.close()
    for name, n in results.items():
        print(f"{'PASS' if n == 0 else 'FAIL'}  {name}  ({n} failing rows)")
    return 0 if all(n == 0 for n in results.values()) else 1
