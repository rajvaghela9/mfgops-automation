"""Stores orders while they are being worked on, plus the permanent decision log and history.

Lifecycle: an order is inserted when first fetched, updated in place when OM corrects it
in Fusion, and deleted from the active tables once it closes (Fusion keeps the full record
forever). A small permanent row is kept in order_history for analytics.
"""

import hashlib
import json
from dataclasses import asdict
from typing import Dict, List, Optional, Set

from src.knowledge_layer.db import get_connection
from src.utils.models import (
    AiRecommendation,
    DecisionType,
    Order,
    OrderLine,
    OrderStatus,
    ValidationResult,
)
from src.utils.time_helpers import utc_now_iso

# Header fields that come from Fusion. A change in any of these (or in the lines)
# means OM edited the order.
FUSION_HEADER_FIELDS = [
    "order_type",
    "booked_date",
    "bill_to_customer",
    "bill_to_address",
    "ship_to_customer",
    "ship_to_address",
    "ship_to_country",
    "ship_to_country_code",
    "end_customer_country",
    "fob",
    "freight_terms",
    "shipping_method",
    "shipping_instructions",
    "customer_po",
    "scheduled_ship_date",
]
LINE_FIELDS = [
    "line_number",
    "sku",
    "item_description",
    "ordered_qty",
    "line_status",
    "warehouse",
    "sub_inventory",
    "item_class",
    "selling_price",
    "serial_number",
    "work_order",
]
REVIEWABLE_STATUSES = [OrderStatus.IN_REVIEW.value, OrderStatus.WAITING_ON_OM.value]


def compute_content_hash(order: Order) -> str:
    """Fingerprint the Fusion-owned content of an order (header fields and lines).

    Args:
        order: The order.

    Returns:
        A SHA-256 hex digest that changes whenever OM edits the order.
    """
    content = {name: getattr(order, name) for name in FUSION_HEADER_FIELDS}
    content["lines"] = [asdict(line) for line in order.lines]
    return hashlib.sha256(json.dumps(content, sort_keys=True).encode()).hexdigest()


def insert_lines(connection, order: Order) -> None:
    """Write an order's lines.

    Args:
        connection: Open SQLite connection.
        order: The order whose lines to write.

    Returns:
        None.
    """
    placeholders = ", ".join("?" for _ in LINE_FIELDS)
    for line in order.lines:
        connection.execute(
            f"INSERT INTO order_lines (order_no, {', '.join(LINE_FIELDS)}) "
            f"VALUES (?, {placeholders})",
            (order.order_no, *[getattr(line, name) for name in LINE_FIELDS]),
        )


def upsert_order(order: Order) -> str:
    """Insert a newly fetched order, or update it in place if OM changed it.

    An updated order goes back to ROUTED so it is validated again; its old findings,
    AI recommendation and human decision are cleared.

    Args:
        order: The fetched order, with route and route_reason already set.

    Returns:
        "inserted", "updated_in_place", "unchanged", or "already_closed".
    """
    new_hash = compute_content_hash(order)
    raw_json = order.raw_json or json.dumps(asdict(order), default=str)
    now = utc_now_iso()
    header_values = [getattr(order, name) for name in FUSION_HEADER_FIELDS]
    with get_connection() as connection:
        if connection.execute(
            "SELECT 1 FROM order_history WHERE order_no = ?", (order.order_no,)
        ).fetchone():
            return "already_closed"
        existing = connection.execute(
            "SELECT content_hash, status FROM orders WHERE order_no = ?",
            (order.order_no,),
        ).fetchone()

        if existing is None:
            columns = [
                "order_no",
                *FUSION_HEADER_FIELDS,
                "documents_received",
                "eus_case_id",
            ]
            columns += ["route", "route_reason", "status", "content_hash", "raw_json"]
            columns += ["first_seen_at", "last_updated_at"]
            values = [order.order_no, *header_values, int(order.documents_received)]
            values += [order.eus_case_id, order.route, order.route_reason]
            values += [OrderStatus.ROUTED.value, new_hash, raw_json, now, now]
            connection.execute(
                f"INSERT INTO orders ({', '.join(columns)}) "
                f"VALUES ({', '.join('?' for _ in columns)})",
                values,
            )
            insert_lines(connection, order)
            return "inserted"

        # A released order is Fusion's to finish; don't pull it back into review.
        if existing["content_hash"] == new_hash or existing["status"] == (
            OrderStatus.AWAITING_USPO.value
        ):
            return "unchanged"

        assignments = ", ".join(f"{name} = ?" for name in FUSION_HEADER_FIELDS)
        connection.execute(
            f"UPDATE orders SET {assignments}, route = ?, route_reason = ?, status = ?, "
            "ai_recommendation = NULL, ai_explanation = NULL, human_decision = NULL, "
            "rework_count = rework_count + 1, content_hash = ?, raw_json = ?, "
            "last_updated_at = ? WHERE order_no = ?",
            (
                *header_values,
                order.route,
                order.route_reason,
                OrderStatus.ROUTED.value,
                new_hash,
                raw_json,
                now,
                order.order_no,
            ),
        )
        connection.execute(
            "DELETE FROM order_lines WHERE order_no = ?", (order.order_no,)
        )
        connection.execute(
            "DELETE FROM validation_results WHERE order_no = ?", (order.order_no,)
        )
        insert_lines(connection, order)
        return "updated_in_place"


