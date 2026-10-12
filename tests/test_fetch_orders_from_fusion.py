import pytest

from src.integration_layer.fetch_orders_from_fusion import fetch_new_orders, sync_orders
from src.integration_layer.push_orders_to_b2b import release_order_to_arrow
from src.knowledge_layer import order_store


def test_live_mode_says_what_is_missing(monkeypatch):
    monkeypatch.setenv("FUSION_MODE", "live")
    with pytest.raises(NotImplementedError, match="Infoblox Orders Backlog Report"):
        fetch_new_orders()
    with pytest.raises(NotImplementedError, match="Priority Release Pause Task"):
        release_order_to_arrow("A-1")


def test_sync_skips_returns_and_stores_new_orders(seeded_db):
    counts = sync_orders()
    assert counts["excluded"] == 1 and counts["new"] == 14
    assert order_store.get_order("RMA-MOCK-0006") is None
    assert order_store.get_order("DIST-MOCK-0004").status == "NEW"
