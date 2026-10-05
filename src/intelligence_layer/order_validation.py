"""Deterministic rules engine. It alone decides pass/fail; the LLM only explains.

How it works:
1. build_order_facts() turns an order into named facts (e.g. power_cord_count_ok).
   Adding a new kind of check that needs new data = add a fact here (code change).
2. Rules in the database say "if the order is in <scope> and <conditions> on facts
   are true, report <message>". Adding a rule over existing facts = no code change.
"""

import re
from typing import Any, Dict, List, Optional

from src.intelligence_layer import llm_assist
from src.intelligence_layer.order_router import get_destination_country
from src.knowledge_layer import order_store, reference_store, rule_store
from src.utils.logging_config import get_logger
from src.utils.models import (
    AiRecommendation,
    Order,
    OrderStatus,
    Rule,
    RuleAction,
    ValidationResult,
)

logger = get_logger(__name__)

POWER_CORD_SKU_PREFIX = "IB-POWER-CORD"
SFP_PLUS_SKU_PREFIX = "IB-SFPPLUS"
CORD_NEEDING_ITEM_CLASSES = {"HARDWARE BASE", "POWER SUPPLY"}
PHONE_PATTERN = re.compile(r"phone[^0-9+]*\+?[\d][\d\s\-().]{5,}", re.IGNORECASE)
SUPPORTED_OPERATORS = ["eq", "neq", "in", "not_in", "exists", "missing", "gt", "lt"]

# Every fact a rule can use, with a description shown in the Knowledge Addition tab.
# Keep in sync with build_order_facts (a test checks this).
FACT_DESCRIPTIONS = {
    "order_no": "Fusion order number",
    "order_type": "Order type, e.g. Renewal, Distributor, Federal",
    "route": "standard | special_instructions | eus",
    "ship_to_country_code": "Ship-to ISO country code",
    "end_customer_country": "End-customer ISO country code (distributor orders)",
    "destination_country": "End-customer country if set, else ship-to country",
    "bill_to_customer": "Bill-to customer name",
    "ship_to_customer": "Ship-to customer name",
    "shipping_method": "Shipping method / carrier",
    "fob": "FOB / incoterm",
    "freight_terms": "Freight terms",
    "customer_po": "Customer PO number",
    "ship_to_phone_present": "True if shipping instructions contain a phone number",
    "is_embargoed_destination": "True if destination is on the embargoed list",
    "power_cord_qty": "Total power cords on the order",
    "expected_power_cord_qty": "Cords needed by appliances and PSUs (None if unknown)",
    "power_cord_count_ok": "True/False, or None when a ratio is unknown",
    "power_cord_regions": "Cord regions on the order, e.g. 'US' or 'EU, US'",
    "expected_cord_region": "Cord region the destination needs (None if unmapped)",
    "cord_region_known": "False when there are cords but the destination is unmapped",
    "cord_region_ok": "True/False, or None when it can't be checked",
    "sfp_plus_qty": "Total SFP+ (10GE) transceivers",
    "has_10ge_appliance": "True if any appliance has 10GE ports",
    "sfp_compatible": "False when SFP+ is ordered without a 10GE appliance",
    "documents_received": "True once OM's order mail is matched (not automated yet)",
    "eus_case_id": "Linked approved EUS case, if any",
}


def match_sku_prefix(sku: str, prefixes: Dict[str, str]) -> Optional[str]:
    """Find the longest prefix in a reference list that a SKU starts with.

    Args:
        sku: The item SKU.
        prefixes: Reference list keyed by SKU prefix.

    Returns:
        The matching entry's value, or None if no prefix matches.
    """
    matches = [prefix for prefix in prefixes if sku.upper().startswith(prefix.upper())]
    return prefixes[max(matches, key=len)] if matches else None


