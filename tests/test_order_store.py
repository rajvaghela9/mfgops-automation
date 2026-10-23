from dataclasses import replace

from src.knowledge_layer import order_store
from tests.sample_orders import get_sample_orders
from src.utils.models import OrderStatus, is_dummy_order


def test_insert_unchanged_and_updated_in_place(seeded_db):
    order = get_sample_orders()[0]
    assert order_store.upsert_order(order) == "new"
    assert order_store.upsert_order(order) == "unchanged"
    order_store.update_order(
        order.order_no, ai_verdict="approve", status="WAITING_FOR_REVIEW"
    )
    assert order_store.upsert_order(replace(order, customer_po="NEW-PO")) == "updated"
    stored = order_store.get_order(order.order_no)
    assert (stored.status, stored.ai_verdict, stored.rework_count) == ("NEW", None, 1)


def test_released_order_is_not_fetched_again(seeded_db):
    order = get_sample_orders()[0]
    order_store.upsert_order(order)
    order_store.mark_released(order.order_no, "ESS-1")
    assert order_store.get_order(order.order_no) is None
    assert order_store.upsert_order(order) == "already_released"
    assert order_store.get_all_orders()[0]["status"] == OrderStatus.RELEASED.value


def test_agreement_only_counts_approve_and_reject():
    assert order_store.is_agreement("approve", "approve") is True
    assert order_store.is_agreement("reject", "send_to_om") is True
    assert order_store.is_agreement("reject", "approve") is False
    assert order_store.is_agreement("approve", "send_to_om") is False
    assert order_store.is_agreement("hold", "approve") is None


def test_dashboard_numbers(seeded_db):
    for order in get_sample_orders()[:2]:
        order_store.upsert_order(order)
    first = get_sample_orders()[0].order_no
    order_store.update_order(first, ai_verdict="approve")
    order_store.record_reviewer_decision(first, "approve", "Tester", None)
    order_store.mark_released(first, "ESS-1")
    numbers = order_store.get_dashboard_numbers(minutes_saved_per_order=30)
    assert (numbers["orders_seen"], numbers["released"]) == (2, 1)
    assert numbers["agreement_rate"] == 1.0
    assert numbers["hours_saved"] == 0.5


def test_an_older_database_is_upgraded(tmp_path, monkeypatch):
    import sqlite3

    old_db = tmp_path / "old.db"
    with sqlite3.connect(old_db) as connection:
        connection.execute(
            "CREATE TABLE orders (order_no TEXT PRIMARY KEY, ai_summary TEXT)"
        )
        connection.execute(
            "INSERT INTO orders VALUES ('A-1', 'AI review not available (the AI is off).')"
        )
        connection.execute("INSERT INTO orders VALUES ('A-2', 'All good.')")
    monkeypatch.setenv("DB_PATH", str(old_db))
    from src.knowledge_layer.db import get_connection, init_db

    init_db()
    init_db()  # safe to repeat
    with get_connection() as connection:
        rows = connection.execute(
            "SELECT order_no, ai_used FROM orders ORDER BY 1"
        ).fetchall()
    assert [(row["order_no"], row["ai_used"]) for row in rows] == [
        ("A-1", 0),
        ("A-2", None),
    ]


def test_fusion_release_changes_are_not_mistaken_for_om_edits(seeded_db):
    order = get_sample_orders()[0]
    order_store.upsert_order(order)
    released_lines = [replace(line, line_status="AWAIT_SHIP") for line in order.lines]
    assert order_store.upsert_order(replace(order, lines=released_lines)) == "unchanged"
    assert order_store.upsert_order(replace(order, lines=order.lines[::-1])) == (
        "unchanged"
    )  # the report lists lines in any order


def test_an_approved_order_is_never_sent_back_to_review(seeded_db):
    order = get_sample_orders()[0]
    order_store.upsert_order(order)
    order_store.update_order(order.order_no, status=OrderStatus.APPROVED.value)
    assert order_store.upsert_order(replace(order, fob="FCA")) == "unchanged"
    stored = order_store.get_order(order.order_no)
    assert (stored.status, stored.fob, stored.rework_count) == ("APPROVED", "FCA", 0)


def test_dummy_orders_are_listed_last(seeded_db):
    from src.utils.mock_data import get_dummy_orders

    for order in [*get_dummy_orders()[:2], *get_sample_orders()[:2]]:
        order_store.upsert_order(order)
    listed = [order.order_no for order in order_store.get_active_orders()]
    assert [is_dummy_order(number) for number in listed] == [False, False, True, True]
    everything = [row["order_no"] for row in order_store.get_all_orders()]
    assert [is_dummy_order(number) for number in everything][-2:] == [True, True]


def test_released_order_keeps_its_uspo(seeded_db):
    order = get_sample_orders()[0]
    order_store.upsert_order(order)
    order_store.mark_released(order.order_no, uspo="USPO1", released_by="Tester")
    row = order_store.get_all_orders()[0]
    assert (row["uspo"], row["released_by"]) == ("USPO1", "Tester")
    assert order_store.backfill_uspo(order.order_no, "USPO2")  # known; kept as USPO1
    assert order_store.get_all_orders()[0]["uspo"] == "USPO1"
    assert not order_store.backfill_uspo("NEVER-SEEN", "USPO3")


def test_orders_missing_from_two_reports_in_a_row_are_closed(seeded_db):
    from src.utils.mock_data import get_dummy_orders

    first, second, approved = get_sample_orders()[:3]
    dummy = get_dummy_orders()[0]
    for order in [first, second, approved, dummy]:
        order_store.upsert_order(order)
    order_store.update_order(approved.order_no, status=OrderStatus.APPROVED.value)
    present = [first.order_no]
    assert order_store.note_orders_missing_from_backlog(present) == {
        "removed": 0,
        "missing": 2,
    }
    assert order_store.note_orders_missing_from_backlog(present)["removed"] == 1
    assert order_store.get_order(second.order_no) is None
    statuses = {row["order_no"]: row["status"] for row in order_store.get_all_orders()}
    assert statuses[second.order_no] == OrderStatus.REMOVED.value
    assert order_store.get_order(approved.order_no).missing_snapshot_count == 2
    assert order_store.get_order(dummy.order_no) is not None  # never in Fusion
