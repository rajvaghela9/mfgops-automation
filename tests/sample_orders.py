"""Sample backlog used by the tests (the orders the UI showed before real mail data).

They cover every category and outcome. They live with the tests, not in the service:
the running service only shows real orders from the Fusion backlog mails plus the five
dummy orders in src/utils/mock_data.py.
"""

from dataclasses import replace
from typing import Iterable, List

from src.utils.mock_data import (
    CORD_CN,
    CORD_EU,
    CORD_UK,
    CORD_US,
    SAMPLE_CONTACT,
    appliance,
    make_line,
    make_order,
    power_cord,
    power_supply,
)
from src.utils.models import Order


def get_sample_orders(sent_back_order_nos: Iterable[str] = ()) -> List[Order]:
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
        # EUS country (like Jennifer's DIST-1102211): held until an approved EUS is linked.
        make_order(
            "DIST-MOCK-0004",
            "Distributor",
            "China",
            "CN",
            [appliance(1, "TE-2306-HW-AC", 2), power_cord(2, CORD_CN, 4)],
        ),
        # Embargoed destination.
        make_order(
            "DIST-MOCK-0005",
            "Distributor",
            "Iran",
            "IR",
            [appliance(1, "TE-1506-HW-AC"), power_cord(2, CORD_EU)],
        ),
        # Jennifer's clean US domestic example: 1606 x2 (4 cords), 1506 x2 (2 cords),
        # 4 spare PSUs (4 cords) = 10 cords.
        make_order(
            "FED-1103714",
            "Federal",
            "United States",
            "US",
            [
                appliance(1, "TE-1606-HW-AC", 2),
                power_cord(2, CORD_US, 4),
                power_supply(3, 2),
                power_cord(4, CORD_US, 2),
                appliance(5, "TE-1506-HW-AC", 2),
                power_cord(6, CORD_US, 4),
                power_supply(7, 2),
            ],
        ),
        # Like Jennifer's Expernet South Korea order: a letter-of-credit order, so the AI
        # should hold it for the reviewer.
        make_order(
            "DIST-MOCK-0007",
            "Distributor",
            "Korea, Republic of",
            "KR",
            [
                appliance(1, "TE-1506-10GE-HW-AC", 2),
                power_cord(2, CORD_EU, 2),
                power_supply(3, 2),
                power_cord(4, CORD_EU, 2),
                appliance(5, "TE-1606-10GE-HW-AC", 6),
                power_cord(6, CORD_EU, 12),
            ],
            bill_to_customer="EXPERNET",
            ship_to_customer="EXPERNET",
            shipping_instructions=(
                "Ship to Contact: Sample Contact  Phone Number: 82-2-555-0100.  Shipping "
                "Comments: Mixed New Order. Payment by L/C (letter of credit). Contact the "
                "forwarder H & FRIENDS GTL by email before shipping."
            ),
        ),
        # Like Jennifer's Dubai example: special instructions (blue-ink stamp, DHL account).
        make_order(
            "DIST-MOCK-0010",
            "Distributor",
            "United Arab Emirates",
            "AE",
            [appliance(1, "TE-906-HW-2AC"), power_cord(2, CORD_UK, 2)],
            bill_to_customer="EXCLUSIVE NETWORKS ME",
            ship_to_customer="EXCLUSIVE NETWORKS ME",
            shipping_method="DHL Various Various",
            freight_terms="Collect",
            shipping_instructions=(
                "Ship to Contact: Sample Contact  Phone Number: +971 4 555 0100.  Shipping "
                "Comments: Please ship through DHL Account# 000000000 EXPRESS WORLDWIDE. "
                "PARTIAL SHIPMENT NOT ALLOWED. Shipping/Commercial invoices must state the "
                "ECCN, HS Code and Country of Origin."
            ),
        ),
        # EUS + Special Instructions: an EUS country and a special-instructions customer.
        make_order(
            "DIST-MOCK-0008",
            "Distributor",
            "Iraq",
            "IQ",
            [appliance(1, "TE-1506-HW-AC"), power_cord(2, CORD_UK)],
            bill_to_customer="EXCLUSIVE NETWORKS ME",
            ship_to_customer="Sample Customer Baghdad",
        ),
        # Banned customer: can never be released, even if a reviewer approves.
        make_order(
            "DIST-MOCK-0009",
            "Distributor",
            "United States",
            "US",
            [appliance(1, "TE-1506-HW-AC"), power_cord(2, CORD_US)],
            bill_to_customer="SAMPLE BANNED TRADING LLC",
        ),
        # Return order: out of scope, filtered out before review.
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
