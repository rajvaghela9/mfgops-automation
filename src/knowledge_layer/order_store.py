"""Stores orders while they are being worked on, plus the permanent all-orders record.

Lifecycle: an order is inserted when first fetched and updated in place when OM corrects
it in Fusion. Once approved it waits (status APPROVED) until Fusion's USPO for it shows up
in a backlog report; then it is released and leaves the active `orders` table. Every order
ever seen keeps one row in `all_orders` (it feeds the Dashboard). Every reviewer decision
is kept in `order_decisions`. Dummy (demo) orders are always listed after real ones.
"""

import hashlib
import json
from dataclasses import asdict
from typing import Dict, List, Optional

from src.knowledge_layer.db import get_connection
from src.utils.models import (
    DUMMY_ORDER_PREFIX,
    Order,
    OrderLine,
    OrderStatus,
    ReviewerDecision,
    RuleFinding,
    Verdict,
)
from src.utils.time_helpers import utc_now_iso

# Header fields that come from Fusion (stored on every fetch).
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
    "ship_from",
    "fob",
    "freight_terms",
    "shipping_method",
    "shipping_instructions",
    "customer_po",
    "arrow_po",
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
# Fields Fusion itself changes while releasing an order (the USPO, the line status).
# They are left out of the fingerprint, so a release is never mistaken for an OM edit.
NOT_EDITED_BY_OM = {"arrow_po", "line_status"}
# Sort dummy orders after real ones, then oldest first.
DUMMY_LAST = (
    f"CASE WHEN substr(order_no, 1, {len(DUMMY_ORDER_PREFIX)}) = '{DUMMY_ORDER_PREFIX}' "
    "THEN 1 ELSE 0 END"
)


def compute_content_hash(order: Order) -> str:
    """Fingerprint the parts of an order that OM edits (header fields and lines).

    Args:
        order: The order.

    Returns:
        A SHA-256 hex digest that changes whenever OM edits the order.
    """
    content = {
        name: getattr(order, name)
        for name in FUSION_HEADER_FIELDS
        if name not in NOT_EDITED_BY_OM
    }
    content["lines"] = [
        {
            key: value
            for key, value in asdict(line).items()
            if key not in NOT_EDITED_BY_OM
        }
        for line in sorted(order.lines, key=lambda line: line.line_number)
    ]
    return hashlib.sha256(json.dumps(content, sort_keys=True).encode()).hexdigest()


def is_agreement(
    ai_verdict: Optional[str], reviewer_decision: Optional[str]
) -> Optional[bool]:
    """Tell whether the reviewer agreed with the AI.

    Only approve/reject pairs count; a hold on either side is neither.

    Args:
        ai_verdict: A Verdict value.
        reviewer_decision: A ReviewerDecision value.

    Returns:
        True or False, or None when either side held the order.
    """
    pairs = {
        (Verdict.APPROVE.value, ReviewerDecision.APPROVE.value): True,
        (Verdict.REJECT.value, ReviewerDecision.SEND_TO_OM.value): True,
        (Verdict.APPROVE.value, ReviewerDecision.SEND_TO_OM.value): False,
        (Verdict.REJECT.value, ReviewerDecision.APPROVE.value): False,
    }
    return pairs.get((ai_verdict, reviewer_decision))


