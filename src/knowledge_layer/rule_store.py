"""Stores business rules: add, read, change and retire them.

Rules are never hard-deleted; retiring one switches it off but keeps it for the record.
A new hard rule "contradicts" an active one when both cover the same order categories and
scope and check the same facts; the older one is then retired automatically.
"""

import json
from dataclasses import asdict, fields
from typing import Any, Dict, List, Optional

from src.knowledge_layer.db import get_app_setting, get_connection, set_app_setting
from src.utils.logging_config import get_logger
from src.utils.models import (
    EUS_ROUTES,
    SPECIAL_INSTRUCTION_ROUTES,
    Rule,
    RuleOutcome,
    RuleStatus,
    RuleType,
)
from src.utils.time_helpers import utc_now_iso

logger = get_logger(__name__)

# Rule fields stored as JSON text, and their column names.
JSON_COLUMNS = {
    "applies_to_routes": "applies_to_routes_json",
    "scope": "scope_json",
    "conditions": "conditions_json",
}
BOOLEAN_FIELDS = {"can_be_overridden", "confirmed"}
# Fields a user may change through the Rules Assistant.
EDITABLE_FIELDS = [
    "name",
    "rule_type",
    "outcome",
    "description",
    "category",
    "applies_to_routes",
    "scope",
    "conditions",
    "guidance",
    "message",
    "can_be_overridden",
    "severity",
    "source",
    "owner",
]

JENNIFER = "Jennifer Walker (order validation rules email)"
TRADE_COMPLIANCE = "Seed list — confirm with Trade Compliance"
MANDATORY_FIELDS = "Mandatory order fields (research plan)"
RULES_THREAD = "Raj/Jennifer rules thread, 30 Sep 2026"

CORD_GUIDANCE = (
    "Cord counts are checked by fixed checks (too few = send to OM, too many or mixed "
    "types = hold). Cords needed per unit: TE-1606, TE-2306, TE-4106 and TE-906-HW-2AC = "
    "2 (dual power supplies); TE-1506 and TE-906-HW-AC = 1; each T-PSU600-AC power "
    "supply = 1. Transceivers (SFP, SFP+) never need a cord. If expected_power_cord_qty "
    "is unknown, name the appliance whose cord ratio is missing and hold only if the "
    "count looks wrong."
)
SHIP_FROM_GUIDANCE = (
    "Arrow ships from two places: Phoenix, US (Arrow Electronics, Inc.: US domestic, "
    "Canada, and US-booked exports going through a forwarder in the US) and Budapest, "
    "Hungary (Arrow Central Europe GmbH: EMEA and other international orders, including "
    "standard 30-day APAC orders). Warn only when ship-from clearly doesn't fit, e.g. a "
    "US or Canadian domestic order shipping from Central Europe, or a European order "
    "shipping from Phoenix with no US forwarder. Never reject on this alone."
)


def hard_rule(name: str, outcome: str, description: str, **details) -> Rule:
    """Build a seed hard rule (a fixed check run by code).

    Args:
        name: Rule name.
        outcome: A RuleOutcome value.
        description: Plain-English description.
        **details: Any other Rule field (conditions, message, scope...).

    Returns:
        The Rule.
    """
    return Rule(
        name=name,
        rule_type=RuleType.HARD.value,
        outcome=outcome,
        description=description,
        confirmed=True,
        **details,
    )


def soft_rule(
    name: str, outcome: str, description: str, guidance: str, **details
) -> Rule:
    """Build a seed soft rule (judged by the AI using its guidance).

    Args:
        name: Rule name.
        outcome: The usual RuleOutcome when the rule finds a problem.
        description: Plain-English description.
        guidance: How the AI should judge it.
        **details: Any other Rule field (scope, source...).

    Returns:
        The Rule.
    """
    return Rule(
        name=name,
        rule_type=RuleType.SOFT.value,
        outcome=outcome,
        description=description,
        guidance=guidance,
        **details,
    )


