import pytest

from src.intelligence_layer.order_router import decide_route
from src.intelligence_layer.order_validation import (
    FACT_DESCRIPTIONS,
    build_order_facts,
    evaluate_condition,
    recommend,
    render_message,
    validate_order,
)
from src.knowledge_layer import reference_store, rule_store
from src.utils.mock_data import get_mock_orders
from src.utils.models import AiRecommendation


def findings_for(order_no):
    lists = reference_store.get_all_reference_lists()
    order = next(order for order in get_mock_orders() if order.order_no == order_no)
    order.route = decide_route(order, lists)[0].value
    return validate_order(order, rule_store.get_active_rules(), lists)


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
        ({"field": "x", "op": "not_in", "value": [2]}, True),
        ({"field": "x", "op": "gt", "value": 0}, True),
        ({"field": "x", "op": "lt", "value": 0}, False),
        ({"field": "blank", "op": "missing"}, True),
        ({"field": "blank", "op": "exists"}, False),
        ({"field": "unknown", "op": "eq", "value": False}, False),
    ],
)
def test_evaluate_condition(condition, expected):
    facts = {"x": 1, "blank": "  ", "unknown": None}
    assert evaluate_condition(facts, condition) is expected


def test_unsupported_operator_is_rejected():
    with pytest.raises(ValueError):
        evaluate_condition({"x": 1}, {"field": "x", "op": "like", "value": 1})


def test_render_message_keeps_unknown_placeholders():
    assert render_message("{a} and {b}", {"a": 1}) == "1 and {b}"


def test_clean_order_passes(seeded_db):
    findings = findings_for("REN-1103701")
    assert findings == []
    assert recommend(findings) == AiRecommendation.APPROVE


def test_dry_run_sfp_without_10ge_appliance_is_blocked(seeded_db):
    findings = findings_for("FEDR-1103736")
    assert names(findings) == {"SFP+ without a 10GE appliance"}
    assert recommend(findings) == AiRecommendation.REJECT


def test_dry_run_thailand_orders_warn_about_missing_cord_mapping(seeded_db):
    for order_no in ["DIST-1102990", "DIST-1103784"]:
        findings = findings_for(order_no)
        assert names(findings) == {"No cord mapping for destination"}
        assert recommend(findings) == AiRecommendation.APPROVE


def test_dry_run_uae_order_with_eu_cords(seeded_db):
    findings = findings_for("REN-1103679")
    assert names(findings) == {
        "Power-cord region",
        "Order documents not received",
        "Blue-ink sign and stamp",
    }
    assert "needs UK power cords" in next(
        f.message for f in findings if f.rule_name == "Power-cord region"
    )


def test_cord_count_mismatch(seeded_db):
    findings = findings_for("REN-MOCK-0002")
    assert names(findings) == {"Power-cord count"}
    assert "Expected 4" in findings[0].message


def test_missing_mandatory_fields(seeded_db):
    assert names(findings_for("DIST-MOCK-0003")) == {
        "Missing shipping method",
        "Missing customer PO",
        "Missing ship-to phone",
    }


def test_eus_order_waits_for_documents(seeded_db):
    assert {"Approved EUS not linked", "Order documents not received"} <= names(
        findings_for("DIST-MOCK-0004")
    )


def test_embargoed_destination(seeded_db):
    assert "Embargoed destination" in names(findings_for("DIST-MOCK-0005"))
