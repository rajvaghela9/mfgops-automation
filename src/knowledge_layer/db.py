"""SQLite connection and schema for the whole service (one file: DB_PATH)."""

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from src.utils.config import load_config

SCHEMA = """
CREATE TABLE IF NOT EXISTS orders (
    order_no TEXT PRIMARY KEY,
    order_type TEXT, booked_date TEXT,
    bill_to_customer TEXT, bill_to_address TEXT,
    ship_to_customer TEXT, ship_to_address TEXT,
    ship_to_country TEXT, ship_to_country_code TEXT, end_customer_country TEXT,
    fob TEXT, freight_terms TEXT, shipping_method TEXT, shipping_instructions TEXT,
    customer_po TEXT, scheduled_ship_date TEXT,
    documents_received INTEGER NOT NULL DEFAULT 0,
    eus_case_id TEXT,
    route TEXT, route_reason TEXT, status TEXT,
    ai_recommendation TEXT, ai_explanation TEXT, human_decision TEXT,
    rework_count INTEGER NOT NULL DEFAULT 0,
    release_request_id TEXT, released_at TEXT,
    content_hash TEXT, raw_json TEXT,
    first_seen_at TEXT, last_updated_at TEXT
);

CREATE TABLE IF NOT EXISTS order_lines (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    order_no TEXT NOT NULL REFERENCES orders(order_no) ON DELETE CASCADE,
    line_number INTEGER, sku TEXT, item_description TEXT, ordered_qty INTEGER,
    line_status TEXT, warehouse TEXT, sub_inventory TEXT, item_class TEXT,
    selling_price REAL, serial_number TEXT, work_order TEXT
);

CREATE TABLE IF NOT EXISTS validation_results (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    order_no TEXT NOT NULL REFERENCES orders(order_no) ON DELETE CASCADE,
    rule_id INTEGER, rule_name TEXT, action TEXT, message TEXT, evaluated_at TEXT
);

CREATE TABLE IF NOT EXISTS order_decisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    order_no TEXT NOT NULL,
    decision TEXT NOT NULL, decided_by TEXT NOT NULL, comment TEXT,
    ai_recommendation_at_time TEXT, agreed INTEGER, created_at TEXT
);

CREATE TABLE IF NOT EXISTS order_history (
    order_no TEXT PRIMARY KEY,
    order_type TEXT, route TEXT,
    first_seen_at TEXT, released_at TEXT, uspo_number TEXT, closed_at TEXT,
    ai_recommendation TEXT, final_decision TEXT, agreed INTEGER,
    rework_count INTEGER
);

CREATE TABLE IF NOT EXISTS rules (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL, domain TEXT NOT NULL, action TEXT NOT NULL, message TEXT NOT NULL,
    scope_json TEXT NOT NULL, conditions_json TEXT NOT NULL,
    source TEXT, owner TEXT, version INTEGER NOT NULL DEFAULT 1,
    attachment_path TEXT, is_active INTEGER NOT NULL DEFAULT 1, created_at TEXT
);

CREATE TABLE IF NOT EXISTS reference_lists (
    list_name TEXT NOT NULL, item_key TEXT NOT NULL, item_value TEXT,
    source TEXT, updated_at TEXT,
    PRIMARY KEY (list_name, item_key)
);
"""


@contextmanager
def get_connection() -> Iterator[sqlite3.Connection]:
    """Open the SQLite database, commit on success, roll back on error, always close.

    Returns:
        A context manager yielding a connection whose rows behave like dicts.
    """
    db_path = Path(load_config().db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(db_path)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    try:
        yield connection
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def init_db() -> None:
    """Create every table that doesn't exist yet. Safe to call on every start.

    Returns:
        None.
    """
    with get_connection() as connection:
        connection.executescript(SCHEMA)
