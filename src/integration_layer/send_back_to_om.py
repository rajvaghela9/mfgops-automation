"""Sends a rejected order back to the OM team with the findings and the reviewer's comment.

The order stays in Fusion (and in our active orders) as SENT_BACK_TO_OM. When OM fixes
it, the next Fusion sync sees the change, updates it in place and validates it again.
EMAIL_MODE=mock writes the email to OUTBOX_DIR; live sending (MS Graph) is pending access.
"""

from pathlib import Path
from typing import List, Tuple

from src.knowledge_layer import order_store
from src.utils.config import load_config
from src.utils.logging_config import get_logger
from src.utils.models import DecisionType, Order, OrderStatus, ValidationResult
from src.utils.time_helpers import utc_now_iso

logger = get_logger(__name__)


def build_rejection_email(
    order: Order, findings: List[ValidationResult], decided_by: str, comment: str
) -> Tuple[str, str]:
    """Write the subject and body of the send-back email.

    Args:
        order: The rejected order.
        findings: Rules that fired on it.
        decided_by: Name of the MfgOps user.
        comment: The user's comment for OM.

    Returns:
        A (subject, body) tuple.
    """
    subject = f"Order {order.order_no} needs correction before release to Arrow"
    finding_lines = "\n".join(
        f"- {item.rule_name}: {item.message}" for item in findings
    )
    body = (
        f"Hello OM team,\n\n"
        f"Order {order.order_no} ({order.order_type}, ship to {order.ship_to_country}) "
        f"was not released to Arrow.\n\n"
        f"Comments from MfgOps ({decided_by}):\n{comment}\n\n"
        f"Issues found:\n{finding_lines or '- None recorded'}\n\n"
        f"Explanation:\n{order.ai_explanation or 'n/a'}\n\n"
        f"Please correct the order in Fusion. It will be checked again automatically.\n\n"
        f"MfgOps Order Validation"
    )
    return subject, body


def send_mock(to_address: str, subject: str, body: str) -> str:
    """Save the email as a text file instead of sending it.

    Args:
        to_address: Recipient (written into the file).
        subject: Email subject.
        body: Email body.

    Returns:
        Path of the written file.
    """
    outbox = Path(load_config().outbox_dir)
    outbox.mkdir(parents=True, exist_ok=True)
    stamp = utc_now_iso().replace(":", "").replace("+0000", "Z")
    path = outbox / f"{stamp}_{subject.split()[1]}.txt"
    path.write_text(
        f"To: {to_address}\nSubject: {subject}\n\n{body}\n", encoding="utf-8"
    )
    logger.info("[mock] Send-back email written to %s", path)
    return str(path)


def send_live(to_address: str, subject: str, body: str) -> str:
    """Send the email through MS Graph from the monitored MfgOps mailbox.

    Args:
        to_address: Recipient.
        subject: Email subject.
        body: Email body.

    Returns:
        The sent message's id.
    """
    raise NotImplementedError(
        "Live email is pending the MS Graph app registration (Mail.Send) and the "
        "mailbox to send from; consider sharing one Graph sender module with the EUS flow."
    )


def send_back_to_om(order_no: str, decided_by: str, comment: str) -> str:
    """Reject an order: email OM, log the decision, and mark it SENT_BACK_TO_OM.

    Args:
        order_no: Fusion order number.
        decided_by: Name of the MfgOps user.
        comment: What OM should fix (required).

    Returns:
        Where the email went (file path in mock mode, message id when live).
    """
    if not (comment or "").strip():
        raise ValueError("Sending an order back to OM needs a comment.")
    order = order_store.get_order(order_no)
    if order is None or order.status not in order_store.REVIEWABLE_STATUSES:
        raise ValueError(f"Order {order_no} is not waiting for review.")

    config = load_config()
    to_address = config.om_team_email or "<OM_TEAM_EMAIL not set>"
    subject, body = build_rejection_email(
        order, order_store.get_validation_results(order_no), decided_by, comment
    )
    sent = (
        send_live(to_address, subject, body)
        if config.email_mode == "live"
        else send_mock(to_address, subject, body)
    )
    order_store.record_decision(
        order_no, DecisionType.SENT_BACK.value, decided_by, comment
    )
    order_store.update_order_status(order_no, OrderStatus.SENT_BACK_TO_OM.value)
    return sent
