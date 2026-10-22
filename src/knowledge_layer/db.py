"""SQLite connection and schema for the whole service (one file: DB_PATH)."""

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator, Optional

from src.utils.config import load_config
from src.utils.time_helpers import utc_now_iso

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
    approved_at TEXT,
    missing_snapshot_count INTEGER NOT NULL DEFAULT 0,
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

-- One row per order ever seen, kept forever. Feeds the Dashboard.
CREATE TABLE IF NOT EXISTS all_orders (
    order_no TEXT PRIMARY KEY,
    order_type TEXT, route TEXT, ship_to_country TEXT, status TEXT,
    ai_verdict TEXT, reviewer_decision TEXT, agreed INTEGER,
    decided_by TEXT, reviewer_comment TEXT, rework_count INTEGER,
    release_request_id TEXT, released_at TEXT,
    uspo TEXT,                            -- Fusion's USPO, read from a backlog report mail
    released_by TEXT,                     -- who released it ("Fusion (outside this tool)")
    first_seen_at TEXT, last_updated_at TEXT
);

-- Every mail the service read or sent (metadata only, never the body).
CREATE TABLE IF NOT EXISTS mail_messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    graph_message_id TEXT UNIQUE,
    internet_message_id TEXT, conversation_id TEXT,
    direction TEXT NOT NULL,              -- in | out
    kind TEXT NOT NULL,                   -- backlog_report | om_correction | arrow_notice
    order_no TEXT,
    sender TEXT, recipient TEXT, subject TEXT,
    received_at TEXT,
    status TEXT NOT NULL,                 -- processed | superseded | failed | sent | mock
    attachment_path TEXT, attachment_sha256 TEXT, order_count INTEGER,
    idempotency_key TEXT,
    error TEXT, processed_at TEXT
);
CREATE INDEX IF NOT EXISTS mail_messages_key ON mail_messages (idempotency_key);

-- Small key/value settings the service keeps for itself (e.g. seed versions).
CREATE TABLE IF NOT EXISTS app_settings (
    setting_name TEXT PRIMARY KEY, setting_value TEXT, updated_at TEXT
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
ADDED_COLUMNS = [
    ("orders", "ai_used", "INTEGER"),
    ("orders", "approved_at", "TEXT"),
    ("orders", "missing_snapshot_count", "INTEGER NOT NULL DEFAULT 0"),
    ("all_orders", "uspo", "TEXT"),
    ("all_orders", "released_by", "TEXT"),
]


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


def get_app_setting(name: str) -> Optional[str]:
    """Read one of the service's own settings.

    Args:
        name: Setting name.

    Returns:
        The stored value, or None if it was never set.
    """
    with get_connection() as connection:
        row = connection.execute(
            "SELECT setting_value FROM app_settings WHERE setting_name = ?", (name,)
        ).fetchone()
    return row["setting_value"] if row else None


def set_app_setting(name: str, value: str) -> None:
    """Store one of the service's own settings.

    Args:
        name: Setting name.
        value: Value to store.

    Returns:
        None.
    """
    with get_connection() as connection:
        connection.execute(
            "INSERT OR REPLACE INTO app_settings (setting_name, setting_value, updated_at) "
            "VALUES (?, ?, ?)",
            (name, value, utc_now_iso()),
        )
