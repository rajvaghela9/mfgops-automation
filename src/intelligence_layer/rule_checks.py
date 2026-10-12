"""Runs the fixed (hard) rules and picks which rules apply to an order.

How it works:
1. build_order_facts() turns an order into named facts (e.g. cord_region_ok).
   A new kind of check that needs new data = add a fact here (code change).
2. Every rule has a scope (which orders it is about). Hard rules also have conditions
   on facts and are checked here, always with the same answer. Soft rules are passed
   to the AI judge with their guidance.
"""

import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from src.intelligence_layer.order_router import get_destination_country
from src.knowledge_layer import reference_store
from src.utils.models import (
    Order,
    OrderRoute,
    Rule,
    RuleFinding,
    RuleOutcome,
    RuleType,
    Verdict,
)

POWER_CORD_SKU_PREFIX = "IB-POWER-CORD"
SFP_PLUS_SKU_PREFIX = "IB-SFPPLUS"
# Fusion calls these "Hardware Base Units" and "Power Supply Units"; match on the start.
CORD_NEEDING_ITEM_CLASSES = ("HARDWARE BASE", "POWER SUPPLY")
APPLIANCE_ITEM_CLASS = "HARDWARE BASE"
PHONE_PATTERN = re.compile(r"phone[^0-9+]*\+?[\d][\d\s\-().]{5,}", re.IGNORECASE)
SUPPORTED_OPERATORS = ["eq", "neq", "in", "not_in", "exists", "missing", "gt", "lt"]

# Every fact a rule can use. Keep in sync with build_order_facts (a test checks this).
FACT_DESCRIPTIONS = {
    "order_no": "Fusion order number",
    "order_type": "Fusion order type, e.g. Renewal, Distributor, Federal",
    "route": "Order category: " + ", ".join(route.value for route in OrderRoute),
    "ship_to_country_code": "Ship-to ISO country code",
    "end_customer_country": "End-customer ISO country code (distributor orders)",
    "destination_country": "End-customer country if set, else ship-to country",
    "bill_to_customer": "Bill-to customer name",
    "ship_to_customer": "Ship-to customer name",
    "ship_from": "Where the order ships from",
    "shipping_method": "Shipping method / carrier",
    "fob": "FOB / incoterm",
    "freight_terms": "Freight terms",
    "customer_po": "Customer PO number",
    "ship_to_phone_present": "True if shipping instructions contain a phone number",
    "is_embargoed_destination": "True if destination is on the embargoed list",
    "is_banned_customer": "True if the bill-to or ship-to customer is on the banned list",
    "power_cord_qty": "Total power cords on the order",
    "expected_power_cord_qty": "Cords needed by appliances and PSUs (None if unknown)",
    "power_cord_count_ok": "True/False, or None when a ratio is unknown",
    "power_cord_regions": "Cord regions on the order, e.g. 'US' or 'EU, US'",
    "expected_cord_region": "Cord region the destination needs (None if unmapped)",
    "cord_region_known": "False when there are cords but the destination is unmapped",
    "cord_region_ok": "True/False, or None when it can't be checked",
    "sfp_plus_qty": "Total SFP+ (10GE) transceivers",
    "has_10ge_appliance": "True if an appliance has 10GE ports ('10GE' in SKU or on the list)",
    "sfp_compatible": "False when SFP+ is ordered without a 10GE appliance",
    "documents_received": "True once OM's order mail is matched (not automated yet)",
    "eus_case_id": "Linked approved EUS case, if any",
}

# Strictest first: one rejecting finding outweighs any number of holds.
VERDICT_STRICTNESS = [Verdict.REJECT.value, Verdict.HOLD.value, Verdict.APPROVE.value]


