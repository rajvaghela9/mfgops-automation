"""The five dummy orders, plus helpers to build orders shaped like the backlog report.

Real orders come from the Fusion backlog report mails. The dummy orders are invented to
show the AI rejecting or holding orders; their numbers start with "dummy_" so nobody
mistakes them for real ones, and they are listed after the real orders in the UI.
Customer names, addresses and contacts are placeholders, not real customer data.
"""

from dataclasses import replace
from typing import List

from src.utils.models import DUMMY_ORDER_PREFIX, Order, OrderLine

SAMPLE_CONTACT = "Ship to Contact: Sample Contact  Phone Number: +1 555 010 0100"
CORD_US = ("IB-POWER-CORD-14G-US", "Power Cord, 14Gauge, United States")
CORD_EU = ("IB-POWER-CORD-14G-EU", "Power Cord, 14Gauge, Europe")
CORD_UK = ("IB-POWER-CORD-14G-UK", "Power Cord, 14Gauge, United Kingdom")
CORD_CN = ("IB-POWER-CORD-14G-CN", "Power Cord, 14Gauge, China")


def make_line(
    line_number: int, sku: str, description: str, qty: int, item_class: str
) -> OrderLine:
    """Build one order line with the warehouse defaults used by Arrow orders.

    Args:
        line_number: Line number within the order.
        sku: Item SKU.
        description: Item description as shown in the backlog report.
        qty: Ordered quantity.
        item_class: Item class, e.g. "Hardware Base" or "Power Cords".

    Returns:
        An OrderLine.
    """
    return OrderLine(
        line_number=line_number,
        sku=sku,
        item_description=description,
        ordered_qty=qty,
        line_status="AWAIT_SHIP",
        warehouse="ARROW",
        sub_inventory="ARRW-FGI-R",
        item_class=item_class,
    )


def appliance(line_number: int, sku: str, qty: int = 1) -> OrderLine:
    """Build a hardware appliance line (item class "Hardware Base Units").

    Args:
        line_number: Line number within the order.
        sku: Appliance SKU, e.g. "TE-1506-HW-AC".
        qty: Ordered quantity.

    Returns:
        An OrderLine.
    """
    model = sku.split("-HW")[0]
    return make_line(
        line_number, sku, f"{model} Hardware, AC", qty, "Hardware Base Units"
    )


def power_cord(line_number: int, cord: tuple, qty: int = 1) -> OrderLine:
    """Build a power-cord line.

    Args:
        line_number: Line number within the order.
        cord: One of the CORD_* (sku, description) tuples.
        qty: Ordered quantity.

    Returns:
        An OrderLine.
    """
    return make_line(line_number, cord[0], cord[1], qty, "Power Cords")


def power_supply(line_number: int, qty: int = 1) -> OrderLine:
    """Build a spare power supply line (item class "Power Supply Units").

    Args:
        line_number: Line number within the order.
        qty: Ordered quantity.

    Returns:
        An OrderLine.
    """
    return make_line(
        line_number,
        "T-PSU600-AC",
        "FRU, Infoblox AC Power Supply Unit, 600W",
        qty,
        "Power Supply Units",
    )


def make_order(
    order_no: str,
    order_type: str,
    country: str,
    country_code: str,
    lines: list,
    **overrides,
) -> Order:
    """Build an order with complete, valid header fields unless overridden.

    Args:
        order_no: Fusion order number.
        order_type: Order type as shown in the backlog report.
        country: Ship-to country name.
        country_code: Ship-to ISO country code.
        lines: The order's lines.
        **overrides: Any Order field to replace (e.g. shipping_method="").

    Returns:
        An Order.
    """
    order = Order(
        order_no=order_no,
        order_type=order_type,
        booked_date="2026-09-22",
        bill_to_customer=f"Sample Customer {order_no}",
        bill_to_address="1 Sample Street",
        ship_to_customer=f"Sample Customer {order_no}",
        ship_to_address="1 Sample Street",
        ship_to_country=country,
        ship_to_country_code=country_code,
        fob="Ex-works",
        freight_terms="Prepay & Add",
        shipping_method="FedEx Ground (3-10 days)",
        shipping_instructions=SAMPLE_CONTACT,
        customer_po=f"PO-{order_no}",
        ship_from="ARROW ELECTRONICS, INC.",
        scheduled_ship_date="2026-10-08",
        lines=lines,
    )
    return replace(order, **overrides)


def get_dummy_orders() -> List[Order]:
    """Return the five invented orders that the review should reject or hold.

    They are added to the real backlog orders (unless INCLUDE_DEMO_ORDERS=false), and are
    the only orders in FUSION_MODE=mock. Like unreleased Fusion orders, they have no
    customer PO and no USPO.

    Returns:
        The dummy orders, each with a note on the expected outcome.
    """
    plain = {"customer_po": ""}
    sfp = make_line(3, "IB-SFPPLUS-SR", "SFP+ 10GE SR Transceiver", 4, "Transceiver")
    return [
        # Reject, never releasable: embargoed destination.
        make_order(
            f"{DUMMY_ORDER_PREFIX}DIST-0001",
            "Distributor",
            "Iran",
            "IR",
            [appliance(1, "TE-1506-HW-AC"), power_cord(2, CORD_EU)],
            **plain,
        ),
        # Reject: EU cords on a UK order.
        make_order(
            f"{DUMMY_ORDER_PREFIX}REN-0002",
            "Renewal",
            "United Kingdom",
            "GB",
            [appliance(1, "TE-1606-HW-AC", 2), power_cord(2, CORD_EU, 4)],
            **plain,
        ),
        # Reject: two TE-1606 need 4 cords, only 2 ordered.
        make_order(
            f"{DUMMY_ORDER_PREFIX}REG-0003",
            "Regular",
            "United States",
            "US",
            [appliance(1, "TE-1606-HW-AC", 2), power_cord(2, CORD_US, 2)],
            **plain,
        ),
        # Reject ("WRONG SKU", like FED-1103664): SFP+ with a non-10GE appliance.
        make_order(
            f"{DUMMY_ORDER_PREFIX}FED-0004",
            "Federal",
            "United States",
            "US",
            [appliance(1, "TE-1506-HW-AC"), power_cord(2, CORD_US), sfp],
            **plain,
        ),
        # Hold: China needs an approved EUS.
        make_order(
            f"{DUMMY_ORDER_PREFIX}DIST-0005",
            "Distributor",
            "China",
            "CN",
            [appliance(1, "TE-2306-HW-AC", 2), power_cord(2, CORD_CN, 4)],
            **plain,
        ),
    ]