SEED_RULES = [
    hard_rule(
        "Embargoed destination",
        RuleOutcome.REJECT.value,
        "Orders going to an embargoed country can never be released.",
        category="embargo",
        conditions=[{"field": "is_embargoed_destination", "op": "eq", "value": True}],
        message="Destination {destination_country} is an embargoed country.",
        can_be_overridden=False,
        severity="critical",
        source=TRADE_COMPLIANCE,
    ),
    hard_rule(
        "Banned customer",
        RuleOutcome.REJECT.value,
        "Orders for a banned customer can never be released.",
        category="banned_customer",
        conditions=[{"field": "is_banned_customer", "op": "eq", "value": True}],
        message="The customer is on the banned-customer list.",
        can_be_overridden=False,
        severity="critical",
        source=TRADE_COMPLIANCE,
    ),
    hard_rule(
        "Missing shipping method",
        RuleOutcome.REJECT.value,
        "Every order needs a shipping method.",
        category="mandatory_field",
        conditions=[{"field": "shipping_method", "op": "missing"}],
        message="Shipping method is empty.",
        source=MANDATORY_FIELDS,
    ),
    hard_rule(
        "Missing FOB / incoterm",
        RuleOutcome.REJECT.value,
        "Every order needs an FOB / incoterm.",
        category="mandatory_field",
        conditions=[{"field": "fob", "op": "missing"}],
        message="FOB / incoterm is empty.",
        source=MANDATORY_FIELDS,
    ),
    hard_rule(
        "Missing ship-to phone",
        RuleOutcome.REJECT.value,
        "The shipping instructions must include a ship-to contact phone number.",
        category="mandatory_field",
        conditions=[{"field": "ship_to_phone_present", "op": "eq", "value": False}],
        message="Shipping instructions have no ship-to contact phone number.",
        source=MANDATORY_FIELDS,
    ),
    hard_rule(
        "Missing ship-to contact name",
        RuleOutcome.REJECT.value,
        "The shipping instructions must name the ship-to contact (the carrier needs it).",
        category="mandatory_field",
        conditions=[{"field": "ship_to_contact_present", "op": "eq", "value": False}],
        message="Shipping instructions have no 'Ship to Contact:' name.",
        source=RULES_THREAD,
    ),
    hard_rule(
        "Forwarder details for customer-routed freight",
        RuleOutcome.REJECT.value,
        "Freight Collect orders must say who picks them up (forwarder or carrier account).",
        category="shipping",
        conditions=[
            {"field": "customer_routed_freight", "op": "eq", "value": True},
            {"field": "forwarder_details_present", "op": "eq", "value": False},
        ],
        message=(
            "Freight terms are {freight_terms}, but the shipping instructions name no "
            "freight forwarder, carrier account or contact email. Arrow needs them to "
            "arrange the pickup."
        ),
        source=RULES_THREAD,
    ),
    hard_rule(
        "Embargoed region",
        RuleOutcome.REJECT.value,
        "Orders for an embargoed region (Crimea, Donetsk, Luhansk) can never be released.",
        category="embargo",
        conditions=[{"field": "embargoed_region", "op": "exists"}],
        message="The address is in an embargoed region ({embargoed_region}).",
        can_be_overridden=False,
        severity="critical",
        source=RULES_THREAD,
    ),
    hard_rule(
        "Power cord type matches the country",
        RuleOutcome.REJECT.value,
        "Power cords must suit the destination country (e.g. no EU cords on a UK order).",
        category="power_cords",
        conditions=[{"field": "cord_region_ok", "op": "eq", "value": False}],
        message=(
            "{destination_country} needs {expected_cord_region} power cords, "
            "but the order has {power_cord_regions} cords."
        ),
        source=JENNIFER,
    ),
    hard_rule(
        "Power cord type unknown for the country",
        RuleOutcome.WARN.value,
        "Warn when no power cord type is recorded for the destination country.",
        category="power_cords",
        conditions=[{"field": "cord_region_known", "op": "eq", "value": False}],
        message=(
            "No power cord type is recorded for {destination_country} (order has "
            "{power_cord_regions} cords). Please check the cords yourself."
        ),
        source="Dry run on Udit's backlog extract",
    ),
    hard_rule(
        "SFP+ transceivers need a 10GE appliance",
        RuleOutcome.REJECT.value,
        "SFP+ (10GE) transceivers can only ship with a 10GE appliance.",
        category="compatibility",
        conditions=[{"field": "sfp_compatible", "op": "eq", "value": False}],
        message="Order has {sfp_plus_qty} SFP+ (10GE) transceiver(s) but no 10GE appliance.",
        source=JENNIFER + " (FED-1103664 was cancelled and rebooked)",
    ),
    hard_rule(
        "Too few power cords",
        RuleOutcome.REJECT.value,
        "Each appliance and power supply ships with its power cords (e.g. TE-1606 = 2).",
        category="power_cords",
        conditions=[{"field": "too_few_power_cords", "op": "eq", "value": True}],
        message=(
            "The order has {power_cord_qty} power cord(s), but its appliances and power "
            "supplies need {expected_power_cord_qty}."
        ),
        source=JENNIFER,
    ),
    hard_rule(
        "More power cords than needed",
        RuleOutcome.HOLD.value,
        "Hold orders with more power cords than the appliances need, to check with OM.",
        category="power_cords",
        conditions=[{"field": "too_many_power_cords", "op": "eq", "value": True}],
        message=(
            "The order has {power_cord_qty} power cords, but its appliances and power "
            "supplies need {expected_power_cord_qty}. Check with OM whether the extra "
            "cords are intended."
        ),
        source=JENNIFER,
    ),
    hard_rule(
        "Mixed power cord types",
        RuleOutcome.HOLD.value,
        "Hold orders with more than one power cord type, to check with OM.",
        category="power_cords",
        conditions=[{"field": "mixed_power_cord_types", "op": "eq", "value": True}],
        message=(
            "The order has more than one power cord type ({power_cord_regions}). "
            "Check with OM."
        ),
        source=JENNIFER,
    ),
    hard_rule(
        "Power supply fits the appliance",
        RuleOutcome.HOLD.value,
        "T-PSU600-AC fits TE-1506, TE-1606, TE-2205 and TE-2306 only.",
        category="compatibility",
        conditions=[{"field": "psu_compatible", "op": "eq", "value": False}],
        message=(
            "The order has {psu_qty} T-PSU600-AC power supply(ies), but none of its "
            "appliances takes them (they fit TE-1506, TE-1606, TE-2205 and TE-2306)."
        ),
        source=RULES_THREAD,
    ),
    hard_rule(
        "Approved EUS needed",
        RuleOutcome.HOLD.value,
        "EUS orders are held until an approved End-Use Statement is linked.",
        category="eus",
        applies_to_routes=EUS_ROUTES,
        conditions=[{"field": "eus_case_id", "op": "missing"}],
        message=(
            "No approved EUS is linked to this order yet. If the EUS was already "
            "checked and sent to Arrow by email, you can approve it."
        ),
        source=JENNIFER,
    ),
    hard_rule(
        "OM's order email needed",
        RuleOutcome.HOLD.value,
        "Special-instruction orders wait for OM's email with the PO and instructions.",
        category="documents",
        applies_to_routes=SPECIAL_INSTRUCTION_ROUTES,
        conditions=[{"field": "documents_received", "op": "eq", "value": False}],
        message=(
            "Waiting for OM's order email (PO and special instructions). Matching it "
            "isn't automated yet: once you've confirmed it by email, you can approve."
        ),
        source="Centralized plan, step 6",
    ),
    soft_rule(
        "Power cords per appliance",
        RuleOutcome.REJECT.value,
        "Each appliance and power supply ships with the right number of power cords.",
        guidance=CORD_GUIDANCE,
        category="power_cords",
        source=JENNIFER,
    ),
    soft_rule(
        "Ship-from and ship-to look right",
        RuleOutcome.WARN.value,
        "Ship-from and ship-to are checked on every order.",
        guidance=SHIP_FROM_GUIDANCE,
        category="shipping",
        source=RULES_THREAD,
    ),
    soft_rule(
        "Blue-ink sign and stamp (UAE, Qatar)",
        RuleOutcome.WARN.value,
        "UAE and Qatar orders need documents signed and stamped in blue ink.",
        guidance=(
            "Remind the reviewer that the documents need a sign and stamp in blue ink "
            "and that a follow-up email is needed. This alone is not a reason to reject."
        ),
        category="special_instructions",
        scope={"destination_country": ["AE", "QA"]},
        source=JENNIFER,
    ),
    soft_rule(
        "Special shipping comments go in the Arrow email",
        RuleOutcome.WARN.value,
        "Special shipping comments must be passed on to Arrow.",
        guidance=(
            "If the shipping instructions contain special comments (carrier account "
            "number, no partial shipment, ECCN / HS code / country of origin on invoices, "
            "a freight forwarder or contact to call first), list them so the reviewer "
            "includes them in the Arrow email."
        ),
        category="special_instructions",
        applies_to_routes=SPECIAL_INSTRUCTION_ROUTES,
        source=JENNIFER,
    ),
    soft_rule(
        "Letter-of-credit orders are held",
        RuleOutcome.HOLD.value,
        "Letter-of-credit (L/C) orders, e.g. Expernet South Korea, are handled by hand.",
        guidance=(
            "L/C orders need a custom commercial invoice that matches the letter of "
            "credit to the penny, which is out of scope for now: always hold them for the "
            "reviewer. Signs of an L/C order: 'L/C', 'LC' or 'letter of credit' in the "
            "order text, or an Expernet order to South Korea unless it is clearly a normal "
            "30-day order (those ship from Hungary like any international order)."
        ),
        category="complex_order",
        source=JENNIFER,
    ),
    soft_rule(
        "1GbE SFP transceivers need SFP slots",
        RuleOutcome.HOLD.value,
        "IB-SFP-SX and IB-SFP-CO only fit appliances with 1GbE SFP slots.",
        guidance=(
            "If the order has IB-SFP-SX or IB-SFP-CO transceivers, they need an appliance "
            "with 1GbE SFP slots; models with RJ-45 copper ports only can't take them. "
            "If it isn't clear that the appliance on the order has SFP slots, hold so "
            "the reviewer checks with OM."
        ),
        category="compatibility",
        source=RULES_THREAD,
    ),
]

