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
from src.utils.mock_data import (
    CORD_EU,
    CORD_US,
    appliance,
    get_dummy_orders,
    make_line,
    make_order,
    power_cord,
    power_supply,
)
from src.utils.models import Rule, RuleFinding
from tests.sample_orders import get_sample_orders


def checks_for(order_no):
    lists = reference_store.get_all_reference_lists()
    order = next(order for order in get_sample_orders() if order.order_no == order_no)
    order.route = decide_route(order, lists)[0].value
    return check_order_against_rules(order, rule_store.get_rules(), lists)


def names(findings):
    return {finding.rule_name for finding in findings}


def test_facts_match_documented_fact_list(seeded_db):
    facts = build_order_facts(
        get_sample_orders()[0], reference_store.get_all_reference_lists()
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


def checks_on(order):
    lists = reference_store.get_all_reference_lists()
    order.route = decide_route(order, lists)[0].value
    return check_order_against_rules(order, rule_store.get_rules(), lists)


def dummy_checks(order_no):
    return checks_on(next(o for o in get_dummy_orders() if o.order_no == order_no))


def test_dummy_orders_fail_or_hold(seeded_db):
    assert [order.order_no for order in get_dummy_orders()] == [
        "dummy_DIST-0001",
        "dummy_REN-0002",
        "dummy_REG-0003",
        "dummy_FED-0004",
        "dummy_DIST-0005",
    ]
    findings = dummy_checks("dummy_DIST-0001").findings  # embargoed Iran
    assert verdict_from_findings(findings) == "reject"
    assert find_release_blocker(findings)
    for order_no, rule_name in [
        ("dummy_REN-0002", "Power cord type matches the country"),
        ("dummy_REG-0003", "Too few power cords"),
        ("dummy_FED-0004", "SFP+ transceivers need a 10GE appliance"),
    ]:
        findings = dummy_checks(order_no).findings
        assert rule_name in names(findings), order_no
        assert verdict_from_findings(findings) == "reject", order_no
        assert find_release_blocker(findings) is None
    assert names(dummy_checks("dummy_DIST-0005").findings) == {"Approved EUS needed"}


def test_real_item_class_names_count_cords(seeded_db):
    """Fusion calls them 'Hardware Base Units' and 'Power Supply Units'."""
    facts = dummy_checks("dummy_REG-0003").facts
    assert (facts["expected_power_cord_qty"], facts["power_cord_qty"]) == (4, 2)
    assert dummy_checks("dummy_FED-0004").facts["sfp_compatible"] is False
    assert (
        checks_for("DIST-MOCK-0007").facts["has_10ge_appliance"] is True
    )  # "10GE" in SKU


def us_order(lines, **overrides):
    return make_order("REG-1", "Regular", "United States", "US", lines, **overrides)


def test_power_cord_counts(seeded_db):
    # Like REN-1104280: 4 x TE-1506 + 4 PSU (8 cords) and 9 x TE-906-HW-2AC (18).
    lines = [
        appliance(1, "TE-1506-HW-AC", 4),
        power_supply(2, 4),
        appliance(3, "TE-906-HW-2AC", 9),
        power_cord(4, CORD_US, 26),
        make_line(5, "IB-SFPPLUS-SR", "SFP+", 4, "Transceiver"),  # needs no cord
    ]
    assert checks_on(us_order(lines)).facts["expected_power_cord_qty"] == 26
    one_cord_906 = [appliance(1, "TE-906-HW-AC"), power_cord(2, CORD_US, 2)]
    checks = checks_on(us_order(one_cord_906))
    assert names(checks.findings) == {"More power cords than needed"}
    assert verdict_from_findings(checks.findings) == "hold"
    mixed = [
        appliance(1, "TE-1506-HW-AC", 2),
        power_cord(2, CORD_US),
        power_cord(3, CORD_EU),
    ]
    assert {"Mixed power cord types", "Power cord type matches the country"} <= names(
        checks_on(us_order(mixed)).findings
    )
    spare_cords_only = [power_cord(1, CORD_US, 3)]
    assert checks_on(us_order(spare_cords_only)).findings == []


@pytest.mark.parametrize(
    "country, code, cord",
    [
        ("Japan", "JP", "IB-POWER-CORD-14G-JP"),
        ("Australia", "AU", "IB-POWER-CORD-14G-AUS"),
        ("Singapore", "SG", "IB-POWER-CORD-14G-UK"),
        ("Hong Kong", "HK", "IB-POWER-CORD-14G-UK"),
        ("Italy", "IT", "IB-POWER-CORD-14G-CL"),
        ("India", "IN", "IB-POWER-CORD-14G-INDIA"),
        ("Spain", "ES", "IB-POWER-CORD-14G-EU"),
    ],
)
def test_cord_type_per_country(seeded_db, country, code, cord):
    order = make_order(
        "DIST-1",
        "Distributor",
        country,
        code,
        [appliance(1, "TE-1506-HW-AC"), power_cord(2, (cord, "Power Cord"))],
    )
    assert checks_on(order).facts["cord_region_ok"] is True


def test_embargoed_countries_and_regions_can_never_be_released(seeded_db):
    lines = [appliance(1, "TE-1506-HW-AC"), power_cord(2, CORD_EU)]
    russia = make_order("DIST-1", "Distributor", "Russia", "RU", lines)
    assert "Embargoed destination" in names(checks_on(russia).findings)
    crimea = make_order(
        "DIST-2",
        "Distributor",
        "Ukraine",
        "UA",
        lines,
        ship_to_address="1 Lenin St, Simferopol, Crimea",
    )
    findings = checks_on(crimea).findings
    assert "Embargoed region" in names(findings) and find_release_blocker(findings)


def test_power_supply_must_fit_an_appliance(seeded_db):
    fits = [appliance(1, "TE-1506-HW-AC"), power_supply(2), power_cord(3, CORD_US, 2)]
    assert checks_on(us_order(fits)).facts["psu_compatible"] is True
    wrong = [appliance(1, "TE-906-HW-AC"), power_supply(2), power_cord(3, CORD_US, 2)]
    assert "Power supply fits the appliance" in names(
        checks_on(us_order(wrong)).findings
    )
    spare_psu_only = [power_supply(1), power_cord(2, CORD_US)]
    assert checks_on(us_order(spare_psu_only)).facts["psu_compatible"] is None


def test_collect_orders_need_forwarder_details(seeded_db):
    lines = [appliance(1, "TE-1506-HW-AC"), power_cord(2, CORD_US)]
    contact = "Ship to Contact: Kelly Cheng  Phone Number: 8610-82704326."
    missing = us_order(lines, freight_terms="Collect", shipping_instructions=contact)
    assert "Forwarder details for customer-routed freight" in names(
        checks_on(missing).findings
    )
    for details in [
        " SHIP VIA: Apex Logistics International Inc.",
        " Please ship through DHL Account# 968823840.",
        " UPS Acct # 6W7F69",
        " Contact fra@hnfriends.com before shipping.",
    ]:
        given = us_order(
            lines, freight_terms="Collect", shipping_instructions=contact + details
        )
        assert checks_on(given).facts["forwarder_details_present"] is True, details
    prepaid = us_order(lines, shipping_instructions=contact)  # "Prepay & Add"
    assert checks_on(prepaid).findings == []


def test_ship_to_contact_name_is_required(seeded_db):
    lines = [appliance(1, "TE-1506-HW-AC"), power_cord(2, CORD_US)]
    no_name = us_order(
        lines, shipping_instructions="Ship to Contact:  Phone Number: 555 0100"
    )
    assert "Missing ship-to contact name" in names(checks_on(no_name).findings)
    named = us_order(
        lines,
        shipping_instructions="Ship to Contact: Shinya 吉田真也  Phone Number: +81 454 709 638",
    )
    assert checks_on(named).facts["ship_to_contact_present"] is True
