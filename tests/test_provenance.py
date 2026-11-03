from spend_desk.agent.provenance import check, extract_numbers

CALLS = {
    "T1": {"variance_dollars": 22589.0, "variance_pct": 48.3, "budget_dollars": 46789.0},
    "T2": {"idle_share": 0.958, "monthly_dollars_expected": 3934.0, "auto_suspend_s": 1800},
}


def values(text):
    return [f.value for f in extract_numbers(text)]


def test_extracts_money_percent_and_suffixes_but_not_dates_or_ids():
    assert values("ML Training is $22,589 over (48.3%) [T1]") == [22589.0, 48.3]
    assert values("about $14.3k and 1.2M credits") == [14300.0, 1.2e6]
    assert values("On 2026-08-04 in week 32 of Q3 FY27, AN008 and R03 fired") == []
    assert values("the top 3 of 5 drivers") == []


def test_grounded_numbers_pass():
    text = (
        "ML Training will finish $22,589 over budget, 48.3% above plan [T1].\n"
        "Downsizing saves $3,934 a month, idle share is 95.8% [T2]."
    )
    r = check(text, CALLS)
    assert r.passed and r.numbers_checked == 4


def test_rounded_display_matches_within_its_precision():
    assert check("Overrun of about $22.6k [T1].", CALLS).passed
    assert check("Overrun of about $23k [T1].", CALLS).passed
    assert not check("Overrun of about $24k [T1].", CALLS).passed


def test_computed_or_invented_numbers_fail():
    r = check("The overrun is $69,378 minus budget, so $22,600 [T1].", CALLS)
    assert not r.passed and {u["text"] for u in r.unmatched} == {"$69,378", "$22,600"}


def test_number_must_come_from_a_tool_cited_on_that_line():
    r = check("Downsizing saves $3,934 a month [T1].", CALLS)
    assert not r.passed


def test_uncited_numbers_fail():
    r = check("ML Training is $22,589 over budget.", CALLS)
    assert not r.passed and r.uncited_lines


def test_dash_connectors_are_flagged_as_style():
    r = check("Spend rose — mostly idle [T1].", CALLS)
    assert r.style_violations