def copy_to_all_orders(connection, order_no: str, **release_details) -> None:
    """Write the order's latest state into the permanent all_orders table.

    Args:
        connection: Open SQLite connection.
        order_no: Fusion order number.
        **release_details: Optional release_request_id, released_at, uspo, released_by.

    Returns:
        None.
    """
    row = connection.execute(
        "SELECT * FROM orders WHERE order_no = ?", (order_no,)
    ).fetchone()
    if row is None:
        return
    agreed = is_agreement(row["ai_verdict"], row["reviewer_decision"])
    values = {
        "order_no": order_no,
        "order_type": row["order_type"],
        "route": row["route"],
        "ship_to_country": row["ship_to_country"],
        "status": row["status"],
        "ai_verdict": row["ai_verdict"],
        "reviewer_decision": row["reviewer_decision"],
        "agreed": None if agreed is None else int(agreed),
        "decided_by": row["decided_by"],
        "reviewer_comment": row["reviewer_comment"],
        "rework_count": row["rework_count"],
        "first_seen_at": row["first_seen_at"],
        "last_updated_at": row["last_updated_at"],
        **release_details,
    }
    updates = ", ".join(
        f"{name} = excluded.{name}" for name in values if name != "order_no"
    )
    connection.execute(
        f"INSERT INTO all_orders ({', '.join(values)}) "
        f"VALUES ({', '.join('?' for _ in values)}) "
        f"ON CONFLICT(order_no) DO UPDATE SET {updates}",
        list(values.values()),
    )


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

    A changed order goes back to NEW so it is checked again from the start; its old
    findings, AI verdict and reviewer decision are cleared.

    Args:
        order: The fetched order.

    Returns:
        "new", "updated", "unchanged", or "already_released".
    """
    new_hash = compute_content_hash(order)
    raw_json = order.raw_json or json.dumps(asdict(order), default=str)
    now = utc_now_iso()
    header_values = [getattr(order, name) for name in FUSION_HEADER_FIELDS]
    with get_connection() as connection:
        released = connection.execute(
            "SELECT 1 FROM all_orders WHERE order_no = ? AND status = ?",
            (order.order_no, OrderStatus.RELEASED.value),
        ).fetchone()
        if released:
            return "already_released"
        existing = connection.execute(
            "SELECT content_hash, status FROM orders WHERE order_no = ?",
            (order.order_no,),
        ).fetchone()

        if existing is None:
            columns = ["order_no", *FUSION_HEADER_FIELDS, "documents_received"]
            columns += ["eus_case_id", "status", "content_hash", "raw_json"]
            columns += ["first_seen_at", "last_updated_at"]
            values = [order.order_no, *header_values, int(order.documents_received)]
            values += [order.eus_case_id, OrderStatus.NEW.value, new_hash, raw_json]
            values += [now, now]
            connection.execute(
                f"INSERT INTO orders ({', '.join(columns)}) "
                f"VALUES ({', '.join('?' for _ in columns)})",
                values,
            )
            insert_lines(connection, order)
            copy_to_all_orders(connection, order.order_no)
            return "new"

        if existing["content_hash"] == new_hash:
            return "unchanged"

        assignments = ", ".join(f"{name} = ?" for name in FUSION_HEADER_FIELDS)
        if existing["status"] == OrderStatus.APPROVED.value:
            # Already approved and released in Fusion: keep the decision, refresh the data.
            connection.execute(
                f"UPDATE orders SET {assignments}, content_hash = ?, raw_json = ?, "
                "last_updated_at = ? WHERE order_no = ?",
                (*header_values, new_hash, raw_json, now, order.order_no),
            )
            connection.execute(
                "DELETE FROM order_lines WHERE order_no = ?", (order.order_no,)
            )
            insert_lines(connection, order)
            return "unchanged"

        connection.execute(
            f"UPDATE orders SET {assignments}, status = ?, route = NULL, "
            "route_reason = NULL, ai_verdict = NULL, ai_used = NULL, ai_summary = NULL, "
            "ai_reasons_json = NULL, reviewer_decision = NULL, reviewer_comment = NULL, "
            "decided_by = NULL, review_thread_id = NULL, approved_at = NULL, "
            "rework_count = rework_count + 1, content_hash = ?, raw_json = ?, "
            "last_updated_at = ? WHERE order_no = ?",
            (
                *header_values,
                OrderStatus.NEW.value,
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
            "DELETE FROM rule_findings WHERE order_no = ?", (order.order_no,)
        )
        insert_lines(connection, order)
        copy_to_all_orders(connection, order.order_no)
        return "updated"


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
    data["ai_reasons"] = json.loads(row["ai_reasons_json"] or "[]")
    data["ai_used"] = row["ai_used"] != 0  # not reviewed yet (None) counts as fine
    return Order(**data, lines=lines)


def get_order(order_no: str) -> Optional[Order]:
    """Read one active order with its lines.

    Args:
        order_no: Fusion order number.

    Returns:
        The Order, or None if it isn't active (never seen, or already released).
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
        Orders (with lines): real orders oldest first, then dummy orders.
    """
    query, params = "SELECT order_no FROM orders", ()
    if statuses:
        query += f" WHERE status IN ({', '.join('?' for _ in statuses)})"
        params = tuple(statuses)
    with get_connection() as connection:
        order_nos = [
            row["order_no"]
            for row in connection.execute(
                f"{query} ORDER BY {DUMMY_LAST}, first_seen_at", params
            )
        ]
    return [order for order in map(get_order, order_nos) if order is not None]


def update_order(order_no: str, **changes) -> None:
    """Change some workflow fields of an active order and refresh its all_orders row.

    Args:
        order_no: Fusion order number.
        **changes: Column name to new value, e.g. status="ON_HOLD".

    Returns:
        None.
    """
    if "ai_used" in changes:
        changes["ai_used"] = int(changes["ai_used"])
    if "ai_reasons" in changes:
        changes["ai_reasons_json"] = json.dumps(changes.pop("ai_reasons"))
    changes["last_updated_at"] = utc_now_iso()
    with get_connection() as connection:
        connection.execute(
            f"UPDATE orders SET {', '.join(f'{name} = ?' for name in changes)} "
            "WHERE order_no = ?",
            [*changes.values(), order_no],
        )
        copy_to_all_orders(connection, order_no)


def save_rule_findings(order_no: str, findings: List[RuleFinding]) -> None:
    """Replace an order's hard-rule findings.

    Args:
        order_no: Fusion order number.
        findings: Hard rules that found a problem.

    Returns:
        None.
    """
    now = utc_now_iso()
    with get_connection() as connection:
        connection.execute("DELETE FROM rule_findings WHERE order_no = ?", (order_no,))
        for finding in findings:
            connection.execute(
                "INSERT INTO rule_findings (order_no, rule_id, rule_name, outcome, "
                "message, can_be_overridden, checked_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    order_no,
                    finding.rule_id,
                    finding.rule_name,
                    finding.outcome,
                    finding.message,
                    int(finding.can_be_overridden),
                    now,
                ),
            )


def get_rule_findings(order_no: str) -> List[RuleFinding]:
    """Read an order's current hard-rule findings.

    Args:
        order_no: Fusion order number.

    Returns:
        The findings, in the order they were checked.
    """
    with get_connection() as connection:
        rows = connection.execute(
            "SELECT * FROM rule_findings WHERE order_no = ? ORDER BY id", (order_no,)
        ).fetchall()
    return [
        RuleFinding(
            rule_id=row["rule_id"],
            rule_name=row["rule_name"],
            outcome=row["outcome"],
            message=row["message"],
            can_be_overridden=bool(row["can_be_overridden"]),
        )
        for row in rows
    ]


def record_reviewer_decision(
    order_no: str, decision: str, decided_by: str, comment: Optional[str]
) -> None:
    """Note the reviewer's decision on the order and append it to the permanent log.

    Args:
        order_no: Fusion order number.
        decision: A ReviewerDecision value.
        decided_by: Name of the MfgOps user.
        comment: The reviewer's comment, if any.

    Returns:
        None.
    """
    order = get_order(order_no)
    ai_verdict = order.ai_verdict if order else None
    agreed = is_agreement(ai_verdict, decision)
    with get_connection() as connection:
        connection.execute(
            "INSERT INTO order_decisions (order_no, decision, decided_by, comment, "
            "ai_verdict_at_time, agreed, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                order_no,
                decision,
                decided_by,
                comment,
                ai_verdict,
                None if agreed is None else int(agreed),
                utc_now_iso(),
            ),
        )
    update_order(
        order_no,
        reviewer_decision=decision,
        reviewer_comment=comment,
        decided_by=decided_by,
    )


def mark_released(
    order_no: str,
    release_request_id: str = "",
    uspo: str = "",
    released_by: str = "",
) -> None:
    """Record the release in all_orders, then remove the order from the active tables.

    Args:
        order_no: Fusion order number.
        release_request_id: Id of the release job submission (live Fusion only).
        uspo: Fusion's USPO, as read from the backlog report ("" for dummy orders).
        released_by: Who released it, e.g. "Fusion (outside this tool)"; "" means the
            reviewer who approved it.

    Returns:
        None.
    """
    now = utc_now_iso()
    with get_connection() as connection:
        connection.execute(
            "UPDATE orders SET status = ?, last_updated_at = ? WHERE order_no = ?",
            (OrderStatus.RELEASED.value, now, order_no),
        )
        copy_to_all_orders(
            connection,
            order_no,
            release_request_id=release_request_id,
            released_at=now,
            uspo=uspo,
            released_by=released_by,
        )
        # Lines and findings go with it (ON DELETE CASCADE).
        connection.execute("DELETE FROM orders WHERE order_no = ?", (order_no,))


def backfill_uspo(order_no: str, uspo: str) -> bool:
    """Store the USPO on a released order that doesn't have it yet.

    Args:
        order_no: Fusion order number.
        uspo: The USPO from the backlog report.

    Returns:
        True if the order is known in all_orders (whether or not it needed the USPO).
    """
    with get_connection() as connection:
        row = connection.execute(
            "SELECT uspo FROM all_orders WHERE order_no = ?", (order_no,)
        ).fetchone()
        if row is None:
            return False
        if not row["uspo"]:
            connection.execute(
                "UPDATE all_orders SET uspo = ?, last_updated_at = ? WHERE order_no = ?",
                (uspo, utc_now_iso(), order_no),
            )
    return True


def mark_removed_from_backlog(order_no: str) -> None:
    """Close an order that is no longer in the Fusion backlog (e.g. cancelled by OM).

    Args:
        order_no: Fusion order number.

    Returns:
        None.
    """
    with get_connection() as connection:
        connection.execute(
            "UPDATE orders SET status = ?, last_updated_at = ? WHERE order_no = ?",
            (OrderStatus.REMOVED.value, utc_now_iso(), order_no),
        )
        copy_to_all_orders(connection, order_no)
        connection.execute("DELETE FROM orders WHERE order_no = ?", (order_no,))


def note_orders_missing_from_backlog(present_order_nos: List[str]) -> Dict[str, int]:
    """Count, per active order, how many backlog reports in a row no longer listed it.

    Orders missing from two reports in a row are closed as "no longer in the backlog",
    except approved orders (the reviewer is told to check Fusion instead) and dummy
    orders (they are never in Fusion).

    Args:
        present_order_nos: Every order number in the newest backlog report.

    Returns:
        {"removed": n, "missing": n} for this report.
    """
    present = set(present_order_nos)
    counts = {"removed": 0, "missing": 0}
    for order in get_active_orders():
        if order.order_no.startswith(DUMMY_ORDER_PREFIX):
            continue
        if order.order_no in present:
            if order.missing_snapshot_count:
                update_order(order.order_no, missing_snapshot_count=0)
            continue
        missing_count = order.missing_snapshot_count + 1
        if missing_count >= 2 and order.status != OrderStatus.APPROVED.value:
            mark_removed_from_backlog(order.order_no)
            counts["removed"] += 1
        else:
            update_order(order.order_no, missing_snapshot_count=missing_count)
            counts["missing"] += 1
    return counts


def get_all_orders(search: str = "") -> List[Dict]:
    """Read every order ever seen, newest activity first.

    Args:
        search: Optional text to match in the order number or country.

    Returns:
        A list of dicts, one per order (dummy orders last).
    """
    pattern = f"%{search.strip()}%"
    with get_connection() as connection:
        rows = connection.execute(
            "SELECT * FROM all_orders WHERE order_no LIKE ? OR ship_to_country LIKE ? "
            f"ORDER BY {DUMMY_LAST}, last_updated_at DESC",
            (pattern, pattern),
        ).fetchall()
    return [dict(row) for row in rows]


def get_dashboard_numbers(minutes_saved_per_order: float) -> Dict:
    """Compute the numbers shown on the Dashboard, from the all_orders table.

    Args:
        minutes_saved_per_order: Assumed manual minutes saved per reviewed order.

    Returns:
        A dict of counts, rates, hours saved, and breakdowns by category and status.
    """
    with get_connection() as connection:
        totals = connection.execute(
            "SELECT COUNT(*) AS seen, "
            "COALESCE(SUM(status = ?), 0) AS released, "
            "COALESCE(SUM(status = ?), 0) AS sent_to_om, "
            "COALESCE(SUM(status IN (?, ?)), 0) AS waiting, "
            "COALESCE(SUM(status = ?), 0) AS approved_waiting, "
            "COALESCE(SUM(ai_verdict IS NOT NULL), 0) AS checked_by_ai, "
            "COUNT(agreed) AS compared, COALESCE(SUM(agreed), 0) AS agreed, "
            "COALESCE(SUM(rework_count = 0 AND status = ?), 0) AS first_time_right "
            "FROM all_orders",
            (
                OrderStatus.RELEASED.value,
                OrderStatus.SENT_TO_OM.value,
                OrderStatus.WAITING_FOR_REVIEW.value,
                OrderStatus.ON_HOLD.value,
                OrderStatus.APPROVED.value,
                OrderStatus.RELEASED.value,
            ),
        ).fetchone()
        by_route = connection.execute(
            "SELECT route, COUNT(*) AS total FROM all_orders "
            "WHERE route IS NOT NULL GROUP BY route"
        ).fetchall()
        by_status = connection.execute(
            "SELECT status, COUNT(*) AS total FROM all_orders GROUP BY status"
        ).fetchall()
    return {
        "orders_seen": totals["seen"],
        "released": totals["released"],
        "sent_to_om": totals["sent_to_om"],
        "waiting": totals["waiting"],
        "approved_waiting": totals["approved_waiting"],
        "agreement_rate": (
            totals["agreed"] / totals["compared"] if totals["compared"] else None
        ),
        "first_time_right_rate": (
            totals["first_time_right"] / totals["released"]
            if totals["released"]
            else None
        ),
        "hours_saved": totals["checked_by_ai"] * minutes_saved_per_order / 60,
        "by_route": {row["route"]: row["total"] for row in by_route},
        "by_status": {row["status"]: row["total"] for row in by_status},
    }
