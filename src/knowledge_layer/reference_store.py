"""Lookup lists the rules and router rely on: EUS countries, SI customers, cord maps.

Edit them from the Knowledge Addition tab; adding an entry never needs a code change.
"""

from typing import Dict

from src.knowledge_layer.db import get_connection
from src.utils.time_helpers import utc_now_iso

EUS_COUNTRIES = "eus_countries"
SPECIAL_INSTRUCTION_COUNTRIES = "special_instruction_countries"
SPECIAL_INSTRUCTION_CUSTOMERS = "special_instruction_customers"
EMBARGOED_COUNTRIES = "embargoed_countries"
EXCLUDED_ORDER_TYPES = "excluded_order_types"
POWER_CORD_RATIO = "power_cord_ratio"
COUNTRY_CORD_REGION = "country_cord_region"
TEN_GE_APPLIANCES = "ten_ge_appliances"

CONFIRM = "seed — confirm with Jennifer"
SEED_REFERENCE_LISTS = {
    EUS_COUNTRIES: {
        "CN": ("China", "Infoblox EUS list"),
        "HK": ("Hong Kong (with import licence)", "Infoblox EUS list"),
        "IQ": ("Iraq", "Infoblox EUS list"),
        "SD": ("Sudan", "Arrow EUS list"),
    },
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
    },
    # Key is an appliance SKU prefix that has 10GE ports (SFP+ allowed).
    TEN_GE_APPLIANCES: {
        "TE-2306": ("10GE appliance", CONFIRM),
        "TE-4106": ("10GE appliance", CONFIRM),
    },
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

    Lists that already have entries are left alone, so user edits and deletions stick.

    Returns:
        None.
    """
    now = utc_now_iso()
    with get_connection() as connection:
        for list_name, entries in SEED_REFERENCE_LISTS.items():
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
