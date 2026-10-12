import pytest

from src.intelligence_layer.order_router import decide_route
from src.intelligence_layer.rule_checks import (
    FACT_DESCRIPTIONS,
    build_order_facts,
    check_order_against_rules,
    evaluate_condition,
    find_release_blocker,
    find_rule_problems,
    render_message,
    stricter_verdict,
    verdict_from_findings,
)
from src.knowledge_layer import reference_store, rule_store
from src.utils.mock_data import get_mock_orders, get_problem_demo_orders
from src.utils.models import Rule, RuleFinding


def checks_for(order_no):
    lists = reference_store.get_all_reference_lists()
    order = next(order for order in get_mock_orders() if order.order_no == order_no)
    order.route = decide_route(order, lists)[0].value
    return check_order_against_rules(order, rule_store.get_rules(), lists)


def names(findings):
    return {finding.rule_name for finding in findings}


def test_facts_match_documented_fact_list(seeded_db):
    facts = build_order_facts(
        get_mock_orders()[0], reference_store.get_all_reference_lists()
    )
    assert set(facts) == set(FACT_DESCRIPTIONS)


@pytest.mark.parametrize(
    "condition, expected",
    [
        ({"field": "x", "op": "eq", "value": 1}, True),
        ({"field": "x", "op": "neq", "value": 1}, False),
        ({"field": "x", "op": "in", "value": [1, 2]}, True),
        ({"field": "x", "op": "not_in", "value": [1, 2]}, False),
        ({"field": "x", "op": "gt", "value": 0}, True),
        ({"field": "x", "op": "lt", "value": 0}, False),
        ({"field": "x", "op": "exists"}, True),
        ({"field": "blank", "op": "missing"}, True),
        ({"field": "unknown", "op": "eq", "value": None}, False),
    ],
)
def test_evaluate_condition(condition, expected):
    assert (
        evaluate_condition({"x": 1, "blank": " ", "unknown": None}, condition)
        is expected
    )


def test_render_message_keeps_unknown_placeholders():
    assert render_message("{a} and {b}", {"a": "A"}) == "A and {b}"


def test_clean_order_has_no_findings(seeded_db):
    assert checks_for("REN-1103701").findings == []
    assert checks_for("FED-1103714").findings == []  # Jennifer's 10-cord example


def test_hard_rules_find_known_problems(seeded_db):
    assert "SFP+ transceivers need a 10GE appliance" in names(
        checks_for("FEDR-1103736").findings
    )
    assert "Power cord type matches the country" in names(
        checks_for("REN-1103679").findings
    )
    assert {"Missing shipping method", "Missing ship-to phone"} <= names(
        checks_for("DIST-MOCK-0003").findings
    )


def test_eus_order_is_held_not_rejected(seeded_db):
    checks = checks_for("DIST-MOCK-0004")
    assert names(checks.findings) == {"Approved EUS needed"}
    assert verdict_from_findings(checks.findings) == "hold"
    assert find_release_blocker(checks.findings) is None


def test_embargo_and_banned_customer_can_never_be_released(seeded_db):
    for order_no in ["DIST-MOCK-0005", "DIST-MOCK-0009"]:
        findings = checks_for(order_no).findings
        assert verdict_from_findings(findings) == "reject"
        assert find_release_blocker(findings)


def test_soft_rules_go_to_the_ai_and_respect_scope(seeded_db):
    uae = {rule.name for rule in checks_for("DIST-MOCK-0010").judgement_rules}
    us = {rule.name for rule in checks_for("REN-1103701").judgement_rules}
    assert "Blue-ink sign and stamp (UAE, Qatar)" in uae
    assert "Blue-ink sign and stamp (UAE, Qatar)" not in us
    assert "Power cords per appliance" in us


def test_verdict_strictness():
    assert stricter_verdict("approve", "hold") == "hold"
    assert stricter_verdict("reject", "hold") == "reject"
    warn_only = [RuleFinding(None, "w", "warn", "m")]
    assert verdict_from_findings(warn_only) == "approve"


def test_find_rule_problems():
    good_hard = Rule(
        "R", "hard", "reject", "d", conditions=[{"field": "fob", "op": "missing"}]
    )
    assert find_rule_problems(good_hard) == []
    assert find_rule_problems(Rule("R", "hard", "reject", "d"))  # no conditions
    assert find_rule_problems(Rule("R", "soft", "hold", "d"))  # no guidance
    unknown_fact = Rule(
        "R", "hard", "reject", "d", conditions=[{"field": "x", "op": "eq"}]
    )
    assert find_rule_problems(unknown_fact)
    bad_placeholder = Rule(
        "R",
        "hard",
        "reject",
        "d",
        conditions=[{"field": "fob", "op": "missing"}],
        message="{nope}",
    )
    assert find_rule_problems(bad_placeholder)


def demo_checks(order_no):
    lists = reference_store.get_all_reference_lists()
    order = next(o for o in get_problem_demo_orders() if o.order_no == order_no)
    order.route = decide_route(order, lists)[0].value
    return check_order_against_rules(order, rule_store.get_rules(), lists)


def test_demo_orders_fail_or_hold(seeded_db):
    assert len(get_problem_demo_orders()) == 10
    for order_no in ["DIST-DEMO-0001", "DIST-DEMO-0002"]:  # embargo, banned customer
        findings = demo_checks(order_no).findings
        assert verdict_from_findings(findings) == "reject"
        assert find_release_blocker(findings)
    for order_no in ["REN-DEMO-0003", "FED-DEMO-0005", "DIST-DEMO-0006"]:
        findings = demo_checks(order_no).findings
        assert verdict_from_findings(findings) == "reject", order_no
        assert find_release_blocker(findings) is None
    for order_no in ["DIST-DEMO-0007", "DIST-DEMO-0008"]:  # EUS needed
        assert verdict_from_findings(demo_checks(order_no).findings) == "hold"


def test_demo_orders_left_to_ai_judgement(seeded_db):
    for order_no, rule_name in [
        ("REG-DEMO-0004", "Power cords per appliance"),  # 2 TE-1606 need 4 cords
        ("REG-DEMO-0010", "Power cords per appliance"),  # mixed cord types
        ("DIST-DEMO-0009", "Letter-of-credit orders are held"),
    ]:
        checks = demo_checks(order_no)
        assert rule_name in {rule.name for rule in checks.judgement_rules}, order_no
    assert demo_checks("REG-DEMO-0004").facts["power_cord_count_ok"] is False


def test_real_item_class_names_count_cords(seeded_db):
    """Fusion calls them 'Hardware Base Units' and 'Power Supply Units'."""
    facts = demo_checks("REG-DEMO-0004").facts
    assert (facts["expected_power_cord_qty"], facts["power_cord_qty"]) == (4, 2)
    assert demo_checks("FED-DEMO-0005").facts["sfp_compatible"] is False
    assert (
        checks_for("DIST-MOCK-0007").facts["has_10ge_appliance"] is True
    )  # "10GE" in SKU
