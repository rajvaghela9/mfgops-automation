from unittest.mock import patch

import pytest

from src.app import run_pipeline_once
from src.integration_layer import fetch_orders_from_fusion
from src.integration_layer.fetch_orders_from_fusion import fetch_new_orders, sync_orders
from src.integration_layer.push_orders_to_b2b import release_order_to_arrow
from src.knowledge_layer import order_store
from src.utils.mock_data import get_dummy_orders
from tests.sample_orders import get_sample_orders


def all_orders():
    return {row["order_no"]: row for row in order_store.get_all_orders()}


def test_live_mode_says_what_is_missing(monkeypatch):
    monkeypatch.setenv("FUSION_MODE", "live")
    with pytest.raises(NotImplementedError, match="Infoblox Orders Backlog Report"):
        fetch_new_orders()
    with pytest.raises(NotImplementedError, match="Priority Release Pause Task"):
        release_order_to_arrow("A-1")


def test_mock_mode_has_only_the_five_dummy_orders(seeded_db):
    orders = fetch_new_orders()
    assert [order.order_no for order in orders] == [
        order.order_no for order in get_dummy_orders()
    ]
    assert all(order.order_no.startswith("dummy_") for order in orders)
    assert not any(order.arrow_po for order in orders)


def test_sync_skips_returns_and_stores_new_orders(seeded_db, sample_backlog):
    counts = sync_orders()
    assert counts["excluded"] == 1 and counts["new"] == 14
    assert order_store.get_order("RMA-MOCK-0006") is None
    assert order_store.get_order("DIST-MOCK-0004").status == "NEW"


def test_orders_with_a_uspo_are_not_reviewed(seeded_db, sample_backlog):
    sample_backlog["REN-1103701"] = "USPO1"
    counts = sync_orders()
    assert counts["already_sent_to_arrow"] == 1
    assert order_store.get_order("REN-1103701") is None
    assert "REN-1103701" not in all_orders()  # nothing to review: not stored


def test_uspo_for_an_order_nobody_approved_closes_it(seeded_db, sample_backlog):
    run_pipeline_once()
    sample_backlog["FEDR-1103736"] = "USPO7"
    assert sync_orders()["released_outside_tool"] == 1
    assert order_store.get_order("FEDR-1103736") is None
    record = all_orders()["FEDR-1103736"]
    assert (record["status"], record["uspo"], record["released_by"]) == (
        "RELEASED",
        "USPO7",
        "Fusion (outside this tool)",
    )


def test_mail_mode_with_no_new_report_changes_nothing(seeded_db, monkeypatch):
    monkeypatch.setenv("FUSION_MODE", "mail")
    with patch.object(
        fetch_orders_from_fusion, "fetch_backlog_snapshot", return_value=None
    ):
        counts = sync_orders()
    assert counts["new_report"] == 0 and counts["new"] == 0


def test_mail_mode_reads_the_report_and_adds_the_dummy_orders(seeded_db, monkeypatch):
    monkeypatch.setenv("FUSION_MODE", "mail")
    report = get_sample_orders()[:3]
    snapshot = type("Snapshot", (), {"orders": report})()
    with patch.object(
        fetch_orders_from_fusion, "fetch_backlog_snapshot", return_value=snapshot
    ):
        orders = fetch_new_orders()
        assert len(orders) == 3 + 5
        monkeypatch.setenv("INCLUDE_DEMO_ORDERS", "false")
        assert len(fetch_new_orders()) == 3


def test_mail_mode_closes_orders_that_left_the_backlog(seeded_db, monkeypatch):
    monkeypatch.setenv("FUSION_MODE", "mail")
    monkeypatch.setenv("INCLUDE_DEMO_ORDERS", "false")
    orders = get_sample_orders()[:4]

    def sync_with(report):
        snapshot = type("Snapshot", (), {"orders": report})()
        with patch.object(
            fetch_orders_from_fusion, "fetch_backlog_snapshot", return_value=snapshot
        ):
            return sync_orders()

    sync_with(orders)
    sync_with(orders[:3])  # missing once: kept
    assert order_store.get_order(orders[3].order_no) is not None
    assert sync_with(orders[:3])["removed_from_backlog"] == 1
    assert order_store.get_order(orders[3].order_no) is None
    assert sync_with(orders[:1])["removed_from_backlog"] == 0  # looks broken: skipped