# Seed rules added after the first version; an existing database gets them once (a rule
# with the same name, even retired, is never added again).
RULES_SEED_VERSION = 2
SEED_VERSION_SETTING = "rules_seed_version"
ADDED_IN_SEED_VERSION = {
    2: [
        "Missing ship-to contact name",
        "Forwarder details for customer-routed freight",
        "Embargoed region",
        "Too few power cords",
        "More power cords than needed",
        "Mixed power cord types",
        "Power supply fits the appliance",
        "1GbE SFP transceivers need SFP slots",
    ]
}
# Seed rules whose content changed in a later version. Only applied to a seed rule that
# nobody has edited since it was loaded (still version 1).
CHANGED_IN_SEED_VERSION = {
    2: {
        "Power cords per appliance": {"guidance": CORD_GUIDANCE},
        "Ship-from and ship-to look right": {
            "guidance": SHIP_FROM_GUIDANCE,
            "source": RULES_THREAD,
        },
    }
}


def row_to_rule(row) -> Rule:
    """Convert a rules-table row into a Rule.

    Args:
        row: A sqlite3.Row from the rules table.

    Returns:
        A Rule.
    """
    data = {}
    for rule_field in fields(Rule):
        name = rule_field.name
        if name in JSON_COLUMNS:
            data[name] = json.loads(row[JSON_COLUMNS[name]] or "null") or (
                {} if name == "scope" else []
            )
        elif name in BOOLEAN_FIELDS:
            data[name] = bool(row[name])
        else:
            value = row[name]
            data[name] = "" if value is None and rule_field.default == "" else value
    return Rule(**data)


