from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
CONFIG_PATH = ROOT / "config.yaml"


@dataclass(frozen=True)
class Config:
    raw: dict

    @property
    def dollars_per_credit(self) -> float:
        return float(self.raw["pricing"]["dollars_per_credit"])

    @property
    def teams(self) -> list[str]:
        return list(self.raw["teams"])

    @property
    def history_start(self) -> date:
        return date.fromisoformat(self.raw["calendar"]["history_start"])

    @property
    def as_of(self) -> date:
        return date.fromisoformat(self.raw["calendar"]["as_of"])

    @property
    def fiscal_year_start_month(self) -> int:
        return int(self.raw["calendar"]["fiscal_year_start_month"])

    def fiscal_quarter(self, d: date) -> str:
        """Label like 'Q3 FY27'. FY is named for the calendar year it ends in."""
        start = self.fiscal_year_start_month
        months_in = (d.month - start) % 12
        fy = d.year + 1 if d.month >= start else d.year
        return f"Q{months_in // 3 + 1} FY{fy % 100:02d}"

    def fiscal_quarters(self, fy: int) -> list[tuple[str, date, date, int]]:
        """(label, first day, last day, index 1 to 4) for fiscal year fy, e.g. 27 for FY27."""
        start_month = self.fiscal_year_start_month
        year = 2000 + fy - (1 if start_month > 1 else 0)
        out = []
        for i in range(4):
            m = start_month + 3 * i
            first = date(year + (m - 1) // 12, (m - 1) % 12 + 1, 1)
            m_next = m + 3
            nxt = date(year + (m_next - 1) // 12, (m_next - 1) % 12 + 1, 1)
            out.append((f"Q{i + 1} FY{fy:02d}", first, nxt - timedelta(days=1), i + 1))
        return out

    def quarter_of(self, d: date) -> tuple[str, date, date, int]:
        label = self.fiscal_quarter(d)
        fy = int(label[-2:])
        return next(q for q in self.fiscal_quarters(fy) if q[0] == label)


def load_config(path: Path = CONFIG_PATH) -> Config:
    with open(path, encoding="utf-8") as f:
        return Config(yaml.safe_load(f))
