import json

import pytest

from spend_desk import recommend as rc
from spend_desk.config import load_config
from spend_desk.models import Target

CFG = load_config()


@pytest.fixture(scope="module")
def recs(db):
    return rc.run(Target("duckdb", db), CFG)


def test_finds_planted_waste_without_false_recommendations(recs):
    m = recs["metrics"]
    assert m["share_found"] >= 0.8
    assert m["false_recommendations"] <= 1


def test_every_recommendation_is_actionable_and_evidenced(recs):
    df = recs["recommendations"]
    for r in df.itertuples():
        assert r.action and r.method and r.confidence in {"high", "medium", "low"}
        assert isinstance(json.loads(r.evidence), dict)
        if r.rule in ("auto_suspend", "downsize", "upsize"):
            assert r.action.startswith(f"ALTER WAREHOUSE {r.warehouse_name} SET ")
        if r.rule != "upsize":
            assert 0 <= r.monthly_dollars_conservative <= r.monthly_dollars_expected + 1e-9


def test_upsize_shows_cost_increase_and_performance_gain(recs):
    up = recs["recommendations"][recs["recommendations"].rule == "upsize"]
    assert len(up) >= 1
    for r in up.itertuples():
        assert r.monthly_dollars_conservative < 0
        assert json.loads(r.evidence)["queued_hours_per_month"] > 0


def test_auto_suspend_recommends_sixty_seconds(recs):
    df = recs["recommendations"]
    act = df[(df.rule == "auto_suspend") & (df.warehouse_name == "GTM_REPORTING_WH")].action
    assert act.tolist() == ["ALTER WAREHOUSE GTM_REPORTING_WH SET AUTO_SUSPEND = 60;"]


def test_dollars_use_the_assumed_price(recs):
    df = recs["recommendations"]
    assert (
        abs(df.monthly_dollars_expected - df.monthly_credits_expected * CFG.dollars_per_credit)
        < 1e-6
    ).all()