def row_to_order(row, line_rows) -> Order:
    """Convert database rows into an Order.

    Args:
        row: A sqlite3.Row from the orders table.
        line_rows: That order's sqlite3.Rows from order_lines.

    Returns:
        An Order with its lines.
    """
    lines = [
        OrderLine(**{name: line[name] for name in LINE_FIELDS}) for line in line_rows
    ]
    data = {
        name: row[name] for name in row.keys() if name in Order.__dataclass_fields__
    }
    data["documents_received"] = bool(data.get("documents_received"))
    return Order(**data, lines=lines)


def get_order(order_no: str) -> Optional[Order]:
    """Read one active order with its lines.

    Args:
        order_no: Fusion order number.

    Returns:
        The Order, or None if it isn't active (never seen, or already closed).
    """
    with get_connection() as connection:
        row = connection.execute(
            "SELECT * FROM orders WHERE order_no = ?", (order_no,)
        ).fetchone()
        if row is None:
            return None
        line_rows = connection.execute(
            "SELECT * FROM order_lines WHERE order_no = ? ORDER BY line_number",
            (order_no,),
        ).fetchall()
    return row_to_order(row, line_rows)


def get_active_orders(statuses: Optional[List[str]] = None) -> List[Order]:
    """Read active orders, optionally only those in some statuses.

    Args:
        statuses: OrderStatus values to keep, or None for all active orders.

    Returns:
        Orders (with lines), oldest first.
    """
    query = "SELECT order_no FROM orders"
    params: tuple = ()
    if statuses:
        query += f" WHERE status IN ({', '.join('?' for _ in statuses)})"
        params = tuple(statuses)
    with get_connection() as connection:
        order_nos = [
            row["order_no"]
            for row in connection.execute(query + " ORDER BY first_seen_at", params)
        ]
    return [order for order in map(get_order, order_nos) if order is not None]


def get_sent_back_order_nos() -> Set[str]:
    """List orders currently sent back to OM.

    Returns:
        Their order numbers.
    """
    with get_connection() as connection:
        rows = connection.execute(
            "SELECT order_no FROM orders WHERE status = ?",
            (OrderStatus.SENT_BACK_TO_OM.value,),
        ).fetchall()
    return {row["order_no"] for row in rows}


def update_order_status(order_no: str, status: str) -> None:
    """Move an order to a new status.

    Args:
        order_no: Fusion order number.
        status: An OrderStatus value.

    Returns:
        None.
    """
    with get_connection() as connection:
        connection.execute(
            "UPDATE orders SET status = ?, last_updated_at = ? WHERE order_no = ?",
            (status, utc_now_iso(), order_no),
        )


