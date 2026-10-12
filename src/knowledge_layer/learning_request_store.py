"""Stores decisions where the reviewer and the AI disagreed, so the AI can learn from them.

Each open request shows up in the Rules Assistant; the AI then asks the reviewer why and
turns the answer into a general rule.
"""

from typing import Dict, List, Optional

from src.knowledge_layer.db import get_connection
from src.utils.time_helpers import utc_now_iso

OPEN = "open"
DONE = "done"


def create_learning_request(
    order_no: str,
    ai_verdict: str,
    reviewer_decision: str,
    reviewer_comment: Optional[str],
    decided_by: str,
    order_details: str = "",
) -> int:
    """Record a disagreement between the AI and the reviewer.

    Args:
        order_no: Fusion order number.
        ai_verdict: What the AI recommended.
        reviewer_decision: What the reviewer decided.
        reviewer_comment: The reviewer's comment, if any.
        decided_by: Name of the MfgOps user.
        order_details: The order and the AI's reasoning, as text for the AI to read.

    Returns:
        The new request's id.
    """
    with get_connection() as connection:
        cursor = connection.execute(
            "INSERT INTO learning_requests (order_no, ai_verdict, reviewer_decision, "
            "reviewer_comment, decided_by, order_details, status, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                order_no,
                ai_verdict,
                reviewer_decision,
                reviewer_comment,
                decided_by,
                order_details,
                OPEN,
                utc_now_iso(),
            ),
        )
        return cursor.lastrowid


def get_open_learning_requests() -> List[Dict]:
    """Read the disagreements the AI still wants to learn from.

    Returns:
        A list of dicts, oldest first.
    """
    with get_connection() as connection:
        rows = connection.execute(
            "SELECT * FROM learning_requests WHERE status = ? ORDER BY id", (OPEN,)
        ).fetchall()
    return [dict(row) for row in rows]


def get_learning_request(request_id: int) -> Optional[Dict]:
    """Read one learning request.

    Args:
        request_id: The request's id.

    Returns:
        The request as a dict, or None.
    """
    with get_connection() as connection:
        row = connection.execute(
            "SELECT * FROM learning_requests WHERE id = ?", (request_id,)
        ).fetchone()
    return dict(row) if row else None


def link_chat(request_id: int, chat_thread_id: str) -> None:
    """Remember which Rules Assistant conversation handles a request.

    Args:
        request_id: The request's id.
        chat_thread_id: The conversation's id.

    Returns:
        None.
    """
    with get_connection() as connection:
        connection.execute(
            "UPDATE learning_requests SET chat_thread_id = ? WHERE id = ?",
            (chat_thread_id, request_id),
        )


def close_learning_request(request_id: int) -> None:
    """Mark a learning request as handled (a rule was saved, or the user dismissed it).

    Args:
        request_id: The request's id.

    Returns:
        None.
    """
    with get_connection() as connection:
        connection.execute(
            "UPDATE learning_requests SET status = ? WHERE id = ?", (DONE, request_id)
        )
