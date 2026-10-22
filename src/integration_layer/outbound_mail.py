"""Delivers every mail the service sends (to OM and to Arrow), the same way each time.

EMAIL_MODE=mock writes the mail as an .html file to OUTBOX_DIR; EMAIL_MODE=live sends it
through MS Graph from GRAPH_API_SENDER_MAILBOX (or the monitored mailbox). While
EMAIL_REDIRECT_TO is set, every mail goes to that address instead, with the intended
recipient shown at the top (for testing). Each mail has an idempotency key: a mail
already sent with the same key is not sent again.
"""

import html
import uuid
from pathlib import Path

from src.integration_layer import graph_mail_client
from src.knowledge_layer import mail_store
from src.utils.config import load_config
from src.utils.logging_config import get_logger
from src.utils.time_helpers import utc_now_iso

logger = get_logger(__name__)


class MailNotSentError(Exception):
    """A mail could not be sent; nothing was changed (the message says why)."""


def add_redirect_note(html_body: str, intended_recipient: str) -> str:
    """Put a test-mode banner at the top of a redirected mail.

    Args:
        html_body: The mail body.
        intended_recipient: Who the mail was really meant for.

    Returns:
        The body with the banner.
    """
    banner = (
        '<p style="background:#fff3cd;padding:8px;border:1px solid #e0c36b">'
        "<b>Test mode:</b> this mail was meant for "
        f"<b>{html.escape(intended_recipient)}</b> and was redirected to you "
        "(EMAIL_REDIRECT_TO).</p>"
    )
    return banner + html_body


def write_to_outbox(
    to_address: str, subject: str, html_body: str, order_no: str
) -> str:
    """Save the mail as an HTML file instead of sending it.

    Args:
        to_address: Recipient (written into the file).
        subject: Subject line.
        html_body: Body as HTML.
        order_no: Order the mail is about (used in the file name).

    Returns:
        Path of the written file.
    """
    outbox = Path(load_config().outbox_dir)
    outbox.mkdir(parents=True, exist_ok=True)
    stamp = utc_now_iso().replace(":", "").replace("+0000", "Z")
    path = outbox / f"{stamp}_{order_no}_{uuid.uuid4().hex[:6]}.html"
    path.write_text(
        f"<p>To: {html.escape(to_address)}<br>Subject: {html.escape(subject)}</p>"
        f"<hr>{html_body}\n",
        encoding="utf-8",
    )
    logger.info("[mock] Mail for %s written to %s", order_no, path)
    return str(path)


def deliver_mail(
    kind: str,
    order_no: str,
    to_address: str,
    subject: str,
    html_body: str,
    idempotency_key: str,
) -> str:
    """Send (or, in mock mode, save) one mail and log it.

    Args:
        kind: mail_store.OM_CORRECTION or mail_store.ARROW_NOTICE.
        order_no: Order the mail is about.
        to_address: Intended recipient.
        subject: Subject line.
        html_body: Body as HTML (every dynamic value already escaped).
        idempotency_key: Stops the same mail being sent twice.

    Returns:
        Where it went: the outbox file (mock) or the Graph message id (live). Raises
        MailNotSentError if it could not be sent.
    """
    config = load_config()
    live = config.email_mode == "live"
    already_sent = mail_store.find_sent_mail(
        idempotency_key, mail_store.SENT if live else mail_store.MOCK
    )
    if already_sent:
        logger.info("Mail %s was already sent; not sending again", idempotency_key)
        return already_sent["graph_message_id"] or already_sent["recipient"]
    if not to_address:
        raise MailNotSentError(
            "No recipient set (OM_TEAM_EMAIL / ARROW_RECIPIENT_EMAIL in .env)."
        )
    recipient = to_address
    if config.email_redirect_to:
        recipient = config.email_redirect_to
        html_body = add_redirect_note(html_body, to_address)
    if not live:
        path = write_to_outbox(recipient, subject, html_body, order_no)
        mail_store.record_outbound(
            kind,
            order_no,
            "outbox",
            recipient,
            subject,
            mail_store.MOCK,
            idempotency_key,
        )
        return path
    sender = config.sender_mailbox
    try:
        sent = graph_mail_client.send_html_mail(sender, recipient, subject, html_body)
    except graph_mail_client.GraphError as error:
        mail_store.record_outbound(
            kind,
            order_no,
            sender or "",
            recipient,
            subject,
            mail_store.FAILED,
            idempotency_key,
            error=str(error),
        )
        raise MailNotSentError(f"The email could not be sent: {error}") from error
    mail_store.record_outbound(
        kind,
        order_no,
        sender,
        recipient,
        subject,
        mail_store.SENT,
        idempotency_key,
        graph_message_id=sent["message_id"],
        conversation_id=sent["conversation_id"],
        internet_message_id=sent["internet_message_id"],
    )
    return sent["message_id"]
