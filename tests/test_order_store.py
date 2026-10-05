from dataclasses import replace

from src.knowledge_layer import order_store
from src.utils.mock_data import get_mock_orders
from src.utils.models import OrderStatus


def first_mock_order():
    order = get_mock_orders()[0]
    order.route, order.route_reason = "standard", "test"
    return order


def test_insert_then_unchanged(seeded_db):
    order = first_mock_order()
    assert order_store.upsert_order(order) == "inserted"
    assert order_store.upsert_order(order) == "unchanged"
    stored = order_store.get_order(order.order_no)
    assert stored.status == OrderStatus.ROUTED.value
    assert len(stored.lines) == len(order.lines)


def test_corrected_order_is_updated_in_place_and_revalidated(seeded_db):
    order = first_mock_order()
    order_store.upsert_order(order)
    order_store.save_validation(
        order.order_no, [], "approve", "", OrderStatus.IN_REVIEW.value
    )

    corrected = replace(order, shipping_method="DHL Express")
    assert order_store.upsert_order(corrected) == "updated_in_place"
    stored = order_store.get_order(order.order_no)
    assert stored.shipping_method == "DHL Express"
    assert stored.status == OrderStatus.ROUTED.value
    assert stored.ai_recommendation is None
    assert stored.rework_count == 1


def test_close_moves_order_to_history_and_never_reinserts_it(seeded_db):
    order = first_mock_order()
    order_store.upsert_order(order)
    order_store.save_validation(
        order.order_no, [], "approve", "", OrderStatus.IN_REVIEW.value
    )
    order_store.record_decision(order.order_no, "approved", "Tester", None)
    order_store.mark_released(order.order_no, "ESS-1")
    order_store.close_order(order.order_no, "USPO-1")

    assert order_store.get_order(order.order_no) is None
    history = order_store.get_order_history()
    assert history[0]["order_no"] == order.order_no
    assert history[0]["uspo_number"] == "USPO-1"
    assert history[0]["agreed"] == 1
    assert order_store.upsert_order(order) == "already_closed"


def test_agreement_rules():
    assert order_store.is_agreement("approved", "approve")
    assert order_store.is_agreement("sent_back", "reject")
    assert not order_store.is_agreement("pushed_override", "reject")
    assert not order_store.is_agreement("sent_back", "approve")


def test_metrics_count_decisions_and_closed_orders(seeded_db):
    order = first_mock_order()
    order_store.upsert_order(order)
    order_store.save_validation(
        order.order_no, [], "reject", "x", OrderStatus.IN_REVIEW.value
    )
    order_store.record_decision(
        order.order_no, "pushed_override", "Tester", "EUS ok by email"
    )
    order_store.mark_released(order.order_no, "ESS-1")
    order_store.close_order(order.order_no, "USPO-1")

    metrics = order_store.get_order_history_metrics(minutes_saved_per_order=12)
    assert metrics["orders_closed"] == 1
    assert metrics["overrides"] == 1
    assert metrics["agreement_rate"] == 0
    assert metrics["hours_saved"] == 0.2
    assert metrics["closed_by_route"] == {"standard": 1}