def save_validation(
    order_no: str,
    findings: List[ValidationResult],
    ai_recommendation: str,
    ai_explanation: str,
    status: str,
) -> None:
    """Replace an order's findings and store the AI recommendation and explanation.

    Args:
        order_no: Fusion order number.
        findings: Rules that fired on the order.
        ai_recommendation: An AiRecommendation value.
        ai_explanation: Plain-language summary of the findings ("" if none).
        status: Status to move to (IN_REVIEW or WAITING_ON_OM).

    Returns:
        None.
    """
    now = utc_now_iso()
    with get_connection() as connection:
        connection.execute(
            "DELETE FROM validation_results WHERE order_no = ?", (order_no,)
        )
        for finding in findings:
            connection.execute(
                "INSERT INTO validation_results "
                "(order_no, rule_id, rule_name, action, message, evaluated_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (
                    order_no,
                    finding.rule_id,
                    finding.rule_name,
                    finding.action,
                    finding.message,
                    now,
                ),
            )
        connection.execute(
            "UPDATE orders SET ai_recommendation = ?, ai_explanation = ?, status = ?, "
            "last_updated_at = ? WHERE order_no = ?",
            (ai_recommendation, ai_explanation, status, now, order_no),
        )


def get_validation_results(order_no: str) -> List[ValidationResult]:
    """Read an order's current findings.

    Args:
        order_no: Fusion order number.

    Returns:
        The findings, in the order they were evaluated.
    """
    with get_connection() as connection:
        rows = connection.execute(
            "SELECT * FROM validation_results WHERE order_no = ? ORDER BY id",
            (order_no,),
        ).fetchall()
    return [
        ValidationResult(
            order_no=row["order_no"],
            rule_id=row["rule_id"],
            rule_name=row["rule_name"],
            action=row["action"],
            message=row["message"],
        )
        for row in rows
    ]


def is_agreement(decision: str, ai_recommendation: Optional[str]) -> bool:
    """Tell whether a human decision agrees with the AI recommendation.

    Args:
        decision: A DecisionType value.
        ai_recommendation: An AiRecommendation value (or None).

    Returns:
        True for approve+approved and reject+sent back; False otherwise (incl. overrides).
    """
    return (
        decision == DecisionType.APPROVED.value
        and ai_recommendation == AiRecommendation.APPROVE.value
    ) or (
        decision == DecisionType.SENT_BACK.value
        and ai_recommendation == AiRecommendation.REJECT.value
    )


def record_decision(
    order_no: str, decision: str, decided_by: str, comment: Optional[str]
) -> None:
    """Append a human decision to the permanent log and note it on the order.

    Args:
        order_no: Fusion order number.
        decision: A DecisionType value.
        decided_by: Name of the MfgOps user.
        comment: Optional comment (required by callers for overrides and send-backs).

    Returns:
        None.
    """
    with get_connection() as connection:
        row = connection.execute(
            "SELECT ai_recommendation FROM orders WHERE order_no = ?", (order_no,)
        ).fetchone()
        ai_recommendation = row["ai_recommendation"] if row else None
        connection.execute(
            "INSERT INTO order_decisions (order_no, decision, decided_by, comment, "
            "ai_recommendation_at_time, agreed, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                order_no,
                decision,
                decided_by,
                comment,
                ai_recommendation,
                int(is_agreement(decision, ai_recommendation)),
                utc_now_iso(),
            ),
        )
        connection.execute(
            "UPDATE orders SET human_decision = ? WHERE order_no = ?",
            (decision, order_no),
        )


def mark_released(order_no: str, release_request_id: str) -> None:
    """Record that the release job was submitted; the order now waits for its USPO.

    Args:
        order_no: Fusion order number.
        release_request_id: Id returned by the release job submission.

    Returns:
        None.
    """
    now = utc_now_iso()
    with get_connection() as connection:
        connection.execute(
            "UPDATE orders SET status = ?, release_request_id = ?, released_at = ?, "
            "last_updated_at = ? WHERE order_no = ?",
            (OrderStatus.AWAITING_USPO.value, release_request_id, now, now, order_no),
        )


