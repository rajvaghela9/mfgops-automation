"""SQLite state for documents shown in the EUS Approval tab."""

import re
from typing import Dict, Set

from src.knowledge_layer.db import get_connection, init_db
from src.utils.time_helpers import utc_now_iso


def sync_documents() -> None:
    """Add every stored attachment and copy its saved decision status."""
    init_db()
    now = utc_now_iso()
    with get_connection() as connection:
        connection.execute(
            """
            INSERT INTO eus_documents (
                relative_path, folder_name, file_name, review_status,
                received_at, decided_by, decided_at, updated_at
            )
            SELECT
                attachment.relative_path,
                attachment.folder_name,
                attachment.file_name,
                CASE decision.decision
                    WHEN 'approve' THEN 'approved'
                    WHEN 'reject' THEN 'rejected'
                    ELSE 'active'
                END,
                attachment.received_at,
                decision.decided_by,
                decision.decided_at,
                ?
            FROM mailbox_attachments AS attachment
            LEFT JOIN attachment_decisions AS decision
                ON decision.attachment_path = attachment.relative_path
            WHERE true
            ON CONFLICT(relative_path) DO UPDATE SET
                folder_name = excluded.folder_name,
                file_name = excluded.file_name,
                received_at = excluded.received_at,
                review_status = CASE
                    WHEN excluded.review_status = 'active'
                    THEN eus_documents.review_status
                    ELSE excluded.review_status
                END,
                decided_by = COALESCE(excluded.decided_by, eus_documents.decided_by),
                decided_at = COALESCE(excluded.decided_at, eus_documents.decided_at),
                updated_at = excluded.updated_at
            """,
            (now,),
        )


def set_document_types(folder_name: str, document_types: Dict[str, str]) -> None:
    """Save automated classifications for the documents in one email folder."""
    init_db()
    now = utc_now_iso()
    with get_connection() as connection:
        connection.executemany(
            """
            UPDATE eus_documents
            SET document_type = ?, updated_at = ?
            WHERE folder_name = ? AND file_name = ?
            """,
            [
                (document_type, now, folder_name, file_name)
                for file_name, document_type in document_types.items()
            ],
        )


def record_decision(relative_path: str, decision: str, decided_by: str) -> None:
    """Move one EUS document out of the active queue after review."""
    status = {"approve": "approved", "reject": "rejected"}[decision]
    now = utc_now_iso()
    with get_connection() as connection:
        connection.execute(
            """
            UPDATE eus_documents
            SET review_status = ?, decided_by = ?, decided_at = ?, updated_at = ?
            WHERE relative_path = ?
            """,
            (status, decided_by, now, now, relative_path),
        )


def get_active_folders() -> Set[str]:
    """Return email folders in which no document is approved or rejected yet."""
    init_db()
    with get_connection() as connection:
        rows = connection.execute(
            """
            SELECT folder_name FROM eus_documents
            GROUP BY folder_name
            HAVING SUM(review_status != 'active') = 0
            """
        ).fetchall()
    return {row["folder_name"] for row in rows}



def count_waiting_folders() -> int:
    """Count timestamped email folders still waiting for an EUS decision."""
    sync_documents()
    return len(get_active_folders())


def _normalise_end_user(name: str) -> str:
    return re.sub(r"[^a-z0-9]", "", str(name or "").lower())


def set_end_user(folder_name: str, file_name: str, end_user_name: str) -> None:
    """Remember which end user an EUS document belongs to."""
    init_db()
    with get_connection() as connection:
        connection.execute(
            """
            UPDATE eus_documents
            SET end_user_name = ?, updated_at = ?
            WHERE folder_name = ? AND file_name = ?
            """,
            (end_user_name, utc_now_iso(), folder_name, file_name),
        )


def is_repeat_customer(end_user_name: str, exclude_folder: str = "") -> bool:
    """Return whether this end user already has an approved EUS in another email."""
    target = _normalise_end_user(end_user_name)
    if not target:
        return False
    init_db()
    with get_connection() as connection:
        rows = connection.execute(
            """
            SELECT end_user_name FROM eus_documents
            WHERE review_status = 'approved'
                AND end_user_name IS NOT NULL
                AND folder_name != ?
            """,
            (exclude_folder,),
        ).fetchall()
    return any(_normalise_end_user(row["end_user_name"]) == target for row in rows)