def build_power_cord_facts(
    order: Order, lists: Dict[str, Dict[str, str]]
) -> Dict[str, Any]:
    """Work out power-cord count and region facts for an order.

    Args:
        order: The order.
        lists: All reference lists.

    Returns:
        The power-cord facts (see FACT_DESCRIPTIONS).
    """
    cord_lines = [
        line
        for line in order.lines
        if line.sku.upper().startswith(POWER_CORD_SKU_PREFIX)
    ]
    power_cord_qty = sum(line.ordered_qty for line in cord_lines)

    expected: Optional[int] = 0
    for line in order.lines:
        if line.item_class.upper() not in CORD_NEEDING_ITEM_CLASSES:
            continue
        ratio = match_sku_prefix(
            line.sku, lists.get(reference_store.POWER_CORD_RATIO, {})
        )
        if ratio is None:
            expected = None  # One unknown ratio makes the whole count uncheckable.
            break
        expected += int(ratio) * line.ordered_qty

    regions = sorted({line.sku.rsplit("-", 1)[-1].upper() for line in cord_lines})
    destination = get_destination_country(order)
    expected_region = lists.get(reference_store.COUNTRY_CORD_REGION, {}).get(
        destination
    )
    has_cords = bool(cord_lines)
    return {
        "power_cord_qty": power_cord_qty,
        "expected_power_cord_qty": expected,
        "power_cord_count_ok": None if expected is None else power_cord_qty == expected,
        "power_cord_regions": ", ".join(regions) or "no",
        "expected_cord_region": expected_region,
        "cord_region_known": (expected_region is not None) if has_cords else None,
        "cord_region_ok": (
            all(region == expected_region for region in regions)
            if has_cords and expected_region
            else None
        ),
    }


def build_order_facts(order: Order, lists: Dict[str, Dict[str, str]]) -> Dict[str, Any]:
    """Turn an order into the named facts that rules check.

    Args:
        order: The order.
        lists: All reference lists (from reference_store.get_all_reference_lists).

    Returns:
        A dict with exactly the keys in FACT_DESCRIPTIONS.
    """
    destination = get_destination_country(order)
    sfp_qty = sum(
        line.ordered_qty
        for line in order.lines
        if line.sku.upper().startswith(SFP_PLUS_SKU_PREFIX)
    )
    ten_ge_list = lists.get(reference_store.TEN_GE_APPLIANCES, {})
    has_10ge = any(
        match_sku_prefix(line.sku, ten_ge_list) is not None
        for line in order.lines
        if line.item_class.upper() == "HARDWARE BASE"
    )
    facts = {
        "order_no": order.order_no,
        "order_type": order.order_type,
        "route": order.route,
        "ship_to_country_code": order.ship_to_country_code.upper(),
        "end_customer_country": order.end_customer_country.upper(),
        "destination_country": destination or None,
        "bill_to_customer": order.bill_to_customer,
        "ship_to_customer": order.ship_to_customer,
        "shipping_method": order.shipping_method,
        "fob": order.fob,
        "freight_terms": order.freight_terms,
        "customer_po": order.customer_po,
        "ship_to_phone_present": bool(
            PHONE_PATTERN.search(order.shipping_instructions or "")
        ),
        "is_embargoed_destination": (
            destination in lists.get(reference_store.EMBARGOED_COUNTRIES, {})
            if destination
            else None
        ),
        "sfp_plus_qty": sfp_qty,
        "has_10ge_appliance": has_10ge,
        "sfp_compatible": has_10ge if sfp_qty else None,
        "documents_received": order.documents_received,
        "eus_case_id": order.eus_case_id,
    }
    facts.update(build_power_cord_facts(order, lists))
    return facts


def is_blank(value: Any) -> bool:
    """Tell whether a fact counts as empty.

    Args:
        value: The fact value.

    Returns:
        True for None, empty or whitespace-only strings, and empty lists.
    """
    return (
        value is None or (isinstance(value, str) and not value.strip()) or value == []
    )