def close_order(order_no: str, uspo_number: str) -> None:
    """Close an order: keep a permanent history row, then delete its working data.

    Args:
        order_no: Fusion order number.
        uspo_number: The USPO that confirmed Arrow received the order.

    Returns:
        None.
    """
    with get_connection() as connection:
        row = connection.execute(
            "SELECT * FROM orders WHERE order_no = ?", (order_no,)
        ).fetchone()
        if row is None:
            return
        connection.execute(
            "INSERT INTO order_history (order_no, order_type, route, first_seen_at, "
            "released_at, uspo_number, closed_at, ai_recommendation, final_decision, agreed, "
            "rework_count) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                order_no,
                row["order_type"],
                row["route"],
                row["first_seen_at"],
                row["released_at"],
                uspo_number,
                utc_now_iso(),
                row["ai_recommendation"],
                row["human_decision"],
                int(is_agreement(row["human_decision"], row["ai_recommendation"])),
                row["rework_count"],
            ),
        )
        # Lines and findings go with it (ON DELETE CASCADE).
        connection.execute("DELETE FROM orders WHERE order_no = ?", (order_no,))


def get_order_history(search: str = "") -> List[Dict]:
    """Read closed orders, newest first.

    Args:
        search: Optional text to match in the order number or USPO.

    Returns:
        A list of dicts, one per closed order.
    """
    pattern = f"%{search.strip()}%"
    with get_connection() as connection:
        rows = connection.execute(
            "SELECT * FROM order_history WHERE order_no LIKE ? OR uspo_number LIKE ? "
            "ORDER BY closed_at DESC",
            (pattern, pattern),
        ).fetchall()
    return [dict(row) for row in rows]


def get_order_history_metrics(minutes_saved_per_order: float) -> Dict:
    """Compute the numbers shown on the Analytics tab.

    Args:
        minutes_saved_per_order: Assumed manual minutes saved per closed order.

    Returns:
        A dict of metrics (counts, rates, hours saved, breakdowns by route and status).
    """
    with get_connection() as connection:
        closed = connection.execute(
            "SELECT COUNT(*) AS total, COALESCE(SUM(rework_count = 0), 0) AS first_pass "
            "FROM order_history"
        ).fetchone()
        decisions = connection.execute(
            "SELECT COUNT(*) AS total, COALESCE(SUM(agreed), 0) AS agreed, "
            "COALESCE(SUM(decision = ?), 0) AS overrides, "
            "COALESCE(SUM(decision = ?), 0) AS sent_back FROM order_decisions",
            (DecisionType.PUSHED_OVERRIDE.value, DecisionType.SENT_BACK.value),
        ).fetchone()
        by_route = connection.execute(
            "SELECT route, COUNT(*) AS total FROM order_history GROUP BY route"
        ).fetchall()
        by_status = connection.execute(
            "SELECT status, COUNT(*) AS total FROM orders GROUP BY status"
        ).fetchall()
        cycle = connection.execute(
            "SELECT AVG((julianday(released_at) - julianday(first_seen_at)) * 24 * 60) "
            "AS minutes FROM order_history WHERE released_at IS NOT NULL"
        ).fetchone()
    closed_total = closed["total"]
    decisions_total = decisions["total"]
    return {
        "orders_closed": closed_total,
        "first_pass_rate": (
            closed["first_pass"] / closed_total if closed_total else None
        ),
        "decisions_total": decisions_total,
        "agreement_rate": (
            decisions["agreed"] / decisions_total if decisions_total else None
        ),
        "overrides": decisions["overrides"],
        "sent_back": decisions["sent_back"],
        "hours_saved": closed_total * minutes_saved_per_order / 60,
        "avg_minutes_detected_to_released": cycle["minutes"],
        "closed_by_route": {row["route"]: row["total"] for row in by_route},
        "active_by_status": {row["status"]: row["total"] for row in by_status},
    }
