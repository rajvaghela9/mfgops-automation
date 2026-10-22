"""Lookup lists the rules and router rely on: EUS countries, SI customers, cord maps.

Edit them by chatting with the Rules Assistant; adding an entry never needs a code change.
"""

from typing import Dict

from src.knowledge_layer.db import get_connection
from src.utils.time_helpers import utc_now_iso

EUS_COUNTRIES = "eus_countries"
SPECIAL_INSTRUCTION_COUNTRIES = "special_instruction_countries"
SPECIAL_INSTRUCTION_CUSTOMERS = "special_instruction_customers"
EMBARGOED_COUNTRIES = "embargoed_countries"
BANNED_CUSTOMERS = "banned_customers"
EXCLUDED_ORDER_TYPES = "excluded_order_types"
POWER_CORD_RATIO = "power_cord_ratio"
COUNTRY_CORD_REGION = "country_cord_region"
TEN_GE_APPLIANCES = "ten_ge_appliances"

# Names shown to MfgOps users.
LIST_LABELS = {
    EUS_COUNTRIES: "Countries that need an EUS",
    SPECIAL_INSTRUCTION_COUNTRIES: "Countries with special instructions",
    SPECIAL_INSTRUCTION_CUSTOMERS: "Customers with special instructions",
    EMBARGOED_COUNTRIES: "Embargoed countries",
    BANNED_CUSTOMERS: "Banned customers",
    EXCLUDED_ORDER_TYPES: "Order types not checked (returns, internal)",
    POWER_CORD_RATIO: "Power cords needed per unit",
    COUNTRY_CORD_REGION: "Power cord type per country",
    TEN_GE_APPLIANCES: "Appliances with 10GE ports (SFP+ allowed)",
}

CONFIRM = "seed — confirm with Jennifer"
EUS_WORKBOOK_SOURCE = "EUS Countries v3.0 — 01 August 2022"
REQUIRED_EUS_COUNTRIES = {
    "AF": ("Afghanistan", EUS_WORKBOOK_SOURCE),
    "AM": ("Armenia", EUS_WORKBOOK_SOURCE),
    "AZ": ("Azerbaijan", EUS_WORKBOOK_SOURCE),
    "CF": ("Central African Republic", EUS_WORKBOOK_SOURCE),
    "CD": ("Democratic Republic of the Congo", EUS_WORKBOOK_SOURCE),
    "CN": ("China", EUS_WORKBOOK_SOURCE),
    "ER": ("Eritrea", EUS_WORKBOOK_SOURCE),
    "GE": ("Georgia", EUS_WORKBOOK_SOURCE),
    "HK": ("Hong Kong (with import licence)", EUS_WORKBOOK_SOURCE),
    "IQ": ("Iraq", EUS_WORKBOOK_SOURCE),
    "KH": ("Cambodia", EUS_WORKBOOK_SOURCE),
    "KG": ("Kyrgyzstan", EUS_WORKBOOK_SOURCE),
    "KR": ("South Korea", "MfgOps requirement — 22 October 2026"),
    "KZ": ("Kazakhstan", EUS_WORKBOOK_SOURCE),
    "LA": ("Laos", EUS_WORKBOOK_SOURCE),
    "LB": ("Lebanon", EUS_WORKBOOK_SOURCE),
    "LY": ("Libya", EUS_WORKBOOK_SOURCE),
    "MD": ("Moldova", EUS_WORKBOOK_SOURCE),
    "MN": ("Mongolia", EUS_WORKBOOK_SOURCE),
    "MO": ("Macau", EUS_WORKBOOK_SOURCE),
    "PK": ("Pakistan", EUS_WORKBOOK_SOURCE),
    "SD": ("Sudan", EUS_WORKBOOK_SOURCE),
    "SO": ("Somalia", EUS_WORKBOOK_SOURCE),
    "SS": ("South Sudan", EUS_WORKBOOK_SOURCE),
    "TJ": ("Tajikistan", EUS_WORKBOOK_SOURCE),
    "TM": ("Turkmenistan", EUS_WORKBOOK_SOURCE),
    "UA": ("Ukraine", EUS_WORKBOOK_SOURCE),
    "UZ": ("Uzbekistan", EUS_WORKBOOK_SOURCE),
    "VN": ("Vietnam", EUS_WORKBOOK_SOURCE),
    "YE": ("Yemen", EUS_WORKBOOK_SOURCE),
    "ZW": ("Zimbabwe", EUS_WORKBOOK_SOURCE),
}
SEED_REFERENCE_LISTS = {
    EUS_COUNTRIES: REQUIRED_EUS_COUNTRIES,
    SPECIAL_INSTRUCTION_COUNTRIES: {
        "AE": ("UAE: blue-ink sign and stamp", "Jennifer"),
        "QA": ("Qatar: blue-ink sign and stamp", "Jennifer"),
        "SA": ("Saudi Arabia: commercial invoice", "Research plan"),
    },
    SPECIAL_INSTRUCTION_CUSTOMERS: {
        "EXCLUSIVE NETWORKS": ("Exclusive Networks ME", "Jennifer"),
    },
    EMBARGOED_COUNTRIES: {
        "CU": ("Cuba", "seed — confirm with Trade Compliance"),
        "IR": ("Iran", "seed — confirm with Trade Compliance"),
        "KP": ("North Korea", "seed — confirm with Trade Compliance"),
        "SY": ("Syria", "seed — confirm with Trade Compliance"),
    },
    # Key is text matched inside the bill-to or ship-to customer name.
    BANNED_CUSTOMERS: {
        "SAMPLE BANNED TRADING": (
            "Demo entry — replace with the real denied-party list",
            "seed — confirm with Trade Compliance",
        ),
    },
    EXCLUDED_ORDER_TYPES: {
        "RMA": ("Returns", "Dry run"),
        "FRU SHIP ONLY RMA": ("Returns", "Dry run"),
        "RENEWAL RETURN": ("Returns", "Dry run"),
        "RETURN TO STOCK FOR CREDIT": ("Returns", "Dry run"),
        "DEPOT REPLENISHMENT": ("Internal", "Dry run"),
    },
    # Key is a SKU prefix, value is power cords needed per unit.
    POWER_CORD_RATIO: {
        "TE-1606": ("2", "Jennifer"),
        "TE-1506": ("1", "Jennifer"),
        "T-PSU600-AC": ("1", "Jennifer"),
        "TE-2306": ("2", "inferred from Jennifer's examples — confirm"),
        "TE-906-HW-2AC": ("2", "inferred from Jennifer's examples — confirm"),
    },
    # Key is an ISO country code, value is the cord SKU suffix it needs.
    COUNTRY_CORD_REGION: {
        "US": ("US", CONFIRM),
        "CA": ("US", CONFIRM),
        "GB": ("UK", CONFIRM),
        "AE": ("UK", "Jennifer's Dubai example"),
        "QA": ("UK", CONFIRM),
        "SA": ("UK", CONFIRM),
        "DE": ("EU", CONFIRM),
        "FR": ("EU", CONFIRM),
        "NL": ("EU", CONFIRM),
        "BE": ("EU", CONFIRM),
        "IT": ("EU", CONFIRM),
        "ES": ("EU", CONFIRM),
        "CN": ("CN", "Jennifer's China EUS example"),
    },
    # Appliance SKU prefixes with 10GE ports whose SKU doesn't say "10GE". SKUs that
    # contain "10GE" (e.g. TE-1506-10GE-HW-AC) are recognized automatically.
    TEN_GE_APPLIANCES: {},
}


