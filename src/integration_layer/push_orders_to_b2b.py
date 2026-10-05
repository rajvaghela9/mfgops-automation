"""Releases approved orders to Arrow/B2B and closes them once Arrow's USPO appears.

Release = submitting the "Infoblox Priority Release Pause Task" ESS job for the order;
Fusion and Boomi then send the PO XML to Arrow. Live ESS submission and USPO lookup
are pending the job definition from Fusion IT; mock mode simulates both.
"""

from typing import Optional

from src.knowledge_layer import order_store
from src.utils.config import load_config
from src.utils.logging_config import get_logger
from src.utils.models import AiRecommendation, DecisionType, OrderStatus

logger = get_logger(__name__)


def submit_priority_release_job_live(order_no: str) -> str:
    """Submit the release ESS job in Fusion (POST /ess/rest/scheduler/v1/requests).

    Args:
        order_no: Fusion order number.

    Returns:
        The ESS request id.
    """
    raise NotImplementedError(
        "Live release is pending: need the ESS job package/name and parameter names of "
        "'Infoblox Priority Release Pause Task', and an integration user allowed to submit it."
    )


def submit_priority_release_job_mock(order_no: str) -> str:
    """Pretend to submit the release job.

    Args:
        order_no: Fusion order number.

    Returns:
        A fake ESS request id.
    """
    logger.info("[mock] Submitted Priority Release Pause Task for %s", order_no)
    return f"MOCK-ESS-{order_no}"


def push_order(
    order_no: str,
    decided_by: str,
    comment: Optional[str] = None,
    override: bool = False,
) -> bool:
    """Release an order to Arrow/B2B after a MfgOps user's decision.

    A plain approve needs an "approve" AI recommendation. An override (pushing despite
    a "reject") needs a comment explaining why, e.g. "EUS approved by email 9/28".

    Args:
        order_no: Fusion order number.
        decided_by: Name of the MfgOps user.
        comment: Why; required when override is True.
        override: True to push despite a "reject" recommendation.

    Returns:
        True if released; False if the order isn't waiting for review (e.g. already pushed).
    """
    order = order_store.get_order(order_no)
    if order is None or order.status not in order_store.REVIEWABLE_STATUSES:
        logger.info("Not pushing %s: not waiting for review", order_no)
        return False
    if override and not (comment or "").strip():
        raise ValueError("Override & Push needs a comment explaining why.")
    if not override and order.ai_recommendation != AiRecommendation.APPROVE.value:
        raise ValueError(
            "The AI recommends rejecting this order; use Override & Push instead."
        )

    if load_config().fusion_mode == "live":
        request_id = submit_priority_release_job_live(order_no)
    else:
        request_id = submit_priority_release_job_mock(order_no)

    decision = DecisionType.PUSHED_OVERRIDE if override else DecisionType.APPROVED
    order_store.record_decision(order_no, decision.value, decided_by, comment)
    order_store.mark_released(order_no, request_id)
    return True


def check_uspo_status(order_no: str) -> Optional[str]:
    """Look up the USPO Arrow created for a released order.

    Args:
        order_no: Fusion order number.

    Returns:
        The USPO number, or None if it hasn't appeared yet.
    """
    if load_config().fusion_mode == "live":
        raise NotImplementedError(
            "Live USPO lookup is pending: confirm with Fusion IT where the USPO appears "
            "(order line details, fulfillment references, or the Purchase Orders API)."
        )
    return f"USPO-MOCK-{order_no}"


def verify_released_orders() -> int:
    """Close every released order whose USPO has appeared.

    Returns:
        How many orders were closed.
    """
    closed = 0
    for order in order_store.get_active_orders([OrderStatus.AWAITING_USPO.value]):
        uspo_number = check_uspo_status(order.order_no)
        if uspo_number:
            order_store.close_order(order.order_no, uspo_number)
            closed += 1
    if closed:
        logger.info("Closed %d order(s) after USPO confirmation", closed)
    return closed
