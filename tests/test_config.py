from datetime import date

from spend_desk.config import load_config


def test_config_loads():
    cfg = load_config()
    assert cfg.dollars_per_credit == 3.00
    assert len(cfg.teams) == 6
    assert cfg.history_start < cfg.as_of


def test_history_is_18_months():
    cfg = load_config()
    start, end = cfg.history_start, cfg.as_of
    months = (end.year - start.year) * 12 + end.month - start.month
    assert months == 18


def test_fiscal_quarter_feb_start():
    cfg = load_config()
    assert cfg.fiscal_quarter(date(2026, 2, 1)) == "Q1 FY27"
    assert cfg.fiscal_quarter(date(2026, 9, 15)) == "Q3 FY27"
    assert cfg.fiscal_quarter(date(2027, 1, 31)) == "Q4 FY27"
    assert cfg.fiscal_quarter(date(2026, 1, 31)) == "Q4 FY26"


def test_bootstrap_script_parses():
    from spend_desk.config import ROOT
    from spend_desk.sf import split_statements

    sql = (ROOT / "sql" / "bootstrap" / "00_bootstrap.sql").read_text(encoding="utf-8")
    stmts = split_statements(sql)
    assert any("AUTO_SUSPEND = 60" in s for s in stmts)
    assert any("CREDIT_QUOTA = 25" in s for s in stmts)
    assert all("{{" not in s or "TO USER" in s for s in stmts)