def get_reference_list(list_name: str) -> Dict[str, str]:
    """Read one reference list.

    Args:
        list_name: One of the list-name constants in this module.

    Returns:
        A dict of item key to item value (empty if the list has no entries).
    """
    with get_connection() as connection:
        rows = connection.execute(
            "SELECT item_key, item_value FROM reference_lists WHERE list_name = ?",
            (list_name,),
        ).fetchall()
    return {row["item_key"]: row["item_value"] for row in rows}


def get_all_reference_lists() -> Dict[str, Dict[str, str]]:
    """Read every reference list in one query, for a full validation run.

    Returns:
        A dict of list name to {item key: item value}. Missing lists are empty dicts.
    """
    all_lists: Dict[str, Dict[str, str]] = {name: {} for name in SEED_REFERENCE_LISTS}
    with get_connection() as connection:
        rows = connection.execute(
            "SELECT list_name, item_key, item_value FROM reference_lists"
        ).fetchall()
    for row in rows:
        all_lists.setdefault(row["list_name"], {})[row["item_key"]] = row["item_value"]
    return all_lists


def get_reference_rows() -> list:
    """Read every reference entry with its source, for display in the UI.

    Returns:
        A list of dicts with list_name, item_key, item_value, source, updated_at.
    """
    with get_connection() as connection:
        rows = connection.execute(
            "SELECT * FROM reference_lists ORDER BY list_name, item_key"
        ).fetchall()
    return [dict(row) for row in rows]


def upsert_reference_value(list_name: str, key: str, value: str, source: str) -> None:
    """Add or replace one entry in a reference list. Keys are stored upper-case.

    Args:
        list_name: The list to change.
        key: Entry key, e.g. a country code or SKU prefix.
        value: Entry value, e.g. a cord region or a description.
        source: Who or what this entry came from.

    Returns:
        None.
    """
    with get_connection() as connection:
        connection.execute(
            "INSERT OR REPLACE INTO reference_lists "
            "(list_name, item_key, item_value, source, updated_at) VALUES (?, ?, ?, ?, ?)",
            (
                list_name,
                key.strip().upper(),
                value.strip(),
                source.strip(),
                utc_now_iso(),
            ),
        )


def delete_reference_value(list_name: str, key: str) -> None:
    """Remove one entry from a reference list.

    Args:
        list_name: The list to change.
        key: Entry key to remove.

    Returns:
        None.
    """
    with get_connection() as connection:
        connection.execute(
            "DELETE FROM reference_lists WHERE list_name = ? AND item_key = ?",
            (list_name, key.strip().upper()),
        )


def seed_reference_lists() -> None:
    """Fill each list that has no entries yet with its seed values.

    Lists that already have entries are left alone, so user edits and deletions
    stick. Mandatory EUS countries are always backfilled because omitting one
    could allow an order to bypass the required document check.

    Returns:
        None.
    """
    now = utc_now_iso()
    with get_connection() as connection:
        for list_name, entries in SEED_REFERENCE_LISTS.items():
            if list_name == EUS_COUNTRIES:
                for key, (value, source) in entries.items():
                    connection.execute(
                        "INSERT OR IGNORE INTO reference_lists "
                        "(list_name, item_key, item_value, source, updated_at) "
                        "VALUES (?, ?, ?, ?, ?)",
                        (list_name, key, value, source, now),
                    )
                continue
            already_filled = connection.execute(
                "SELECT 1 FROM reference_lists WHERE list_name = ? LIMIT 1",
                (list_name,),
            ).fetchone()
            if already_filled:
                continue
            for key, (value, source) in entries.items():
                connection.execute(
                    "INSERT OR IGNORE INTO reference_lists "
                    "(list_name, item_key, item_value, source, updated_at) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (list_name, key, value, source, now),
                )
