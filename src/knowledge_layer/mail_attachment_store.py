"""Persistent state for mailbox messages already checked for attachments."""

from typing import Set

from src.knowledge_layer.db import get_connection
from src.utils.time_helpers import utc_now_iso


def get_processed_message_ids(mailbox: str, sender_email: str) -> Set[str]:
    """Return Graph message IDs already processed for one mailbox and sender."""
    with get_connection() as connection:
        rows = connection.execute(
            """
            SELECT message_id
            FROM processed_mail_messages
            WHERE mailbox = ? AND sender_email = ?
            """,
            (mailbox, sender_email),
        ).fetchall()
    return {row["message_id"] for row in rows}


def record_processed_message(
    message_id: str,
    mailbox: str,
    sender_email: str,
    received_at: str,
) -> None:
    """Record that a Graph message was fully checked for attachments."""
    with get_connection() as connection:
        connection.execute(
            """
            INSERT INTO processed_mail_messages (
                message_id, mailbox, sender_email, received_at, processed_at
            ) VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(message_id) DO UPDATE SET
                mailbox = excluded.mailbox,
                sender_email = excluded.sender_email,
                received_at = excluded.received_at,
                processed_at = excluded.processed_at
            """,
            (
                message_id,
                mailbox,
                sender_email,
                received_at,
                utc_now_iso(),
            ),
        )
