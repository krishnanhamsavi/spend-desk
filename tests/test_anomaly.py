import numpy as np
import pandas as pd
import pytest

from spend_desk import anomaly as an
from spend_desk.config import load_config
from spend_desk.models import Target

CFG = load_config()
P = an.Params(z=4.0, min_dollars=25)


def _series(n=200, seed=0):
    rng = np.random.default_rng(seed)
    weekly = np.array([1.0, 1.05, 1.1, 1.05, 1.0, 0.5, 0.45])
    return 100 * weekly[np.arange(n) % 7] * np.exp(rng.normal(0, 0.05, n))


def test_single_day_spike_is_flagged_once():
    y = _series()
    y[150] *= 2.5
    r = an.detect_team(y, np.zeros(len(y), bool), [], P, price=3.0)
    assert r.flagged[150] and r.kind[150] == "spike"
    assert r.flagged[100:200].sum() == 1


def test_unconfirmed_level_shift_stays_flagged_and_confirmed_one_stops():
    y = _series()
    y[120:] *= 1.35  # sustained, modest per day
    none = an.detect_team(y, np.zeros(len(y), bool), [], P, price=3.0)
    assert none.flagged[120:130].any()
    assert none.flagged[180:200].mean() > 0.8  # never confirmed, still flagged

    confirmed = an.detect_team(y, np.zeros(len(y), bool), [(120, 127)], P, price=3.0)
    assert confirmed.flagged[120:127].any()  # alerted before the owner confirmed
    assert not confirmed.flagged[127:].any()  # new baseline afterwards


def test_recurring_close_days_learn_their_own_uplift():
    dates = pd.date_range("2025-01-01", periods=300, freq="D")
    is_close = an.close_days(dates)
    y = _series(300)
    y[is_close] *= 2.0
    r = an.detect_team(y, is_close, [], P, price=3.0)
    assert r.flagged[150:].sum() == 0


def test_attribution_components_add_up_to_the_gap():
    y = _series()
    y[150] *= 2.0
    res = an.detect_team(y, np.zeros(len(y), bool), [], P, price=3.0)
    parts = pd.DataFrame({"a": y * 0.7, "b": y * 0.3})
    parts.loc[150, "a"] += 5.0
    parts.loc[150, "b"] -= 5.0
    team = pd.DataFrame({"t": parts.sum(axis=1)})
    gap = an._contrib(team, [150], res).iloc[0]
    assert np.isclose(an._contrib(parts, [150], res).sum(), gap)


@pytest.fixture(scope="module")
def detected(db):
    an.run(Target("duckdb", db), CFG)
    return db


def test_tuned_on_validation_and_scored_on_holdout(detected):
    ev = detected.execute("select * from mart.anomaly_eval where method = 'robust_z'").df()
    ev = ev.set_index("eval_window")
    assert ev.loc["validation", "threshold"] == ev.loc["holdout", "threshold"]
    assert ev.loc["holdout", "recall"] >= 0.5 and ev.loc["holdout", "precision"] >= 0.5
    assert (ev.legit_days_flagged_after_confirmation == 0).all()


def test_attribution_names_the_planted_root_cause(detected):
    def top(truth_id, dimension):
        aid = (
            detected.execute(f"""select anomaly_ids from mart.anomaly_eval_detail
            where truth_id = '{truth_id}' and method = 'robust_z'""")
            .fetchone()[0]
            .split(",")[0]
        )
        return detected.execute(f"""select key from mart.anomaly_attribution
            where anomaly_id = '{aid}' and dimension = '{dimension}' and rank = 1""").fetchone()[0]

    assert top("A01", "user") == "LFISCHER"
    assert top("A07", "user") == "WOKONKWO"
    assert top("A12", "warehouse") == "GTM_CRM_WH"
    assert top("A05", "cost_type") == "cloud_services"