def rule_to_columns(rule: Rule) -> Dict[str, Any]:
    """Convert a Rule into column values (without id).

    Args:
        rule: The rule.

    Returns:
        A dict of column name to value, ready to insert or update.
    """
    columns = {}
    for name, value in asdict(rule).items():
        if name == "id":
            continue
        if name in JSON_COLUMNS:
            columns[JSON_COLUMNS[name]] = json.dumps(value)
        elif name in BOOLEAN_FIELDS:
            columns[name] = int(bool(value))
        else:
            columns[name] = value
    return columns


def get_rules(status: Optional[str] = RuleStatus.ACTIVE.value) -> List[Rule]:
    """Read rules, by default only the active ones.

    Args:
        status: A RuleStatus value, or None for every rule.

    Returns:
        Rules ordered by id.
    """
    query, params = "SELECT * FROM rules", ()
    if status:
        query, params = query + " WHERE status = ?", (status,)
    with get_connection() as connection:
        rows = connection.execute(query + " ORDER BY id", params).fetchall()
    return [row_to_rule(row) for row in rows]


def get_rule(rule_id: int) -> Optional[Rule]:
    """Read one rule.

    Args:
        rule_id: The rule's id.

    Returns:
        The Rule, or None if there is no such rule.
    """
    with get_connection() as connection:
        row = connection.execute(
            "SELECT * FROM rules WHERE id = ?", (rule_id,)
        ).fetchone()
    return row_to_rule(row) if row else None


def same_coverage_key(rule: Rule) -> str:
    """Describe which orders and facts a hard rule covers, so overlaps compare equal.

    Args:
        rule: The rule.

    Returns:
        Canonical JSON of its categories, scope and checked facts.
    """
    return json.dumps(
        {
            "routes": sorted(rule.applies_to_routes),
            "scope": {
                key: sorted(map(str, values)) for key, values in rule.scope.items()
            },
            "facts": sorted(condition["field"] for condition in rule.conditions),
        },
        sort_keys=True,
    )


