"""Emails the OM team about an order the reviewer sent back, with their comment and the details.

The order stays active as SENT_TO_OM. When OM fixes it in Fusion, the next backlog report
shows the change and the order is reviewed again from the start.
The subject carries "[Order <number>]" so OM's reply can be matched to the order later.
Delivery (mock outbox or MS Graph, test redirect, no double sending) is in outbound_mail.
"""

import html
from typing import List, Tuple

from src.integration_layer.outbound_mail import deliver_mail
from src.knowledge_layer import mail_store
from src.utils.config import load_config
from src.utils.models import ROUTE_LABELS, Order, RuleFinding

CELL = 'style="border:1px solid #ccc;padding:4px 8px;text-align:left"'


def escape(value) -> str:
    """Make a value safe to put in the HTML mail (order data is free text from Fusion).

    Args:
        value: Any value.

    Returns:
        Escaped text; "—" for empty values.
    """
    text = "" if value is None else str(value).strip()
    return html.escape(text) if text else "—"


def build_order_details_html(order: Order) -> str:
    """Write the order header and lines as HTML (used in the OM and Arrow mails).

    Args:
        order: The order, with its lines.

    Returns:
        An HTML fragment.
    """
    details = [
        ("Order type", f"{order.order_type} ({ROUTE_LABELS.get(order.route, '—')})"),
        ("Booked", order.booked_date),
        ("Bill to", f"{order.bill_to_customer}, {order.bill_to_address}"),
        (
            "Ship to",
            f"{order.ship_to_customer}, {order.ship_to_address}, {order.ship_to_country}",
        ),
        ("Ship from", order.ship_from),
        ("Incoterm (FOB)", order.fob),
        ("Freight terms", order.freight_terms),
        ("Shipping method", order.shipping_method),
        ("Shipping instructions", order.shipping_instructions),
    ]
    header_rows = "".join(
        f"<tr><th {CELL}>{escape(name)}</th><td {CELL}>{escape(value)}</td></tr>"
        for name, value in details
    )
    line_rows = "".join(
        f"<tr><td {CELL}>{escape(line.line_number)}</td><td {CELL}>{escape(line.sku)}</td>"
        f"<td {CELL}>{escape(line.item_description)}</td>"
        f"<td {CELL}>{escape(line.ordered_qty)}</td></tr>"
        for line in sorted(order.lines, key=lambda line: line.line_number)
    )
    return (
        f'<table style="border-collapse:collapse">{header_rows}</table>'
        "<p><b>Order lines</b></p>"
        f'<table style="border-collapse:collapse"><tr><th {CELL}>Line</th>'
        f"<th {CELL}>Item</th><th {CELL}>Description</th><th {CELL}>Qty</th></tr>"
        f"{line_rows}</table>"
    )


def build_om_email(
    order: Order, findings: List[RuleFinding], reviewer: str, comment: str
) -> Tuple[str, str]:
    """Write the subject and HTML body of the email to OM.

    Args:
        order: The order sent back.
        findings: Fixed checks that found a problem on it.
        reviewer: Name of the MfgOps user.
        comment: What OM should correct.

    Returns:
        A (subject, html_body) tuple.
    """
    subject = f"[Order {order.order_no}] needs correction before release to Arrow"
    issues = [f"{finding.rule_name}: {finding.message}" for finding in findings]
    issues += list(order.ai_reasons)
    issue_items = "".join(f"<li>{escape(issue)}</li>" for issue in issues) or (
        "<li>None besides the comment above.</li>"
    )
    body = (
        "<p>Hello OM team,</p>"
        f"<p>Order <b>{escape(order.order_no)}</b> was not released to Arrow.</p>"
        f"<p><b>Correction needed</b> (from {escape(reviewer)}, MfgOps):<br>"
        f"{escape(comment).replace(chr(10), '<br>')}</p>"
        f"<p><b>Issues found during review</b></p><ul>{issue_items}</ul>"
        "<p><b>Order details</b></p>"
        f"{build_order_details_html(order)}"
        "<p>Please correct the order in Fusion. It will be checked again automatically "
        "once the corrected order shows up in the backlog report.</p>"
        "<p>MfgOps Order Review</p>"
    )
    return subject, body


def email_om_team(
    order: Order, findings: List[RuleFinding], reviewer: str, comment: str
) -> str:
    """Email OM the reviewer's correction plus the order details (once per correction).

    Args:
        order: The order sent back.
        findings: Fixed checks that found a problem on it.
        reviewer: Name of the MfgOps user.
        comment: What OM should correct.

    Returns:
        Where the email went (outbox file in mock mode, Graph message id when live).
        Raises outbound_mail.MailNotSentError if it could not be sent.
    """
    subject, body = build_om_email(order, findings, reviewer, comment)
    return deliver_mail(
        mail_store.OM_CORRECTION,
        order.order_no,
        load_config().om_team_email or "",
        subject,
        body,
        idempotency_key=f"om_correction:{order.order_no}:{order.rework_count}",
    )
