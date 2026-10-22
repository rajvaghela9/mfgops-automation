"""Keeps a log of every mail the service read or sent (metadata only, never the body).

Inbound backlog report mails are recorded so the same report is never processed twice.
Outbound mails carry an idempotency key, so the same email is never sent twice even if a
step runs again after a crash.
"""

from typing import Dict, List, Optional

from src.knowledge_layer.db import get_connection
from src.utils.time_helpers import utc_now_iso

INBOUND = "in"
OUTBOUND = "out"
BACKLOG_REPORT = "backlog_report"
OM_CORRECTION = "om_correction"
ARROW_NOTICE = "arrow_notice"

PROCESSED = "processed"
SUPERSEDED = "superseded"
FAILED = "failed"
SENT = "sent"
MOCK = "mock"


def is_message_known(graph_message_id: str) -> bool:
    """Tell whether a Graph message was already recorded.

    Args:
        graph_message_id: Graph message id.

    Returns:
        True if it is in the log.
    """
    with get_connection() as connection:
        row = connection.execute(
            "SELECT 1 FROM mail_messages WHERE graph_message_id = ?",
            (graph_message_id,),
        ).fetchone()
    return row is not None


def record_inbound_report(
    graph_message_id: str,
    received_at: str,
    sender: str,
    subject: str,
    status: str,
    internet_message_id: str = "",
    attachment_path: str = "",
    attachment_sha256: str = "",
    order_count: Optional[int] = None,
    error: str = "",
) -> None:
    """Record one backlog report mail and what happened to it.

    Args:
        graph_message_id: Graph message id.
        received_at: When the mail arrived (ISO-8601 from Graph).
        sender: Sender address.
        subject: Subject line.
        status: PROCESSED, SUPERSEDED or FAILED.
        internet_message_id: The mail's Internet Message-ID.
        attachment_path: Where the report file was saved.
        attachment_sha256: SHA-256 of the report file.
        order_count: Orders found in the report.
        error: Why processing failed, if it did.

    Returns:
        None.
    """
    with get_connection() as connection:
        connection.execute(
            "INSERT OR REPLACE INTO mail_messages (graph_message_id, internet_message_id, "
            "direction, kind, sender, subject, received_at, status, attachment_path, "
            "attachment_sha256, order_count, error, processed_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                graph_message_id,
                internet_message_id,
                INBOUND,
                BACKLOG_REPORT,
                sender,
                subject,
                received_at,
                status,
                attachment_path,
                attachment_sha256,
                order_count,
                error,
                utc_now_iso(),
            ),
        )


def get_latest_report_received_at() -> Optional[str]:
    """Find when the newest recorded backlog report mail arrived.

    Returns:
        Its receive time (ISO-8601), or None if no report was recorded yet.
    """
    with get_connection() as connection:
        row = connection.execute(
            "SELECT MAX(received_at) AS latest FROM mail_messages "
            "WHERE direction = ? AND kind = ?",
            (INBOUND, BACKLOG_REPORT),
        ).fetchone()
    return row["latest"]


def find_sent_mail(idempotency_key: str, status: str) -> Optional[Dict]:
    """Find an outbound mail already sent with this idempotency key.

    Args:
        idempotency_key: The key the mail was sent with.
        status: SENT (live mode) or MOCK (mock mode), so a mail only written to the
            outbox while testing is still really sent once live mode is on.

    Returns:
        The log row as a dict, or None if no such mail was sent.
    """
    with get_connection() as connection:
        row = connection.execute(
            "SELECT * FROM mail_messages WHERE direction = ? AND idempotency_key = ? "
            "AND status = ? ORDER BY id DESC LIMIT 1",
            (OUTBOUND, idempotency_key, status),
        ).fetchone()
    return dict(row) if row else None


def record_outbound(
    kind: str,
    order_no: str,
    sender: str,
    recipient: str,
    subject: str,
    status: str,
    idempotency_key: str,
    graph_message_id: Optional[str] = None,
    conversation_id: str = "",
    internet_message_id: str = "",
    error: str = "",
) -> None:
    """Record a mail the service sent (or tried to send).

    Args:
        kind: OM_CORRECTION or ARROW_NOTICE.
        order_no: The order the mail is about.
        sender: Sender mailbox.
        recipient: Where it actually went.
        subject: Subject line.
        status: SENT, MOCK or FAILED.
        idempotency_key: Key that stops the same mail being sent twice.
        graph_message_id: Graph id of the sent message (live mode).
        conversation_id: Graph conversation id, for matching replies later.
        internet_message_id: The mail's Internet Message-ID.
        error: Why sending failed, if it did.

    Returns:
        None.
    """
    with get_connection() as connection:
        connection.execute(
            "INSERT INTO mail_messages (graph_message_id, internet_message_id, "
            "conversation_id, direction, kind, order_no, sender, recipient, subject, "
            "status, idempotency_key, error, processed_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                graph_message_id,
                internet_message_id,
                conversation_id,
                OUTBOUND,
                kind,
                order_no,
                sender,
                recipient,
                subject,
                status,
                idempotency_key,
                error,
                utc_now_iso(),
            ),
        )


def get_mail_sync_status() -> Dict:
    """Summarize the backlog mail feed for the UI.

    Returns:
        {"last_report": newest report mail (row dict or None), "last_processed": newest
        report that was read successfully (row dict or None), "last_error": why the
        newest report failed, or None}.
    """
    with get_connection() as connection:
        last_report = connection.execute(
            "SELECT * FROM mail_messages WHERE direction = ? AND kind = ? "
            "ORDER BY received_at DESC LIMIT 1",
            (INBOUND, BACKLOG_REPORT),
        ).fetchone()
        last_processed = connection.execute(
            "SELECT * FROM mail_messages WHERE direction = ? AND kind = ? AND status = ? "
            "ORDER BY received_at DESC LIMIT 1",
            (INBOUND, BACKLOG_REPORT, PROCESSED),
        ).fetchone()
        last_failed = connection.execute(
            "SELECT error FROM mail_messages WHERE status = ? ORDER BY id DESC LIMIT 1",
            (FAILED,),
        ).fetchone()
    return {
        "last_report": dict(last_report) if last_report else None,
        "last_processed": dict(last_processed) if last_processed else None,
        "last_error": (
            last_failed["error"]
            if last_failed and last_report and dict(last_report).get("status") == FAILED
            else None
        ),
    }


def get_recent_outbound(limit: int = 20) -> List[Dict]:
    """Read the most recent mails the service sent.

    Args:
        limit: How many to return.

    Returns:
        Log rows as dicts, newest first.
    """
    with get_connection() as connection:
        rows = connection.execute(
            "SELECT * FROM mail_messages WHERE direction = ? ORDER BY id DESC LIMIT ?",
            (OUTBOUND, limit),
        ).fetchall()
    return [dict(row) for row in rows]