def find_contradicting_rules(rule: Rule) -> List[Rule]:
    """Find active hard rules that a new hard rule would replace.

    Args:
        rule: The new rule, not yet saved.

    Returns:
        Active hard rules covering the same orders and checking the same facts.
    """
    if rule.rule_type != RuleType.HARD.value or not rule.conditions:
        return []
    key = same_coverage_key(rule)
    return [
        existing
        for existing in get_rules()
        if existing.id != rule.id
        and existing.rule_type == RuleType.HARD.value
        and same_coverage_key(existing) == key
    ]


def add_rule(rule: Rule) -> int:
    """Save a new rule, retiring any active hard rule it contradicts.

    Args:
        rule: The rule to save.

    Returns:
        The new rule's id.
    """
    replaced = find_contradicting_rules(rule)
    now = utc_now_iso()
    rule.version = max([old.version for old in replaced], default=0) + 1
    rule.created_at = rule.created_at or now
    rule.updated_at = now
    columns = rule_to_columns(rule)
    with get_connection() as connection:
        for old in replaced:
            connection.execute(
                "UPDATE rules SET status = ?, updated_at = ? WHERE id = ?",
                (RuleStatus.RETIRED.value, now, old.id),
            )
            logger.info("Rule %s (#%s) replaced by %s", old.name, old.id, rule.name)
        cursor = connection.execute(
            f"INSERT INTO rules ({', '.join(columns)}) "
            f"VALUES ({', '.join('?' for _ in columns)})",
            list(columns.values()),
        )
        return cursor.lastrowid


def update_rule(rule_id: int, changes: Dict[str, Any]) -> Rule:
    """Change some fields of an existing rule and bump its version.

    Args:
        rule_id: The rule's id.
        changes: Field name to new value; only EDITABLE_FIELDS are allowed.

    Returns:
        The updated Rule.
    """
    rule = get_rule(rule_id)
    if rule is None:
        raise ValueError(f"There is no rule #{rule_id}.")
    unknown = set(changes) - set(EDITABLE_FIELDS)
    if unknown:
        raise ValueError(
            f"These fields can't be changed: {', '.join(sorted(unknown))}."
        )
    for name, value in changes.items():
        setattr(rule, name, value)
    rule.version += 1
    rule.updated_at = utc_now_iso()
    columns = rule_to_columns(rule)
    with get_connection() as connection:
        connection.execute(
            f"UPDATE rules SET {', '.join(f'{name} = ?' for name in columns)} WHERE id = ?",
            [*columns.values(), rule_id],
        )
    return rule


def retire_rule(rule_id: int) -> None:
    """Switch a rule off without deleting it (it stays visible for audit).

    Args:
        rule_id: Id of the rule to retire.

    Returns:
        None.
    """
    with get_connection() as connection:
        connection.execute(
            "UPDATE rules SET status = ?, updated_at = ? WHERE id = ?",
            (RuleStatus.RETIRED.value, utc_now_iso(), rule_id),
        )


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
        add_rule(Rule(**asdict(rule)))
    set_app_setting(SEED_VERSION_SETTING, str(RULES_SEED_VERSION))
    logger.info("Seeded %d default rules", len(SEED_RULES))


def upgrade_seed_rules() -> None:
    """Bring an existing database's rules up to the current seed version.

    New seed rules are added; changed seed rules are updated only if nobody edited them.

    Returns:
        None.
    """
    stored = int(get_app_setting(SEED_VERSION_SETTING) or 1)
    if stored >= RULES_SEED_VERSION:
        return
    seeds_by_name = {rule.name: rule for rule in SEED_RULES}
    for version in range(stored + 1, RULES_SEED_VERSION + 1):
        known_names = {rule.name for rule in get_rules(status=None)}
        for name in ADDED_IN_SEED_VERSION.get(version, []):
            if name not in known_names:
                add_rule(Rule(**asdict(seeds_by_name[name])))
        for name, changes in CHANGED_IN_SEED_VERSION.get(version, {}).items():
            for rule in get_rules(status=None):
                if rule.name == name and rule.version == 1:
                    update_rule(rule.id, changes)
    set_app_setting(SEED_VERSION_SETTING, str(RULES_SEED_VERSION))
    logger.info("Rules upgraded to seed version %d", RULES_SEED_VERSION)
