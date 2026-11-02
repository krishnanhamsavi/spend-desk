"""Generate all RAW tables, write Parquet, and load the local DuckDB mirror."""

from __future__ import annotations

import time

import duckdb
import pandas as pd

from spend_desk.config import ROOT, Config, load_config
from spend_desk.generate import tables
from spend_desk.generate.engine import make_calendar, simulate
from spend_desk.generate.scenario import build_world

RAW_DIR = ROOT / "data" / "raw"
DUCKDB_PATH = ROOT / "data" / "spend_desk.duckdb"


def generate(cfg: Config) -> dict[str, pd.DataFrame]:
    seed = cfg.raw["data"]["seed"]
    cal = make_calendar(cfg.history_start, cfg.as_of, cfg.fiscal_year_start_month)
    whs, truth = build_world()
    sims = {wh.name: simulate(wh, cal, seed, detail=True) for wh in whs}
    resize_users = {t.truth_id: t.user_name for t in truth if t.user_name}
    gt, gt_daily = tables.ground_truth(whs, truth, sims, cal, cfg)
    return {
        "WAREHOUSE_METERING_HISTORY": tables.metering(whs, sims, cal),
        "QUERY_HISTORY": tables.query_history(whs, sims, cal, cfg),
        "WAREHOUSE_EVENTS_HISTORY": tables.warehouse_events(whs, sims, cal, seed, resize_users),
        "TAG_REFERENCES": tables.tag_references(whs),
        "GROUND_TRUTH": gt,
        "GROUND_TRUTH_DAILY": gt_daily,
    }


def write_local(frames: dict[str, pd.DataFrame]) -> None:
    RAW_DIR.mkdir(parents=True, exist_ok=True)
    for name, df in frames.items():
        df.to_parquet(RAW_DIR / f"{name.lower()}.parquet", index=False)
    con = duckdb.connect(str(DUCKDB_PATH))
    con.execute("CREATE SCHEMA IF NOT EXISTS RAW")
    for name in frames:
        path = (RAW_DIR / f"{name.lower()}.parquet").as_posix()
        con.execute(f"CREATE OR REPLACE TABLE RAW.{name} AS SELECT * FROM read_parquet('{path}')")
    con.close()


def main() -> None:
    cfg = load_config()
    t = time.time()
    frames = generate(cfg)
    write_local(frames)
    for name, df in frames.items():
        print(f"RAW.{name:28s} {len(df):>10,} rows")
    print(f"Done in {time.time() - t:.0f}s. DuckDB at {DUCKDB_PATH.relative_to(ROOT)}")
