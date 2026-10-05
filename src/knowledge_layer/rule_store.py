"""Stores business rules. Rules persist forever unless a newer rule contradicts them.

A new rule "contradicts" an active one when both have the same domain, the same scope
and check the same set of facts. The older rule is then deleted. This is a simple,
predictable heuristic, not a general conflict solver.
"""

import json
from typing import List, Optional

from src.knowledge_layer.db import get_connection
from src.utils.logging_config import get_logger
from src.utils.models import RouteDecision, Rule, RuleAction
from src.utils.time_helpers import utc_now_iso

logger = get_logger(__name__)

ORDER_VALIDATION = "order_validation"
SPECIAL_INSTRUCTIONS = "special_instructions"
RULE_DOMAINS = [ORDER_VALIDATION, SPECIAL_INSTRUCTIONS]

JENNIFER = "Jennifer (order validation rules reply)"
SEED_RULES = [
    Rule(
        name="Embargoed destination",
        domain=ORDER_VALIDATION,
        action=RuleAction.BLOCK.value,
        conditions=[{"field": "is_embargoed_destination", "op": "eq", "value": True}],
        message="Destination {destination_country} is on the embargoed-country list.",
        source="Legal EUS guide (seed list — confirm with Trade Compliance)",
    ),
    Rule(
        name="Missing shipping method",
        domain=ORDER_VALIDATION,
        action=RuleAction.BLOCK.value,
        conditions=[{"field": "shipping_method", "op": "missing"}],
        message="Shipping method is empty.",
        source="Research plan (mandatory fields)",
    ),
    Rule(
        name="Missing customer PO",
        domain=ORDER_VALIDATION,
        action=RuleAction.BLOCK.value,
        conditions=[{"field": "customer_po", "op": "missing"}],
        message="Customer PO number is empty.",
        source="Research plan (mandatory fields)",
    ),
    Rule(
        name="Missing FOB / incoterm",
        domain=ORDER_VALIDATION,
        action=RuleAction.BLOCK.value,
        conditions=[{"field": "fob", "op": "missing"}],
        message="FOB / incoterm is empty.",
        source="Research plan (mandatory fields)",
    ),
    Rule(
        name="Missing ship-to phone",
        domain=ORDER_VALIDATION,
        action=RuleAction.BLOCK.value,
        conditions=[{"field": "ship_to_phone_present", "op": "eq", "value": False}],
        message="Shipping instructions have no ship-to contact phone number.",
        source="Research plan (mandatory fields)",
    ),
    Rule(
        name="Power-cord count",
        domain=ORDER_VALIDATION,
        action=RuleAction.BLOCK.value,
        conditions=[{"field": "power_cord_count_ok", "op": "eq", "value": False}],
        message=(
            "Expected {expected_power_cord_qty} power cord(s) for the appliances and "
            "power supplies on this order, found {power_cord_qty}."
        ),
        source=JENNIFER + " — TE-2306 and TE-906-2AC ratios inferred, confirm",
    ),
    Rule(
        name="Power-cord region",
        domain=ORDER_VALIDATION,
        action=RuleAction.BLOCK.value,
        conditions=[{"field": "cord_region_ok", "op": "eq", "value": False}],
        message=(
            "Destination {destination_country} needs {expected_cord_region} power cords, "
            "but the order has {power_cord_regions} cords."
        ),
        source=JENNIFER,
    ),
    Rule(
        name="No cord mapping for destination",
        domain=ORDER_VALIDATION,
        action=RuleAction.WARN.value,
        conditions=[{"field": "cord_region_known", "op": "eq", "value": False}],
        message=(
            "No power-cord region is mapped for {destination_country} (order has "
            "{power_cord_regions} cords). Check against Jennifer's cord-to-country map."
        ),
        source="Dry run on Udit's backlog extract",
    ),
    Rule(
        name="SFP+ without a 10GE appliance",
        domain=ORDER_VALIDATION,
        action=RuleAction.BLOCK.value,
        conditions=[{"field": "sfp_compatible", "op": "eq", "value": False}],
        message="Order has {sfp_plus_qty} SFP+ (10GE) transceiver(s) but no 10GE appliance.",
        source=JENNIFER,
    ),
    Rule(
        name="Order documents not received",
        domain=SPECIAL_INSTRUCTIONS,
        action=RuleAction.REQUIRE_DOC.value,
        scope={
            "route": [RouteDecision.SPECIAL_INSTRUCTIONS.value, RouteDecision.EUS.value]
        },
        conditions=[{"field": "documents_received", "op": "eq", "value": False}],
        message=(
            "Waiting for OM's order mail (PO and special instructions). Mail matching is "
            "not automated yet: the MfgOps reviewer confirms by email, then uses "
            "Override & Push."
        ),
        source="Centralized plan, step 6",
    ),
    Rule(
        name="Approved EUS not linked",
        domain=ORDER_VALIDATION,
        action=RuleAction.REQUIRE_DOC.value,
        scope={"route": [RouteDecision.EUS.value]},
        conditions=[{"field": "eus_case_id", "op": "missing"}],
        message=(
            "No approved EUS is linked to this order yet. The MfgOps reviewer confirms "
            "the EUS approval, then uses Override & Push."
        ),
        source="Centralized plan, step 6",
    ),
    Rule(
        name="Blue-ink sign and stamp",
        domain=SPECIAL_INSTRUCTIONS,
        action=RuleAction.WARN.value,
        scope={"destination_country": ["AE", "QA"]},
        conditions=[],
        message=(
            "{destination_country} orders need documents signed and stamped in blue ink. "
            "Include this in the Arrow email."
        ),
        source=JENNIFER,
    ),
]


