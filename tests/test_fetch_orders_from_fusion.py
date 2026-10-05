import pytest

from src.integration_layer.fetch_orders_from_fusion import fetch_new_orders, sync_orders
from src.integration_layer.push_orders_to_b2b import push_order, verify_released_orders
from src.integration_layer.send_back_to_om import send_back_to_om
from src.intelligence_layer.order_validation import validate_pending_orders
from src.knowledge_layer import order_store
from src.utils.models import OrderStatus


def test_live_mode_says_what_is_missing(monkeypatch):
    monkeypatch.setenv("FUSION_MODE", "live")
    with pytest.raises(NotImplementedError, match="Infoblox Orders Backlog Report"):
        fetch_new_orders()


def test_sync_skips_returns_and_sets_route(seeded_db):
    counts = sync_orders()
    assert counts["excluded"] == 1
    assert order_store.get_order("RMA-MOCK-0006") is None
    assert order_store.get_order("DIST-MOCK-0004").route == "eus"


def test_sent_back_order_comes_back_corrected_and_is_revalidated(
    seeded_db, isolated_environment
):
    sync_orders()
    validate_pending_orders()
    assert order_store.get_order("DIST-MOCK-0003").ai_recommendation == "reject"

    send_back_to_om(
        "DIST-MOCK-0003", "Tester", "Please add shipping method, PO and phone."
    )
    assert (
        order_store.get_order("DIST-MOCK-0003").status
        == OrderStatus.SENT_BACK_TO_OM.value
    )
    assert len(list((isolated_environment / "outbox").iterdir())) == 1

    assert sync_orders()["updated_in_place"] == 1  # mock OM fixed it
    validate_pending_orders()
    corrected = order_store.get_order("DIST-MOCK-0003")
    assert corrected.ai_recommendation == "approve"
    assert corrected.rework_count == 1


def test_push_rules_and_close_after_uspo(seeded_db):
    sync_orders()
    validate_pending_orders()

    with pytest.raises(ValueError, match="Override"):
        push_order("FEDR-1103736", "Tester")  # AI says reject
    with pytest.raises(ValueError, match="comment"):
        push_order("FEDR-1103736", "Tester", override=True)

    assert push_order(
        "DIST-MOCK-0004", "Tester", "EUS approved by email", override=True
    )
    assert push_order("REN-1103701", "Tester")
    assert not push_order("REN-1103701", "Tester")  # already pushed: no double release

    assert verify_released_orders() == 2
    assert order_store.get_order("REN-1103701") is None
    assert {row["order_no"] for row in order_store.get_order_history()} == {
        "REN-1103701",
        "DIST-MOCK-0004",
    }
