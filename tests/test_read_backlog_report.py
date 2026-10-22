from types import SimpleNamespace
from unittest.mock import patch

import pytest

from src.integration_layer import fetch_orders_from_fusion
from src.integration_layer.read_backlog_report import read_backlog_report

LINE = {
    "Order Number": "DIST-1",
    "Order Type": "Distributor",
    "Booked Date": "2026-09-24T21:38:49.000+00:00",
    "Line Number": 1.0,
    "Shipping from": "ARROW ELECTRONICS, INC.",
    "Line Status": "AWAIT_SHIP",
    "SKU": "TE-1506-HW-AC",
    "Item Description": "TE-1506 Hardware, AC",
    "Ordered Qty": 2.0,
    "Serial Number": "",
    "Warehouse": "Hardware Base",
    "Sub-Inventory": "ARRW-FGI-R",
    "Selling Price": 100.5,
    "Item Class": "Hardware Base Units",
    "Bill To Customer": "ACME",
    "Ship To Customer": "ACME THAILAND",
    "Ship To Address": "1 Road Bangkok",
    "Ship To Country": "Thailand",
    "Ship To Country Code": "TH",
    "FOB": "Ex-works",
    "Shipping Method": "DHL",
    "Shipping Instructions": "Phone Number: +66 2 555 0100",
    "Scheduled Ship Date": "2026-10-08T06:59:59.000+00:00",
    "Freight Terms": "Collect",
    "Purchase Order": "USPO45334",
    "Work Order": "",
}


def read_with_rows(tmp_path, rows):
    report = tmp_path / "report.xls"
    report.write_bytes(b"")
    with patch(
        "src.integration_layer.read_backlog_report.read_sheet_rows", return_value=rows
    ):
        return read_backlog_report(str(report))


def test_lines_are_grouped_into_one_order(tmp_path):
    second_line = {
        **LINE,
        "Line Number": 2.0,
        "SKU": "IB-POWER-CORD-14G-US",
        "Ordered Qty": 2.0,
    }
    orders = read_with_rows(
        tmp_path, [LINE, second_line, {**LINE, "Order Number": "DIST-2"}]
    )
    assert [order.order_no for order in orders] == ["DIST-1", "DIST-2"]
    order = orders[0]
    assert [(line.line_number, line.sku, line.ordered_qty) for line in order.lines] == [
        (1, "TE-1506-HW-AC", 2),
        (2, "IB-POWER-CORD-14G-US", 2),
    ]
    assert (order.ship_to_country_code, order.ship_from, order.fob) == (
        "TH",
        "ARROW ELECTRONICS, INC.",
        "Ex-works",
    )
    assert (order.booked_date, order.scheduled_ship_date) == (
        "2026-09-24",
        "2026-10-08",
    )
    assert order.lines[0].selling_price == 100.5
    assert order.lines[0].item_class == "Hardware Base Units"


def test_arrow_po_is_not_taken_for_the_customer_po(tmp_path):
    order = read_with_rows(tmp_path, [LINE])[0]
    assert (order.arrow_po, order.customer_po) == ("USPO45334", "")


def test_blank_rows_are_skipped(tmp_path):
    assert read_with_rows(tmp_path, [{**LINE, "Order Number": ""}]) == []


def test_missing_file_and_wrong_type_are_explained(tmp_path):
    with pytest.raises(FileNotFoundError, match="BACKLOG_FILE_PATH"):
        read_backlog_report(str(tmp_path / "nope.xls"))
    csv_file = tmp_path / "report.csv"
    csv_file.write_text("x")
    with pytest.raises(ValueError, match=".xls"):
        read_backlog_report(str(csv_file))


def test_file_mode_adds_the_demo_orders(tmp_path, monkeypatch):
    monkeypatch.setenv("FUSION_MODE", "file")
    monkeypatch.setenv("BACKLOG_FILE_PATH", str(tmp_path / "report.xls"))
    report_orders = read_with_rows(tmp_path, [LINE])
    with patch.object(
        fetch_orders_from_fusion, "read_backlog_report", return_value=report_orders
    ):
        orders = fetch_orders_from_fusion.fetch_new_orders()
        assert len(orders) == 1 + 5 and orders[0].order_no == "DIST-1"
        monkeypatch.setenv("INCLUDE_DEMO_ORDERS", "false")
        assert len(fetch_orders_from_fusion.fetch_new_orders()) == 1


def test_file_mode_needs_a_path(monkeypatch):
    monkeypatch.setenv("FUSION_MODE", "file")
    monkeypatch.setenv("BACKLOG_FILE_PATH", "")
    with pytest.raises(RuntimeError, match="BACKLOG_FILE_PATH"):
        fetch_orders_from_fusion.fetch_new_orders()


def fake_workbook(headings):
    cells = [SimpleNamespace(value=heading) for heading in headings]
    sheet = SimpleNamespace(row=lambda number: cells, nrows=1)
    return SimpleNamespace(sheet_by_index=lambda index: sheet)


def test_a_changed_report_layout_is_explained(tmp_path):
    from src.integration_layer import read_backlog_report as reader

    report = tmp_path / "report.xls"
    report.write_bytes(b"")
    headings = [heading for heading in reader.REQUIRED_HEADINGS if heading != "FOB"]
    with patch.object(
        reader.xlrd, "open_workbook", return_value=fake_workbook(headings)
    ):
        with pytest.raises(
            ValueError, match="missing these backlog report columns: FOB"
        ):
            read_backlog_report(str(report))
    with patch.object(
        reader.xlrd,
        "open_workbook",
        return_value=fake_workbook(reader.REQUIRED_HEADINGS + ["Booked Date"]),
    ):
        assert read_backlog_report(str(report)) == []  # every column there, no rows