def row_to_rule(row) -> Rule:
    """Convert a rules-table row into a Rule.

    Args:
        row: A sqlite3.Row from the rules table.

    Returns:
        A Rule.
    """
    return Rule(
        id=row["id"],
        name=row["name"],
        domain=row["domain"],
        action=row["action"],
        message=row["message"],
        scope=json.loads(row["scope_json"]),
        conditions=json.loads(row["conditions_json"]),
        source=row["source"] or "",
        owner=row["owner"] or "",
        version=row["version"],
        attachment_path=row["attachment_path"],
        is_active=bool(row["is_active"]),
        created_at=row["created_at"],
    )


def scope_key(rule: Rule) -> str:
    """Give a canonical text form of a rule's scope, so equal scopes compare equal.

    Args:
        rule: The rule.

    Returns:
        Sorted JSON of the scope, with each allowed-values list sorted too.
    """
    normalized = {
        field: sorted(map(str, values)) for field, values in rule.scope.items()
    }
    return json.dumps(normalized, sort_keys=True)


def condition_fields(rule: Rule) -> frozenset:
    """List the facts a rule's conditions check.

    Args:
        rule: The rule.

    Returns:
        The set of fact names used in its conditions.
    """
    return frozenset(condition["field"] for condition in rule.conditions)


def get_active_rules(domain: Optional[str] = None) -> List[Rule]:
    """Read active rules, optionally for one domain.

    Args:
        domain: A RULE_DOMAINS value, or None for every domain.

    Returns:
        Active rules ordered by id.
    """
    query = "SELECT * FROM rules WHERE is_active = 1"
    params: tuple = ()
    if domain:
        query += " AND domain = ?"
        params = (domain,)
    with get_connection() as connection:
        rows = connection.execute(query + " ORDER BY id", params).fetchall()
    return [row_to_rule(row) for row in rows]


def find_contradicting_rules(rule: Rule) -> List[Rule]:
    """Find active rules that a new rule would replace.

    Args:
        rule: The new rule, not yet saved.

    Returns:
        Active rules with the same domain, scope and checked facts. Rules without
        conditions (reminders) never contradict anything.
    """
    fields = condition_fields(rule)
    if not fields:
        return []
    return [
        existing
        for existing in get_active_rules(rule.domain)
        if scope_key(existing) == scope_key(rule)
        and condition_fields(existing) == fields
    ]


def add_rule(rule: Rule) -> int:
    """Save a new rule, deleting any active rule it contradicts.

    The new rule's version is one higher than the highest version it replaced.

    Args:
        rule: The rule to save.

    Returns:
        The new rule's id.
    """
    replaced = find_contradicting_rules(rule)
    version = max([old.version for old in replaced], default=0) + 1
    with get_connection() as connection:
        for old in replaced:
            connection.execute("DELETE FROM rules WHERE id = ?", (old.id,))
            logger.info(
                "Rule %s (#%s) replaced by new rule %s", old.name, old.id, rule.name
            )
        cursor = connection.execute(
            "INSERT INTO rules (name, domain, action, message, scope_json, conditions_json, "
            "source, owner, version, attachment_path, is_active, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?)",
            (
                rule.name,
                rule.domain,
                rule.action,
                rule.message,
                json.dumps(rule.scope),
                json.dumps(rule.conditions),
                rule.source,
                rule.owner,
                version,
                rule.attachment_path,
                utc_now_iso(),
            ),
        )
        return cursor.lastrowid


def retire_rule(rule_id: int) -> None:
    """Switch a rule off without deleting it (it stays visible for audit).

    Args:
        rule_id: Id of the rule to retire.

    Returns:
        None.
    """
    with get_connection() as connection:
        connection.execute("UPDATE rules SET is_active = 0 WHERE id = ?", (rule_id,))


def seed_default_rules() -> None:
    """Load the seed rules, but only into an empty rules table.

    Returns:
        None.
    """
    with get_connection() as connection:
        has_rules = connection.execute("SELECT 1 FROM rules LIMIT 1").fetchone()
    if has_rules:
        return
    for rule in SEED_RULES:
        add_rule(rule)
    logger.info("Seeded %d default rules", len(SEED_RULES))
