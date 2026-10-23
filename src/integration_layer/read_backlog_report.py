"""Reads orders from an "Infoblox Orders Backlog Report" Excel file (the Fusion BIP output).

The report has one row per order line. This turns it into one Order per order number, using
the column headings (so extra columns or a different column order don't matter). Where a
heading appears twice (the dates), the first column is the real date; the second is a
placeholder and is ignored.

Not in the report: the customer's own PO number and the end-customer country. The
"Purchase Order" column is the Arrow PO (USPO) created once an order is sent to Arrow.
"""

from collections import defaultdict
from pathlib import Path
from typing import Any, Dict, List

import xlrd

from src.utils.models import Order, OrderLine

ORDER_COLUMNS = {
    "order_type": "Order Type",
    "bill_to_customer": "Bill To Customer",
    "bill_to_address": "Bill To Address",
    "ship_to_customer": "Ship To Customer",
    "ship_to_address": "Ship To Address",
    "ship_to_country": "Ship To Country",
    "ship_to_country_code": "Ship To Country Code",
    "ship_from": "Shipping from",
    "fob": "FOB",
    "freight_terms": "Freight Terms",
    "shipping_method": "Shipping Method",
    "shipping_instructions": "Shipping Instructions",
    "arrow_po": "Purchase Order",
}
DATE_COLUMNS = {
    "booked_date": "Booked Date",
    "scheduled_ship_date": "Scheduled Ship Date",
}
LINE_TEXT_COLUMNS = {
    "sku": "SKU",
    "item_description": "Item Description",
    "line_status": "Line Status",
    "warehouse": "Warehouse",
    "sub_inventory": "Sub-Inventory",
    "item_class": "Item Class",
    "serial_number": "Serial Number",
    "work_order": "Work Order",
}
# Every heading the review needs. If Fusion changes the report layout, reading stops
# with a clear message instead of silently reviewing orders with empty fields.
REQUIRED_HEADINGS = sorted(
    {"Order Number", "Line Number", "Ordered Qty", "Selling Price"}
    | set(ORDER_COLUMNS.values())
    | set(DATE_COLUMNS.values())
    | set(LINE_TEXT_COLUMNS.values())
)


def read_text(row: Dict[str, Any], column: str) -> str:
    """Read a cell as clean text (whole numbers lose their ".0").

    Args:
        row: One report row, keyed by column heading.
        column: The column heading.

    Returns:
        The cell text, or "" if the cell is empty or the column is missing.
    """
    value = row.get(column, "")
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    return str(value).strip()


def read_number(row: Dict[str, Any], column: str) -> float:
    """Read a cell as a number.

    Args:
        row: One report row, keyed by column heading.
        column: The column heading.

    Returns:
        The number, or 0 if the cell is empty or not a number.
    """
    try:
        return float(row.get(column) or 0)
    except (TypeError, ValueError):
        return 0.0


def read_date(row: Dict[str, Any], column: str) -> str:
    """Read a date cell such as "2026-09-24T21:38:49.000+00:00".

    Args:
        row: One report row, keyed by column heading.
        column: The column heading.

    Returns:
        The date as "YYYY-MM-DD", or "" if the cell is empty.
    """
    return read_text(row, column)[:10]


def read_sheet_rows(path: Path) -> List[Dict[str, Any]]:
    """Read the first sheet of the Excel file as a list of rows keyed by column heading.

    Args:
        path: The .xls file.

    Returns:
        One dict per data row. For a repeated heading the first column wins.
    """
    sheet = xlrd.open_workbook(str(path)).sheet_by_index(0)
    headings = [str(cell.value).strip() for cell in sheet.row(0)]
    if "Order Number" not in headings:
        raise ValueError(
            f"{path.name} doesn't look like an Orders Backlog Report: no 'Order Number' column."
        )
    missing = [heading for heading in REQUIRED_HEADINGS if heading not in headings]
    if missing:
        raise ValueError(
            f"{path.name} is missing these backlog report columns: {', '.join(missing)}. "
            "Has the Fusion report layout changed?"
        )
    first_column = {}
    for index, heading in enumerate(headings):
        first_column.setdefault(heading, index)
    return [
        {
            heading: sheet.cell(row_number, index).value
            for heading, index in first_column.items()
        }
        for row_number in range(1, sheet.nrows)
    ]


def build_order_line(row: Dict[str, Any]) -> OrderLine:
    """Build one order line from a report row.

    Args:
        row: One report row, keyed by column heading.

    Returns:
        The OrderLine.
    """
    return OrderLine(
        line_number=int(read_number(row, "Line Number")),
        ordered_qty=int(read_number(row, "Ordered Qty")),
        selling_price=read_number(row, "Selling Price"),
        **{name: read_text(row, column) for name, column in LINE_TEXT_COLUMNS.items()},
    )


def read_backlog_report(path: str) -> List[Order]:
    """Read every order in a backlog report file.

    Args:
        path: Path to the .xls report.

    Returns:
        One Order per order number (with its lines), in the order they appear.
    """
    file = Path(path).expanduser()
    if not file.is_file():
        raise FileNotFoundError(
            f"Backlog report not found: {file}. Set BACKLOG_FILE_PATH in .env."
        )
    if file.suffix.lower() != ".xls":
        raise ValueError("Only .xls backlog reports are supported (the Fusion export).")
    rows_by_order: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for row in read_sheet_rows(file):
        order_no = read_text(row, "Order Number")
        if order_no:
            rows_by_order[order_no].append(row)
    orders = []
    for order_no, rows in rows_by_order.items():
        first = rows[0]
        orders.append(
            Order(
                order_no=order_no,
                lines=[build_order_line(row) for row in rows],
                **{name: read_text(first, col) for name, col in ORDER_COLUMNS.items()},
                **{name: read_date(first, col) for name, col in DATE_COLUMNS.items()},
            )
        )
    return orders
