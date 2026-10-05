from src.intelligence_layer.order_router import decide_route, get_destination_country
from src.utils.mock_data import make_order
from src.utils.models import RouteDecision


def test_us_order_is_standard(seeded_db):
    route, reason = decide_route(
        make_order("A-1", "Renewal", "United States", "US", [])
    )
    assert route == RouteDecision.STANDARD
    assert "US" in reason


def test_china_order_is_eus(seeded_db):
    route, _ = decide_route(make_order("A-2", "Distributor", "China", "CN", []))
    assert route == RouteDecision.EUS


def test_uae_order_is_special_instructions(seeded_db):
    route, _ = decide_route(make_order("A-3", "Renewal", "UAE", "AE", []))
    assert route == RouteDecision.SPECIAL_INSTRUCTIONS


def test_special_instructions_customer_is_matched_by_name(seeded_db):
    order = make_order(
        "A-4",
        "Distributor",
        "Kenya",
        "KE",
        [],
        bill_to_customer="Exclusive Networks ME",
    )
    route, _ = decide_route(order)
    assert route == RouteDecision.SPECIAL_INSTRUCTIONS


def test_end_customer_country_wins_over_forwarder_ship_to(seeded_db):
    order = make_order(
        "A-5", "Distributor", "United States", "US", [], end_customer_country="cn"
    )
    assert get_destination_country(order) == "CN"
    assert decide_route(order)[0] == RouteDecision.EUS