@dataclass
class RuleCheckResult:
    """Everything the fixed checks found out about one order."""

    facts: Dict[str, Any]
    findings: List[RuleFinding] = field(default_factory=list)
    judgement_rules: List[Rule] = field(default_factory=list)


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
        if not line.item_class.upper().startswith(CORD_NEEDING_ITEM_CLASSES):
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
        "10GE" in line.sku.upper()
        or match_sku_prefix(line.sku, ten_ge_list) is not None
        for line in order.lines
        if line.item_class.upper().startswith(APPLIANCE_ITEM_CLASS)
    )
    customers = f"{order.bill_to_customer} {order.ship_to_customer}".upper()
    facts = {
        "order_no": order.order_no,
        "order_type": order.order_type,
        "route": order.route,
        "ship_to_country_code": order.ship_to_country_code.upper(),
        "end_customer_country": order.end_customer_country.upper(),
        "destination_country": destination or None,
        "bill_to_customer": order.bill_to_customer,
        "ship_to_customer": order.ship_to_customer,
        "ship_from": order.ship_from,
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
        "is_banned_customer": any(
            banned in customers
            for banned in lists.get(reference_store.BANNED_CUSTOMERS, {})
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
    """Check whether an order is one the rule is about (its category and scope).

    Args:
        facts: Output of build_order_facts.
        rule: The rule; no categories and no scope means every order.

    Returns:
        True if the rule applies to this order.
    """
    if rule.applies_to_routes and facts.get("route") not in rule.applies_to_routes:
        return False
    return all(facts.get(name) in allowed for name, allowed in rule.scope.items())


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
        return template  # Malformed braces in a user-written message: show as written.


def check_order_against_rules(
    order: Order, rules: List[Rule], lists: Dict[str, Dict[str, str]]
) -> RuleCheckResult:
    """Run every hard rule on an order and collect the soft rules that apply to it.

    Args:
        order: The order (with route set).
        rules: Active rules.
        lists: All reference lists.

    Returns:
        The order's facts, the hard-rule findings and the soft rules for the AI.
    """
    facts = build_order_facts(order, lists)
    result = RuleCheckResult(facts=facts)
    for rule in rules:
        if not rule_applies(facts, rule):
            continue
        if rule.rule_type == RuleType.SOFT.value:
            result.judgement_rules.append(rule)
        elif all(evaluate_condition(facts, condition) for condition in rule.conditions):
            result.findings.append(
                RuleFinding(
                    rule_id=rule.id,
                    rule_name=rule.name,
                    outcome=rule.outcome,
                    message=render_message(rule.message or rule.description, facts),
                    can_be_overridden=rule.can_be_overridden,
                )
            )
    return result


def stricter_verdict(first: str, second: str) -> str:
    """Pick the stricter of two verdicts (reject beats hold beats approve).

    Args:
        first: A Verdict value.
        second: A Verdict value.

    Returns:
        Whichever is stricter.
    """
    return min(first, second, key=VERDICT_STRICTNESS.index)


def verdict_from_findings(findings: List[RuleFinding]) -> str:
    """Turn hard-rule findings into the minimum verdict they force.

    Args:
        findings: Hard rules that found a problem.

    Returns:
        "reject" if any finding rejects, "hold" if any holds, else "approve".
    """
    verdict = Verdict.APPROVE.value
    for finding in findings:
        if finding.outcome == RuleOutcome.REJECT.value:
            verdict = stricter_verdict(verdict, Verdict.REJECT.value)
        elif finding.outcome == RuleOutcome.HOLD.value:
            verdict = stricter_verdict(verdict, Verdict.HOLD.value)
    return verdict


def find_release_blocker(findings: List[RuleFinding]) -> Optional[str]:
    """Tell whether a finding forbids release even when a reviewer approves.

    Args:
        findings: The order's hard-rule findings.

    Returns:
        The blocking finding's message (e.g. embargoed destination), or None.
    """
    for finding in findings:
        if (
            finding.outcome == RuleOutcome.REJECT.value
            and not finding.can_be_overridden
        ):
            return finding.message
    return None


def find_rule_problems(rule: Rule) -> List[str]:
    """Check that a rule is complete and that its checks use known facts.

    Args:
        rule: A rule about to be saved.

    Returns:
        Readable problems; an empty list means the rule is fine.
    """
    problems = []
    if not rule.name.strip():
        problems.append("The rule needs a name.")
    if not rule.description.strip():
        problems.append("The rule needs a plain-English description.")
    if rule.rule_type not in [kind.value for kind in RuleType]:
        problems.append("rule_type must be 'hard' or 'soft'.")
    if rule.outcome not in [outcome.value for outcome in RuleOutcome]:
        problems.append("outcome must be 'reject', 'hold' or 'warn'.")
    for route in rule.applies_to_routes:
        if route not in [known.value for known in OrderRoute]:
            problems.append(f"Unknown order category {route!r}.")
    for name in rule.scope:
        if name not in FACT_DESCRIPTIONS:
            problems.append(f"Unknown fact {name!r} in scope.")
    if rule.rule_type == RuleType.HARD.value:
        if not rule.conditions:
            problems.append(
                "A hard rule needs conditions; if it can't be written as a fixed "
                "check on the known facts, make it a soft rule with guidance."
            )
        for condition in rule.conditions:
            if condition.get("field") not in FACT_DESCRIPTIONS:
                problems.append(f"Unknown fact {condition.get('field')!r}.")
            if condition.get("op") not in SUPPORTED_OPERATORS:
                problems.append(f"Unknown operator {condition.get('op')!r}.")
    for placeholder in re.findall(r"\{(\w+)\}", rule.message or ""):
        if placeholder not in FACT_DESCRIPTIONS:
            problems.append(
                f"The message uses {{{placeholder}}}, which isn't a known fact; use a fact "
                "name such as {shipping_method}, or plain text."
            )
    if rule.rule_type == RuleType.SOFT.value and not rule.guidance.strip():
        problems.append("A soft rule needs guidance telling the AI how to judge it.")
    return problems
