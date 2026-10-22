"""Shared data shapes (orders, rules, findings) and the status values they use."""

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional


class OrderRoute(str, Enum):
    """The four order categories. Each one needs different checks before release."""

    STANDARD = "standard"
    SPECIAL_INSTRUCTIONS = "special_instructions"
    EUS = "eus"
    EUS_AND_SPECIAL_INSTRUCTIONS = "eus_and_special_instructions"


ROUTE_LABELS = {
    OrderRoute.STANDARD.value: "Standard",
    OrderRoute.SPECIAL_INSTRUCTIONS.value: "Special Instructions",
    OrderRoute.EUS.value: "EUS",
    OrderRoute.EUS_AND_SPECIAL_INSTRUCTIONS.value: "EUS + Special Instructions",
}
EUS_ROUTES = [OrderRoute.EUS.value, OrderRoute.EUS_AND_SPECIAL_INSTRUCTIONS.value]
SPECIAL_INSTRUCTION_ROUTES = [
    OrderRoute.SPECIAL_INSTRUCTIONS.value,
    OrderRoute.EUS_AND_SPECIAL_INSTRUCTIONS.value,
]


class OrderStatus(str, Enum):
    """Where an order is. RELEASED orders live only in the all_orders table.

    APPROVED means a MfgOps user approved the order and released it in Fusion by hand;
    it stays active until Fusion's USPO for it shows up in a backlog report mail.
    REMOVED means the order disappeared from the Fusion backlog (e.g. cancelled).
    """

    NEW = "NEW"
    WAITING_FOR_REVIEW = "WAITING_FOR_REVIEW"
    ON_HOLD = "ON_HOLD"
    SENT_TO_OM = "SENT_TO_OM"
    APPROVED = "APPROVED"
    RELEASED = "RELEASED"
    REMOVED = "REMOVED"


REVIEWABLE_STATUSES = [OrderStatus.WAITING_FOR_REVIEW.value, OrderStatus.ON_HOLD.value]

# Invented orders used to show the AI rejecting or holding orders. They never exist in
# Fusion, so they are released straight away when approved (there is no USPO to wait for).
DUMMY_ORDER_PREFIX = "dummy_"


def is_dummy_order(order_no: str) -> bool:
    """Tell whether an order number belongs to one of the invented demo orders.

    Args:
        order_no: Order number.

    Returns:
        True for dummy orders (their number starts with "dummy_").
    """
    return (order_no or "").startswith(DUMMY_ORDER_PREFIX)


class Verdict(str, Enum):
    """The AI's verdict on an order."""

    APPROVE = "approve"
    REJECT = "reject"
    HOLD = "hold"


class ReviewerDecision(str, Enum):
    """The button a MfgOps user clicked."""

    APPROVE = "approve"
    SEND_TO_OM = "send_to_om"
    HOLD = "hold"


class RuleType(str, Enum):
    """Hard rules are fixed checks run by code; soft rules are judged by the AI."""

    HARD = "hard"
    SOFT = "soft"


class RuleOutcome(str, Enum):
    """What happens to an order when a rule finds a problem."""

    REJECT = "reject"
    HOLD = "hold"
    WARN = "warn"


OUTCOME_LABELS = {
    RuleOutcome.REJECT.value: "Send back to OM",
    RuleOutcome.HOLD.value: "Hold for the reviewer",
    RuleOutcome.WARN.value: "Warning only",
}


class RuleStatus(str, Enum):
    """Only active rules are applied; retired rules are kept for the record."""

    ACTIVE = "active"
    RETIRED = "retired"


@dataclass
class OrderLine:
    """One line of a Fusion order; fields mirror the backlog report columns."""

    line_number: int
    sku: str
    item_description: str = ""
    ordered_qty: int = 0
    line_status: str = ""
    warehouse: str = ""
    sub_inventory: str = ""
    item_class: str = ""
    selling_price: float = 0.0
    serial_number: str = ""
    work_order: str = ""


@dataclass
class Order:
    """One Fusion order plus its place in our workflow (route, status, AI verdict)."""

    order_no: str
    order_type: str = ""
    booked_date: str = ""
    bill_to_customer: str = ""
    bill_to_address: str = ""
    ship_to_customer: str = ""
    ship_to_address: str = ""
    ship_to_country: str = ""
    ship_to_country_code: str = ""
    end_customer_country: str = ""
    ship_from: str = ""
    fob: str = ""
    freight_terms: str = ""
    shipping_method: str = ""
    shipping_instructions: str = ""
    customer_po: str = ""
    arrow_po: str = ""
    scheduled_ship_date: str = ""
    lines: List[OrderLine] = field(default_factory=list)
    # Set later by mail matching / the EUS flow, never by Fusion.
    documents_received: bool = False
    eus_case_id: Optional[str] = None
    # Workflow fields, owned by this service.
    route: Optional[str] = None
    route_reason: Optional[str] = None
    status: Optional[str] = None
    ai_verdict: Optional[str] = None
    ai_used: bool = True
    ai_summary: Optional[str] = None
    ai_reasons: List[str] = field(default_factory=list)
    reviewer_decision: Optional[str] = None
    reviewer_comment: Optional[str] = None
    decided_by: Optional[str] = None
    review_thread_id: Optional[str] = None
    rework_count: int = 0
    approved_at: Optional[str] = None
    # Backlog report mails in a row that no longer listed this order.
    missing_snapshot_count: int = 0
    first_seen_at: Optional[str] = None
    last_updated_at: Optional[str] = None
    raw_json: str = ""


@dataclass
class Rule:
    """A business rule. One shape covers both kinds of rule:

    - hard rules use `conditions` (checked by code, always the same answer);
    - soft rules use `guidance` (plain-English instructions the AI weighs).
    Columns a rule doesn't need stay empty.
    """

    name: str
    rule_type: str
    outcome: str
    description: str
    category: str = ""
    applies_to_routes: List[str] = field(default_factory=list)
    scope: Dict[str, List[Any]] = field(default_factory=dict)
    conditions: List[Dict[str, Any]] = field(default_factory=list)
    guidance: str = ""
    message: str = ""
    can_be_overridden: bool = True
    severity: str = ""
    original_wording: str = ""
    source: str = ""
    owner: str = ""
    confidence: Optional[float] = None
    confirmed: bool = False
    status: str = RuleStatus.ACTIVE.value
    generated_by_model: str = ""
    created_by: str = ""
    approved_by: str = ""
    approved_at: Optional[str] = None
    example_orders: str = ""
    attachment_path: Optional[str] = None
    version: int = 1
    id: Optional[int] = None
    created_at: Optional[str] = None
    updated_at: Optional[str] = None


@dataclass
class RuleFinding:
    """A hard rule that found a problem on an order."""

    rule_id: Optional[int]
    rule_name: str
    outcome: str
    message: str
    can_be_overridden: bool = True


@dataclass
class Judgement:
    """The AI's verdict on one order, in plain words for the reviewer."""

    verdict: str
    summary: str
    reasons: List[str] = field(default_factory=list)
    confidence: str = ""
    used_ai: bool = True
