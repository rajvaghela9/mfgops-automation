"""Emails the OM team about an order the reviewer sent back, with their comment and the details.

The order stays active as SENT_TO_OM. When OM fixes it in Fusion, the next Fusion check
sees the change and reviews it again from the start.
EMAIL_MODE=mock writes the email to OUTBOX_DIR; live sending (MS Graph) is pending access.
"""

from pathlib import Path
from typing import List, Tuple

from src.utils.config import load_config
from src.utils.logging_config import get_logger
from src.utils.models import ROUTE_LABELS, Order, RuleFinding
from src.utils.time_helpers import utc_now_iso

logger = get_logger(__name__)


def build_om_email(
    order: Order, findings: List[RuleFinding], reviewer: str, comment: str
) -> Tuple[str, str]:
    """Write the subject and body of the email to OM.

    Args:
        order: The order sent back.
        findings: Fixed checks that found a problem on it.
        reviewer: Name of the MfgOps user.
        comment: What OM should correct.

    Returns:
        A (subject, body) tuple.
    """
    subject = f"Order {order.order_no} needs correction before release to Arrow"
    issues = [f"- {finding.rule_name}: {finding.message}" for finding in findings]
    issues += [f"- {reason}" for reason in order.ai_reasons]
    lines = "\n".join(
        f"- Line {line.line_number}: {line.ordered_qty} x {line.sku} ({line.item_description})"
        for line in order.lines
    )
    body = (
        f"Hello OM team,\n\n"
        f"Order {order.order_no} was not released to Arrow.\n\n"
        f"Correction needed (from {reviewer}, MfgOps):\n{comment}\n\n"
        f"Issues found during review:\n{chr(10).join(issues) or '- None besides the above'}\n\n"
        f"Order details:\n"
        f"- Order type: {order.order_type} ({ROUTE_LABELS.get(order.route, order.route)})\n"
        f"- Ship to: {order.ship_to_customer}, {order.ship_to_address}, "
        f"{order.ship_to_country}\n"
        f"- Customer PO: {order.customer_po or 'missing'}\n"
        f"- Shipping method: {order.shipping_method or 'missing'}\n"
        f"{lines}\n\n"
        f"Please correct the order in Fusion. It will be checked again automatically.\n\n"
        f"MfgOps Order Review"
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
    logger.info("[mock] OM email written to %s", path)
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


def email_om_team(
    order: Order, findings: List[RuleFinding], reviewer: str, comment: str
) -> str:
    """Email OM the reviewer's correction plus the order details.

    Args:
        order: The order sent back.
        findings: Fixed checks that found a problem on it.
        reviewer: Name of the MfgOps user.
        comment: What OM should correct.

    Returns:
        Where the email went (file path in mock mode, message id when live).
    """
    config = load_config()
    to_address = config.om_team_email or "<OM_TEAM_EMAIL not set>"
    subject, body = build_om_email(order, findings, reviewer, comment)
    if config.email_mode == "live":
        return send_live(to_address, subject, body)
    return send_mock(to_address, subject, body)
