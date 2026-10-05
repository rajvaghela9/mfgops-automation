"""Decides which route an order follows: Standard, Special Instructions only, or EUS.

Fully deterministic: it only looks up the destination and customer in reference lists.
"""

from typing import Dict, Optional, Tuple

from src.knowledge_layer import reference_store
from src.utils.models import Order, RouteDecision


def get_destination_country(order: Order) -> str:
    """Pick the country that matters for compliance and power cords.

    For distributor or forwarder shipments the end-customer country wins over ship-to.

    Args:
        order: The order.

    Returns:
        An upper-case ISO country code, or "" if neither is known.
    """
    return (
        (order.end_customer_country or order.ship_to_country_code or "").strip().upper()
    )


def decide_route(
    order: Order, reference_lists: Optional[Dict[str, Dict[str, str]]] = None
) -> Tuple[RouteDecision, str]:
    """Decide which of the three routes an order should follow.

    Args:
        order: The order to classify.
        reference_lists: Preloaded reference lists; loaded from the store if None.

    Returns:
        A tuple of (route, human-readable reason) to store on the order.
    """
    lists = reference_lists or reference_store.get_all_reference_lists()
    destination = get_destination_country(order)

    eus_countries = lists.get(reference_store.EUS_COUNTRIES, {})
    if destination in eus_countries:
        return (
            RouteDecision.EUS,
            f"Destination {destination} is on the EUS country list ({eus_countries[destination]}).",
        )

    si_countries = lists.get(reference_store.SPECIAL_INSTRUCTION_COUNTRIES, {})
    if destination in si_countries:
        return (
            RouteDecision.SPECIAL_INSTRUCTIONS,
            f"Destination {destination} needs special instructions ({si_countries[destination]}).",
        )

    customers = f"{order.bill_to_customer} {order.ship_to_customer}".upper()
    for customer_key, description in lists.get(
        reference_store.SPECIAL_INSTRUCTION_CUSTOMERS, {}
    ).items():
        if customer_key in customers:
            return (
                RouteDecision.SPECIAL_INSTRUCTIONS,
                f"Customer matches special-instructions customer {description}.",
            )

    return (
        RouteDecision.STANDARD,
        f"No EUS country or special-instructions match for destination {destination or 'unknown'}.",
    )
