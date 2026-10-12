"""Decides an order's category: Standard, Special Instructions, EUS, or EUS + Special Instructions.

Fully deterministic: it only looks up the destination and customer in reference lists.
"""

from typing import Dict, Optional, Tuple

from src.knowledge_layer import reference_store
from src.utils.models import Order, OrderRoute


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


def find_special_instruction_reason(
    order: Order, destination: str, lists: Dict[str, Dict[str, str]]
) -> Optional[str]:
    """Check whether the destination or customer needs special instructions.

    Args:
        order: The order.
        destination: Its destination country code.
        lists: All reference lists.

    Returns:
        A readable reason, or None if no special instructions apply.
    """
    si_countries = lists.get(reference_store.SPECIAL_INSTRUCTION_COUNTRIES, {})
    if destination in si_countries:
        return f"{destination} orders need special instructions ({si_countries[destination]})."
    customers = f"{order.bill_to_customer} {order.ship_to_customer}".upper()
    for customer_key, description in lists.get(
        reference_store.SPECIAL_INSTRUCTION_CUSTOMERS, {}
    ).items():
        if customer_key in customers:
            return f"Customer {description} has special instructions."
    return None


def decide_route(
    order: Order, reference_lists: Optional[Dict[str, Dict[str, str]]] = None
) -> Tuple[OrderRoute, str]:
    """Decide which of the four categories an order belongs to.

    Args:
        order: The order to classify.
        reference_lists: Preloaded reference lists; loaded from the store if None.

    Returns:
        A tuple of (category, human-readable reason) to store on the order.
    """
    lists = reference_lists or reference_store.get_all_reference_lists()
    destination = get_destination_country(order)
    eus_countries = lists.get(reference_store.EUS_COUNTRIES, {})
    eus_reason = (
        f"{destination} needs an End-Use Statement ({eus_countries[destination]})."
        if destination in eus_countries
        else None
    )
    si_reason = find_special_instruction_reason(order, destination, lists)

    if eus_reason and si_reason:
        return OrderRoute.EUS_AND_SPECIAL_INSTRUCTIONS, f"{eus_reason} {si_reason}"
    if eus_reason:
        return OrderRoute.EUS, eus_reason
    if si_reason:
        return OrderRoute.SPECIAL_INSTRUCTIONS, si_reason
    return (
        OrderRoute.STANDARD,
        f"No EUS or special instructions needed for {destination or 'an unknown country'}.",
    )
