"""Shared data shapes (orders, rules, findings) and the status values they use."""

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional


class RouteDecision(str, Enum):
    """Which of the three routes an order follows."""

    STANDARD = "standard"
    SPECIAL_INSTRUCTIONS = "special_instructions"
    EUS = "eus"


class OrderStatus(str, Enum):
    """Where an order is in its lifecycle. CLOSED orders live only in order_history."""

    ROUTED = "ROUTED"
    WAITING_ON_OM = "WAITING_ON_OM"
    IN_REVIEW = "IN_REVIEW"
    SENT_BACK_TO_OM = "SENT_BACK_TO_OM"
    AWAITING_USPO = "AWAITING_USPO"
    CLOSED = "CLOSED"


class RuleAction(str, Enum):
    """What a rule does when it fires: block and require_doc reject, warn only flags."""

    BLOCK = "block"
    WARN = "warn"
    REQUIRE_DOC = "require_doc"


class AiRecommendation(str, Enum):
    """The rules engine's recommendation shown to the MfgOps user."""

    APPROVE = "approve"
    REJECT = "reject"


class DecisionType(str, Enum):
    """The action a MfgOps user took on an order."""

    APPROVED = "approved"
    SENT_BACK = "sent_back"
    PUSHED_OVERRIDE = "pushed_override"


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
    """One Fusion order plus its place in our workflow (route, status, AI result)."""

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
    fob: str = ""
    freight_terms: str = ""
    shipping_method: str = ""
    shipping_instructions: str = ""
    customer_po: str = ""
    scheduled_ship_date: str = ""
    lines: List[OrderLine] = field(default_factory=list)
    # Set later by mail matching / the EUS flow, never by Fusion.
    documents_received: bool = False
    eus_case_id: Optional[str] = None
    # Workflow fields, owned by this service.
    route: Optional[str] = None
    route_reason: Optional[str] = None
    status: Optional[str] = None
    ai_recommendation: Optional[str] = None
    ai_explanation: Optional[str] = None
    human_decision: Optional[str] = None
    rework_count: int = 0
    first_seen_at: Optional[str] = None
    last_updated_at: Optional[str] = None
    released_at: Optional[str] = None
    raw_json: str = ""


@dataclass
class Rule:
    """A business rule. It fires (= a finding) when the order is in scope and all
    conditions are true; an empty conditions list always fires in scope."""

    name: str
    domain: str
    action: str
    message: str
    scope: Dict[str, List[Any]] = field(default_factory=dict)
    conditions: List[Dict[str, Any]] = field(default_factory=list)
    source: str = ""
    owner: str = ""
    version: int = 1
    attachment_path: Optional[str] = None
    is_active: bool = True
    id: Optional[int] = None
    created_at: Optional[str] = None


@dataclass
class ValidationResult:
    """One finding: a rule that fired on an order, with its rendered message."""

    order_no: str
    rule_id: Optional[int]
    rule_name: str
    action: str
    message: str
