"""Sample orders used while FUSION_MODE=mock, shaped like the Infoblox Orders Backlog Report.

The three DIST/FEDR/REN-11xxxxx orders are the review candidates from the dry run on
Udit's backlog extract. Orders with MOCK in the number are invented for the demo.
Customer names, addresses and contacts are placeholders, not real customer data.
"""

from dataclasses import replace
from typing import Iterable, List

from src.utils.models import Order, OrderLine

SAMPLE_CONTACT = "Ship to Contact: Sample Contact  Phone Number: +1 555 010 0100"
CORD_US = ("IB-POWER-CORD-14G-US", "Power Cord, 14Gauge, United States")
CORD_EU = ("IB-POWER-CORD-14G-EU", "Power Cord, 14Gauge, Europe")
CORD_UK = ("IB-POWER-CORD-14G-UK", "Power Cord, 14Gauge, United Kingdom")


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
    """Build a hardware appliance line (item class "Hardware Base").

    Args:
        line_number: Line number within the order.
        sku: Appliance SKU, e.g. "TE-1506-HW-AC".
        qty: Ordered quantity.

    Returns:
        An OrderLine.
    """
    model = sku.split("-HW")[0]
    return make_line(line_number, sku, f"{model} Hardware, AC", qty, "Hardware Base")


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
        scheduled_ship_date="2026-10-08",
        lines=lines,
    )
    return replace(order, **overrides)


def get_mock_orders(sent_back_order_nos: Iterable[str] = ()) -> List[Order]:
    """Return the sample backlog, simulating OM fixes for orders that were sent back.

    Args:
        sent_back_order_nos: Orders currently sent back to OM. Any that have a
            corrected version below are returned corrected, as if OM fixed them.

    Returns:
        The list of sample orders.
    """
    sent_back = set(sent_back_order_nos)
    orders = [
        # Clean standard order: should pass every rule.
        make_order(
            "REN-1103701",
            "Renewal",
            "United States",
            "US",
            [appliance(1, "TE-1506-HW-AC"), power_cord(2, CORD_US)],
        ),
        # Dry run: SFP+ (10GE) transceivers with no 10GE appliance.
        make_order(
            "FEDR-1103736",
            "Federal",
            "United States",
            "US",
            [
                appliance(1, "TE-906-HW-2AC"),
                appliance(2, "TE-1506-HW-AC"),
                make_line(
                    3, "IB-SFPPLUS-SR", "SFP+ 10GE SR Transceiver", 7, "Transceiver"
                ),
                power_cord(4, CORD_US, 3),
            ],
        ),
        # Dry run: two Thailand orders for the same appliance, one US and one EU cord.
        make_order(
            "DIST-1102990",
            "Distributor",
            "Thailand",
            "TH",
            [appliance(1, "TE-1506-HW-AC"), power_cord(2, CORD_US)],
        ),
        make_order(
            "DIST-1103784",
            "Distributor",
            "Thailand",
            "TH",
            [appliance(1, "TE-1506-HW-AC"), power_cord(2, CORD_EU)],
        ),
        # Dry run: UAE ship-to with EU cords (Dubai example uses UK cords).
        make_order(
            "REN-1103679",
            "Renewal",
            "United Arab Emirates",
            "AE",
            [appliance(1, "TE-1506-HW-AC"), power_cord(2, CORD_EU)],
        ),
        # Cord count: TE-1606 needs 2 cords each, so 2 units need 4.
        make_order(
            "REN-MOCK-0002",
            "Renewal",
            "United Kingdom",
            "GB",
            [appliance(1, "TE-1606-HW-AC", 2), power_cord(2, CORD_UK, 2)],
        ),
        # Missing mandatory fields. OM "fixes" it after a send-back (see below).
        make_order(
            "DIST-MOCK-0003",
            "Distributor",
            "Germany",
            "DE",
            [appliance(1, "TE-1506-HW-AC"), power_cord(2, CORD_EU)],
            shipping_method="",
            customer_po="",
            shipping_instructions="Deliver to dock 4",
        ),
        # EUS country: routed to the EUS path and held until an approved EUS is linked.
        make_order(
            "DIST-MOCK-0004",
            "Distributor",
            "China",
            "CN",
            [appliance(1, "TE-1506-HW-AC"), power_cord(2, CORD_US)],
        ),
        # Embargoed destination.
        make_order(
            "DIST-MOCK-0005",
            "Distributor",
            "Iran",
            "IR",
            [appliance(1, "TE-1506-HW-AC"), power_cord(2, CORD_EU)],
        ),
        # Return order: out of scope, filtered out before routing.
        make_order(
            "RMA-MOCK-0006",
            "RMA",
            "United States",
            "US",
            [appliance(1, "TE-1506-HW-AC")],
        ),
    ]
    corrections = {
        "DIST-MOCK-0003": {
            "shipping_method": "DHL Express",
            "customer_po": "PO-DIST-MOCK-0003",
            "shipping_instructions": SAMPLE_CONTACT,
        }
    }
    return [
        (
            replace(order, **corrections[order.order_no])
            if order.order_no in sent_back and order.order_no in corrections
            else order
        )
        for order in orders
    ]
