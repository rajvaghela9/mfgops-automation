"""SQLite connection and schema for the whole service (one file: DB_PATH)."""

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from src.utils.config import load_config

SCHEMA = """
-- Orders still being worked on. Released orders leave this table.
CREATE TABLE IF NOT EXISTS orders (
    order_no TEXT PRIMARY KEY,
    order_type TEXT, booked_date TEXT,
    bill_to_customer TEXT, bill_to_address TEXT,
    ship_to_customer TEXT, ship_to_address TEXT,
    ship_to_country TEXT, ship_to_country_code TEXT, end_customer_country TEXT,
    ship_from TEXT, fob TEXT, freight_terms TEXT, shipping_method TEXT,
    shipping_instructions TEXT, customer_po TEXT, arrow_po TEXT, scheduled_ship_date TEXT,
    documents_received INTEGER NOT NULL DEFAULT 0,
    eus_case_id TEXT,
    route TEXT, route_reason TEXT, status TEXT,
    ai_verdict TEXT, ai_used INTEGER, ai_summary TEXT, ai_reasons_json TEXT,
    reviewer_decision TEXT, reviewer_comment TEXT, decided_by TEXT,
    review_thread_id TEXT,
    rework_count INTEGER NOT NULL DEFAULT 0,
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

-- Hard rules that found a problem on an active order.
CREATE TABLE IF NOT EXISTS rule_findings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    order_no TEXT NOT NULL REFERENCES orders(order_no) ON DELETE CASCADE,
    rule_id INTEGER, rule_name TEXT, outcome TEXT, message TEXT,
    can_be_overridden INTEGER NOT NULL DEFAULT 1, checked_at TEXT
);

-- Every reviewer decision, kept forever.
CREATE TABLE IF NOT EXISTS order_decisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    order_no TEXT NOT NULL,
    decision TEXT NOT NULL, decided_by TEXT NOT NULL, comment TEXT,
    ai_verdict_at_time TEXT, agreed INTEGER, created_at TEXT
);

-- Latest reviewer outcome for each downloaded mailbox attachment.
CREATE TABLE IF NOT EXISTS attachment_decisions (
    attachment_path TEXT PRIMARY KEY,
    decision TEXT NOT NULL CHECK (decision IN ('approve', 'reject')),
    decided_by TEXT NOT NULL,
    comment TEXT,
    decided_at TEXT NOT NULL
);

-- The authoritative content and metadata for downloaded mailbox attachments.
CREATE TABLE IF NOT EXISTS mailbox_attachments (
    message_id TEXT NOT NULL,
    attachment_id TEXT NOT NULL,
    mailbox TEXT NOT NULL,
    sender_email TEXT NOT NULL,
    folder_name TEXT NOT NULL,
    relative_path TEXT NOT NULL UNIQUE,
    file_name TEXT NOT NULL,
    content_type TEXT,
    file_size INTEGER NOT NULL,
    content BLOB NOT NULL,
    received_at TEXT NOT NULL,
    stored_at TEXT NOT NULL,
    PRIMARY KEY (message_id, attachment_id)
);

-- Messages already checked for attachments, so mailbox polling is idempotent.
CREATE TABLE IF NOT EXISTS processed_mail_messages (
    message_id TEXT PRIMARY KEY,
    mailbox TEXT NOT NULL,
    sender_email TEXT NOT NULL,
    received_at TEXT NOT NULL,
    processed_at TEXT NOT NULL
);

-- One outbound package per special-instruction email folder.
CREATE TABLE IF NOT EXISTS shared_mail_packages (
    folder_name TEXT PRIMARY KEY,
    recipient TEXT NOT NULL,
    shared_by TEXT NOT NULL,
    shared_at TEXT NOT NULL
);

-- One row per order ever seen, kept forever. Feeds the Dashboard.
CREATE TABLE IF NOT EXISTS all_orders (
    order_no TEXT PRIMARY KEY,
    order_type TEXT, route TEXT, ship_to_country TEXT, status TEXT,
    ai_verdict TEXT, reviewer_decision TEXT, agreed INTEGER,
    decided_by TEXT, reviewer_comment TEXT, rework_count INTEGER,
    release_request_id TEXT, released_at TEXT,
    first_seen_at TEXT, last_updated_at TEXT
);

-- Business rules. One wide table for hard (fixed) and soft (AI-judged) rules;
-- a rule leaves the columns it doesn't need empty.
CREATE TABLE IF NOT EXISTS rules (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    rule_type TEXT NOT NULL,              -- hard | soft
    outcome TEXT NOT NULL,                -- reject | hold | warn
    description TEXT NOT NULL,            -- plain English: what the rule checks
    category TEXT,                        -- e.g. power_cords, embargo, documents
    applies_to_routes_json TEXT,          -- empty list = every order category
    scope_json TEXT,                      -- which orders, e.g. destination countries
    conditions_json TEXT,                 -- hard rules only
    guidance TEXT,                        -- soft rules only: how the AI should judge
    message TEXT,                         -- shown when a hard rule fires
    can_be_overridden INTEGER NOT NULL DEFAULT 1,
    severity TEXT,
    original_wording TEXT,                -- the rule as the user first said it
    source TEXT, owner TEXT,
    confidence REAL, confirmed INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'active',  -- active | retired
    generated_by_model TEXT, created_by TEXT,
    approved_by TEXT, approved_at TEXT,
    example_orders TEXT,                  -- for reference only, never enforced
    attachment_path TEXT,
    version INTEGER NOT NULL DEFAULT 1,
    created_at TEXT, updated_at TEXT
);

CREATE TABLE IF NOT EXISTS reference_lists (
    list_name TEXT NOT NULL, item_key TEXT NOT NULL, item_value TEXT,
    source TEXT, updated_at TEXT,
    PRIMARY KEY (list_name, item_key)
);

-- Decisions where the reviewer and the AI disagreed, so the AI can learn from them.
CREATE TABLE IF NOT EXISTS learning_requests (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    order_no TEXT NOT NULL,
    ai_verdict TEXT, reviewer_decision TEXT, reviewer_comment TEXT, decided_by TEXT,
    order_details TEXT,                   -- snapshot, since released orders leave `orders`
    status TEXT NOT NULL DEFAULT 'open',  -- open | done
    chat_thread_id TEXT, created_at TEXT
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
    connection = sqlite3.connect(db_path, timeout=30)
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


# Columns added after the first release: (table, column, type). Existing databases get them
# added on start, because CREATE TABLE IF NOT EXISTS leaves old tables unchanged.
ADDED_COLUMNS = [("orders", "ai_used", "INTEGER")]


def add_missing_columns(connection: sqlite3.Connection) -> None:
    """Add columns that an older database file doesn't have yet.

    Args:
        connection: Open SQLite connection.

    Returns:
        None.
    """
    for table, column, column_type in ADDED_COLUMNS:
        existing = [
            row["name"] for row in connection.execute(f"PRAGMA table_info({table})")
        ]
        if column not in existing:
            connection.execute(f"ALTER TABLE {table} ADD COLUMN {column} {column_type}")
            if column == "ai_used":
                # Orders judged while the AI was unreachable carry this summary.
                connection.execute(
                    "UPDATE orders SET ai_used = 0 "
                    "WHERE ai_summary LIKE 'AI review not available%'"
                )


def init_db() -> None:
    """Create every table that doesn't exist yet and upgrade older ones. Safe on every start.

    Returns:
        None.
    """
    with get_connection() as connection:
        connection.executescript(SCHEMA)
        add_missing_columns(connection)
