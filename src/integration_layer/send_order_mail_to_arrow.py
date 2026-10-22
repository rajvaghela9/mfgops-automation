"""Emails Arrow the order details and special instructions once an order is released.

Sent only for Special Instructions orders (and EUS + Special Instructions), after Fusion's
USPO for the order has shown up in a backlog report, i.e. after the B2B push. The PO is
never attached or quoted; the approved EUS (if any) is sent by the EUS workflow.
"""

from typing import Tuple

from src.integration_layer.outbound_mail import deliver_mail
from src.integration_layer.send_back_to_om import build_order_details_html, escape
from src.knowledge_layer import mail_store
from src.utils.config import load_config
from src.utils.models import SPECIAL_INSTRUCTION_ROUTES, Order


def needs_arrow_mail(order: Order) -> bool:
    """Tell whether Arrow must get a mail about this order.

    Args:
        order: The order.

    Returns:
        True for Special Instructions categories.
    """
    return order.route in SPECIAL_INSTRUCTION_ROUTES


def build_arrow_email(order: Order, uspo: str) -> Tuple[str, str]:
    """Write the subject and HTML body of the mail to Arrow.

    Args:
        order: The released order.
        uspo: Fusion's USPO for the order.

    Returns:
        A (subject, html_body) tuple.
    """
    subject = f"[Order {order.order_no}] {uspo} special shipping instructions"
    body = (
        "<p>Hello Arrow team,</p>"
        f"<p>Order <b>{escape(order.order_no)}</b> (USPO <b>{escape(uspo)}</b>) has "
        "been released to you through B2B. Please follow these special instructions "
        "when shipping it.</p>"
        f"<p><b>Special instructions:</b><br>{escape(order.shipping_instructions)}</p>"
        "<p><b>Order details</b></p>"
        f"{build_order_details_html(order)}"
        "<p>Thank you,<br>Infoblox MfgOps</p>"
    )
    return subject, body


def email_arrow(order: Order, uspo: str) -> str:
    """Send the special-instructions mail to Arrow (once per order and USPO).

    Args:
        order: The released order.
        uspo: Fusion's USPO for the order.

    Returns:
        Where the mail went. Raises outbound_mail.MailNotSentError on failure.
    """
    subject, body = build_arrow_email(order, uspo)
    return deliver_mail(
        mail_store.ARROW_NOTICE,
        order.order_no,
        load_config().arrow_recipient_email or "",
        subject,
        body,
        idempotency_key=f"arrow_notice:{order.order_no}:{uspo}",
    )
