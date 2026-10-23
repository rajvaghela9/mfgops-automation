"""Persistent content and reviewer state for mailbox attachments."""

from pathlib import Path
from typing import Dict, Optional

from src.knowledge_layer import eus_document_store
from src.knowledge_layer.db import get_connection, init_db
from src.utils.time_helpers import utc_now_iso


def store_attachment(
    message_id: str,
    attachment_id: str,
    mailbox: str,
    sender_email: str,
    folder_name: str,
    relative_path: str,
    file_name: str,
    content_type: str,
    content: bytes,
    received_at: str,
) -> None:
    """Store attachment bytes and metadata in SQLite."""
    init_db()
    with get_connection() as connection:
        connection.execute(
            """
            INSERT INTO mailbox_attachments (
                message_id, attachment_id, mailbox, sender_email, folder_name,
                relative_path, file_name, content_type, file_size, content,
                received_at, stored_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(message_id, attachment_id) DO UPDATE SET
                mailbox = excluded.mailbox,
                sender_email = excluded.sender_email,
                folder_name = excluded.folder_name,
                relative_path = excluded.relative_path,
                file_name = excluded.file_name,
                content_type = excluded.content_type,
                file_size = excluded.file_size,
                content = excluded.content,
                received_at = excluded.received_at,
                stored_at = excluded.stored_at
            """,
            (
                message_id,
                attachment_id,
                mailbox,
                sender_email,
                folder_name,
                relative_path,
                file_name,
                content_type,
                len(content),
                content,
                received_at,
                utc_now_iso(),
            ),
        )


def materialize_attachments(output_dir) -> list[Path]:
    """Restore SQLite attachments into the configured folder cache."""
    init_db()
    output_dir = Path(output_dir).expanduser().resolve()
    restored = []
    with get_connection() as connection:
        rows = connection.execute(
            """
            SELECT relative_path, content
            FROM mailbox_attachments
            ORDER BY received_at DESC, relative_path
            """
        ).fetchall()
    for row in rows:
        destination = (output_dir / row["relative_path"]).resolve()
        if output_dir not in destination.parents:
            raise ValueError("Stored attachment path escapes the output directory")
        destination.parent.mkdir(parents=True, exist_ok=True)
        if not destination.is_file() or destination.read_bytes() != row["content"]:
            destination.write_bytes(row["content"])
        restored.append(destination)
    return restored


def migrate_files(output_dir) -> int:
    """Import existing cached attachment files into SQLite once."""
    init_db()
    output_dir = Path(output_dir).expanduser().resolve()
    if not output_dir.is_dir():
        return 0
    imported = 0
    for path in output_dir.rglob("*"):
        if not path.is_file() or path.name.startswith("."):
            continue
        relative_path = path.relative_to(output_dir).as_posix()
        folder_name = path.relative_to(output_dir).parts[0]
        content = path.read_bytes()
        with get_connection() as connection:
            known = connection.execute(
                "SELECT 1 FROM mailbox_attachments WHERE relative_path = ?",
                (relative_path,),
            ).fetchone()
        if known:
            continue
        store_attachment(
            message_id=f"legacy:{folder_name}",
            attachment_id=f"legacy:{relative_path}",
            mailbox="",
            sender_email="",
            folder_name=folder_name,
            relative_path=relative_path,
            file_name=path.name,
            content_type="application/octet-stream",
            content=content,
            received_at="1970-01-01T00:00:00Z",
        )
        imported += 1
    return imported


def record_decision(
    attachment_path: str, decision: str, decided_by: str, comment: str = ""
) -> None:
    """Save the latest approve or reject decision for an attachment."""
    if decision not in {"approve", "reject"}:
        raise ValueError("Attachment decision must be 'approve' or 'reject'")
    with get_connection() as connection:
        connection.execute(
            """
            INSERT INTO attachment_decisions (
                attachment_path, decision, decided_by, comment, decided_at
            ) VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(attachment_path) DO UPDATE SET
                decision = excluded.decision,
                decided_by = excluded.decided_by,
                comment = excluded.comment,
                decided_at = excluded.decided_at
            """,
            (attachment_path, decision, decided_by, comment, utc_now_iso()),
        )
    eus_document_store.record_decision(attachment_path, decision, decided_by)


def get_decision(attachment_path: str) -> Optional[Dict[str, str]]:
    """Return the saved decision for an attachment, if it has been reviewed."""
    with get_connection() as connection:
        row = connection.execute(
            "SELECT decision, decided_by, comment, decided_at "
            "FROM attachment_decisions WHERE attachment_path = ?",
            (attachment_path,),
        ).fetchone()
    return dict(row) if row else None