def evaluate_condition(facts: Dict[str, Any], condition: Dict[str, Any]) -> bool:
    """Check one rule condition against an order's facts.

    A fact that is None (unknown) never satisfies eq/neq/in/not_in/gt/lt, so a check
    that can't be computed never produces a finding.

    Args:
        facts: Output of build_order_facts.
        condition: {"field": <fact>, "op": <SUPPORTED_OPERATORS>, "value": <optional>}.

    Returns:
        True if the condition holds.
    """
    actual = facts.get(condition["field"])
    op = condition["op"]
    expected = condition.get("value")
    if op == "exists":
        return not is_blank(actual)
    if op == "missing":
        return is_blank(actual)
    if actual is None:
        return False
    if op == "eq":
        return actual == expected
    if op == "neq":
        return actual != expected
    if op == "in":
        return actual in expected
    if op == "not_in":
        return actual not in expected
    if op == "gt":
        return actual > expected
    if op == "lt":
        return actual < expected
    raise ValueError(f"Unsupported operator {op!r}; use one of {SUPPORTED_OPERATORS}")


def rule_applies(facts: Dict[str, Any], rule: Rule) -> bool:
    """Check whether an order falls inside a rule's scope.

    Args:
        facts: Output of build_order_facts.
        rule: The rule; an empty scope applies to every order.

    Returns:
        True if every scope field's fact is one of its allowed values.
    """
    return all(facts.get(field) in allowed for field, allowed in rule.scope.items())


class MissingAsPlaceholder(dict):
    """Dict for str.format_map that leaves unknown {placeholders} as they are."""

    def __missing__(self, key: str) -> str:
        """Return the placeholder unchanged.

        Args:
            key: The missing placeholder name.

        Returns:
            "{key}".
        """
        return "{" + key + "}"


def render_message(template: str, facts: Dict[str, Any]) -> str:
    """Fill a rule message's {fact} placeholders.

    Args:
        template: The rule's message.
        facts: Output of build_order_facts.

    Returns:
        The message with known placeholders filled in.
    """
    try:
        return template.format_map(MissingAsPlaceholder(facts))
    except (ValueError, IndexError):
        return (
            template  # Malformed braces in a user-written message: show it as written.
        )


def validate_order(
    order: Order, rules: List[Rule], lists: Dict[str, Dict[str, str]]
) -> List[ValidationResult]:
    """Run every rule against one order.

    Args:
        order: The order (with route set).
        rules: Active rules.
        lists: All reference lists.

    Returns:
        One ValidationResult per rule that fired.
    """
    facts = build_order_facts(order, lists)
    return [
        ValidationResult(
            order_no=order.order_no,
            rule_id=rule.id,
            rule_name=rule.name,
            action=rule.action,
            message=render_message(rule.message, facts),
        )
        for rule in rules
        if rule_applies(facts, rule)
        and all(evaluate_condition(facts, condition) for condition in rule.conditions)
    ]


def recommend(findings: List[ValidationResult]) -> AiRecommendation:
    """Turn findings into a recommendation: any block or missing document rejects.

    Args:
        findings: The order's findings.

    Returns:
        AiRecommendation.REJECT or AiRecommendation.APPROVE.
    """
    rejecting = {RuleAction.BLOCK.value, RuleAction.REQUIRE_DOC.value}
    if any(finding.action in rejecting for finding in findings):
        return AiRecommendation.REJECT
    return AiRecommendation.APPROVE


def validate_pending_orders() -> int:
    """Validate every order waiting for validation and queue it for a human.

    Orders missing a required document go to WAITING_ON_OM; the rest to IN_REVIEW.

    Returns:
        How many orders were validated.
    """
    orders = order_store.get_active_orders([OrderStatus.ROUTED.value])
    if not orders:
        return 0
    rules = rule_store.get_active_rules()
    lists = reference_store.get_all_reference_lists()
    for order in orders:
        findings = validate_order(order, rules, lists)
        waiting = any(
            finding.action == RuleAction.REQUIRE_DOC.value for finding in findings
        )
        order_store.save_validation(
            order.order_no,
            findings,
            recommend(findings).value,
            llm_assist.explain_discrepancies(order, findings),
            OrderStatus.WAITING_ON_OM.value if waiting else OrderStatus.IN_REVIEW.value,
        )
    logger.info("Validated %d order(s)", len(orders))
    return len(orders)
