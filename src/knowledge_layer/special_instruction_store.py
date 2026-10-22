"""Persistent status for special-instruction email packages."""

from typing import Dict, Optional

from src.knowledge_layer.db import get_connection
from src.utils.time_helpers import utc_now_iso


def record_share(folder_name: str, recipient: str, shared_by: str) -> None:
    """Record that one downloaded email package was shared."""
    with get_connection() as connection:
        connection.execute(
            """
            INSERT INTO shared_mail_packages (
                folder_name, recipient, shared_by, shared_at
            ) VALUES (?, ?, ?, ?)
            ON CONFLICT(folder_name) DO UPDATE SET
                recipient = excluded.recipient,
                shared_by = excluded.shared_by,
                shared_at = excluded.shared_at
            """,
            (folder_name, recipient, shared_by, utc_now_iso()),
        )


def get_share(folder_name: str) -> Optional[Dict[str, str]]:
    """Return the recorded sharing details for one email folder."""
    with get_connection() as connection:
        row = connection.execute(
            """
            SELECT recipient, shared_by, shared_at
            FROM shared_mail_packages
            WHERE folder_name = ?
            """,
            (folder_name,),
        ).fetchone()
    return dict(row) if row else None
