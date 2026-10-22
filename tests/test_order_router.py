from src.intelligence_layer.order_router import decide_route, get_destination_country
from src.knowledge_layer import reference_store
from src.utils.mock_data import make_order
from src.utils.models import OrderRoute


def test_us_order_is_standard(seeded_db):
    route, reason = decide_route(
        make_order("A-1", "Renewal", "United States", "US", [])
    )
    assert route == OrderRoute.STANDARD
    assert "US" in reason


def test_eus_country(seeded_db):
    route, _ = decide_route(make_order("A-2", "Distributor", "China", "CN", []))
    assert route == OrderRoute.EUS


def test_workbook_countries_and_south_korea_require_eus(seeded_db):
    countries = reference_store.get_reference_list(reference_store.EUS_COUNTRIES)

    assert set(countries) == {
        "AF",
        "AM",
        "AZ",
        "CD",
        "CF",
        "CN",
        "ER",
        "GE",
        "HK",
        "IQ",
        "KG",
        "KH",
        "KR",
        "KZ",
        "LA",
        "LB",
        "LY",
        "MD",
        "MN",
        "MO",
        "PK",
        "SD",
        "SO",
        "SS",
        "TJ",
        "TM",
        "UA",
        "UZ",
        "VN",
        "YE",
        "ZW",
    }
    assert countries["HK"] == "Hong Kong (with import licence)"
    for code in ("AF", "CN", "HK", "KR", "UA", "VN", "ZW"):
        route, _ = decide_route(
            make_order(f"EUS-{code}", "Distributor", countries[code], code, [])
        )
        assert route == OrderRoute.EUS


def test_mandatory_eus_countries_are_backfilled(seeded_db):
    reference_store.delete_reference_value(reference_store.EUS_COUNTRIES, "KR")

    reference_store.seed_reference_lists()

    countries = reference_store.get_reference_list(reference_store.EUS_COUNTRIES)
    assert countries["KR"] == "South Korea"


def test_special_instruction_country(seeded_db):
    route, reason = decide_route(make_order("A-3", "Distributor", "UAE", "AE", []))
    assert route == OrderRoute.SPECIAL_INSTRUCTIONS
    assert "blue-ink" in reason


def test_special_instruction_customer(seeded_db):
    order = make_order(
        "A-4",
        "Distributor",
        "Germany",
        "DE",
        [],
        bill_to_customer="Exclusive Networks ME",
    )
    assert decide_route(order)[0] == OrderRoute.SPECIAL_INSTRUCTIONS


def test_eus_and_special_instructions(seeded_db):
    order = make_order(
        "A-5", "Distributor", "Iraq", "IQ", [], bill_to_customer="EXCLUSIVE NETWORKS ME"
    )
    route, reason = decide_route(order)
    assert route == OrderRoute.EUS_AND_SPECIAL_INSTRUCTIONS
    assert "End-Use Statement" in reason and "special instructions" in reason


def test_end_customer_country_wins_over_ship_to():
    order = make_order(
        "A-6", "Distributor", "Singapore", "SG", [], end_customer_country="cn"
    )
    assert get_destination_country(order) == "CN"
