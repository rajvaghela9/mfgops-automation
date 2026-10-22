"""Lookup lists the rules and router rely on: EUS countries, SI customers, cord maps.

Edit them by chatting with the Rules Assistant; adding an entry never needs a code change.
"""

from typing import Dict

from src.knowledge_layer.db import get_app_setting, get_connection, set_app_setting
from src.utils.logging_config import get_logger
from src.utils.time_helpers import utc_now_iso

logger = get_logger(__name__)

EUS_COUNTRIES = "eus_countries"
SPECIAL_INSTRUCTION_COUNTRIES = "special_instruction_countries"
SPECIAL_INSTRUCTION_CUSTOMERS = "special_instruction_customers"
EMBARGOED_COUNTRIES = "embargoed_countries"
BANNED_CUSTOMERS = "banned_customers"
EXCLUDED_ORDER_TYPES = "excluded_order_types"
POWER_CORD_RATIO = "power_cord_ratio"
COUNTRY_CORD_REGION = "country_cord_region"
TEN_GE_APPLIANCES = "ten_ge_appliances"
EMBARGOED_REGIONS = "embargoed_regions"
PSU_COMPATIBLE_APPLIANCES = "psu_compatible_appliances"

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
    EMBARGOED_REGIONS: "Embargoed regions (matched in the addresses)",
    PSU_COMPATIBLE_APPLIANCES: "Appliances that take a T-PSU600-AC power supply",
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
RULES_THREAD = "Raj/Jennifer rules thread, 30 Sep 2026"
CORD_TABLE = RULES_THREAD + " (power cord table)"
CONTINENTAL_EU = [
    "AT", "BE", "BG", "HR", "CZ", "DK", "EE", "FI", "FR", "DE", "GR", "HU",
    "LV", "LT", "LU", "NL", "PL", "PT", "RO", "SK", "SI", "ES", "SE",
]  # fmt: skip
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
        "RU": ("Russia", RULES_THREAD),
        "BY": ("Belarus", RULES_THREAD),
    },
    # Key is a word matched in the ship-to / bill-to address.
    EMBARGOED_REGIONS: {
        "CRIMEA": ("Crimea", RULES_THREAD),
        "SEVASTOPOL": ("Crimea (Sevastopol)", RULES_THREAD),
        "DONETSK": ("Donetsk", RULES_THREAD),
        "LUHANSK": ("Luhansk", RULES_THREAD),
        "LUGANSK": ("Luhansk", RULES_THREAD),
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
        "TE-906-HW-AC": ("1", CORD_TABLE),
        "TE-4106": ("2", CORD_TABLE),
    },
    # Key is an ISO country code, value is the cord SKU suffix it needs (the ultimate
    # destination's plug, even when the order ships through a forwarder elsewhere).
    COUNTRY_CORD_REGION: {
        "US": ("US", CONFIRM),
        "CA": ("US", CONFIRM),
        "MX": ("US", CORD_TABLE + ": North America"),
        "GB": ("UK", CONFIRM),
        "AE": ("UK", "Jennifer's Dubai example"),
        "QA": ("UK", CONFIRM),
        "SA": ("UK", CONFIRM),
        "HK": ("UK", CORD_TABLE),
        "SG": ("UK", CORD_TABLE),
        **{code: ("EU", CORD_TABLE + ": continental EU") for code in CONTINENTAL_EU},
        "IT": ("CL", CORD_TABLE),
        "CL": ("CL", CORD_TABLE),
        "CN": ("CN", "Jennifer's China EUS example"),
        "JP": ("JP", CORD_TABLE),
        "AU": ("AUS", CORD_TABLE),
        "NZ": ("AUS", CORD_TABLE),
        "IN": ("INDIA", CORD_TABLE),
        "TW": ("TW", CORD_TABLE),
        "BR": ("BR", CORD_TABLE),
    },
    # Key is an appliance SKU prefix.
    PSU_COMPATIBLE_APPLIANCES: {
        "TE-1506": ("TE-1506 (second redundant PSU or spare)", RULES_THREAD),
        "TE-1606": ("TE-1606", RULES_THREAD),
        "TE-2205": ("TE-2205", RULES_THREAD),
        "TE-2306": ("TE-2306", RULES_THREAD),
    },
    # Appliance SKU prefixes with 10GE ports whose SKU doesn't say "10GE". SKUs that
    # contain "10GE" (e.g. TE-1506-10GE-HW-AC) are recognized automatically.
    TEN_GE_APPLIANCES: {},
}

# Entries added to lists that existing databases already have (applied once per version;
# entries a user deleted earlier are not brought back by later versions).
REFERENCE_SEED_VERSION = 2
SEED_VERSION_SETTING = "reference_lists_seed_version"
ADDED_IN_SEED_VERSION = {
    2: {
        EMBARGOED_COUNTRIES: ["RU", "BY"],
        POWER_CORD_RATIO: ["TE-906-HW-AC", "TE-4106"],
        COUNTRY_CORD_REGION: [
            "MX", "HK", "SG", "CL", "JP", "AU", "NZ", "IN", "TW", "BR",
            *CONTINENTAL_EU,
        ],
    },
}  # fmt: skip
# Seed values corrected in a later version: (list, key) -> old value. The entry is only
# changed if it still has the old seed value (a user's own edit is never overwritten).
CORRECTED_IN_SEED_VERSION = {2: {(COUNTRY_CORD_REGION, "IT"): "EU"}}


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


def upgrade_reference_lists() -> None:
    """Bring an existing database's lists up to the current seed version.

    Returns:
        None.
    """
    stored = int(get_app_setting(SEED_VERSION_SETTING) or 1)
    if stored >= REFERENCE_SEED_VERSION:
        return
    now = utc_now_iso()
    with get_connection() as connection:
        for version in range(stored + 1, REFERENCE_SEED_VERSION + 1):
            for list_name, keys in ADDED_IN_SEED_VERSION.get(version, {}).items():
                for key in keys:
                    value, source = SEED_REFERENCE_LISTS[list_name][key]
                    connection.execute(
                        "INSERT OR IGNORE INTO reference_lists "
                        "(list_name, item_key, item_value, source, updated_at) "
                        "VALUES (?, ?, ?, ?, ?)",
                        (list_name, key, value, source, now),
                    )
            corrections = CORRECTED_IN_SEED_VERSION.get(version, {})
            for (list_name, key), old_value in corrections.items():
                value, source = SEED_REFERENCE_LISTS[list_name][key]
                connection.execute(
                    "UPDATE reference_lists SET item_value = ?, source = ?, updated_at = ? "
                    "WHERE list_name = ? AND item_key = ? AND item_value = ?",
                    (value, source, now, list_name, key, old_value),
                )
    set_app_setting(SEED_VERSION_SETTING, str(REFERENCE_SEED_VERSION))
    logger.info("Reference lists upgraded to seed version %d", REFERENCE_SEED_VERSION)
