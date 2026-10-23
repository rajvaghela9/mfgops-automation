"""Fetches the Fusion backlog and stores it; the review workflow takes it from there.

FUSION_MODE=mail reads the newest "Infoblox Orders Backlog Report" that Fusion mails to
GRAPH_API_MAILBOX (about every 15 minutes) through MS Graph.
FUSION_MODE=file reads a saved backlog report .xls at BACKLOG_FILE_PATH.
FUSION_MODE=mock (default) has no real orders, only the dummy orders.
In mail and file mode the five dummy orders are added unless INCLUDE_DEMO_ORDERS=false.
FUSION_MODE=live will call Fusion once IT provides API access (raises NotImplementedError).

Only orders without a USPO are reviewed: a USPO means Fusion already released the order
to Arrow. When an approved order's USPO shows up, its release is confirmed.
"""

import base64
from typing import Any, Dict, List, Optional

import requests

from src.integration_layer.fetch_backlog_from_mail import fetch_backlog_snapshot
from src.integration_layer.read_backlog_report import read_backlog_report
from src.intelligence_layer.order_review_workflow import confirm_release_from_report
from src.knowledge_layer import order_store, reference_store
from src.knowledge_layer.db import get_app_setting, set_app_setting
from src.utils.config import Config, load_config
from src.utils.logging_config import get_logger
from src.utils.mock_data import get_dummy_orders
from src.utils.models import Order

# Released outside this tool: the USPO appeared for an order nobody approved here.
RELEASED_OUTSIDE_TOOL = "Fusion (outside this tool)"
LAST_REPORT_ORDER_COUNT = "last_backlog_report_order_count"

logger = get_logger(__name__)

SALES_ORDERS_PATH = "/fscmRestApi/resources/11.13.18.05/salesOrdersForOrderHub"


def get_auth_headers(config: Config) -> Dict[str, str]:
    """Build the Authorization header for Fusion, using OAuth or Basic Auth.

    OAuth client credentials are used when client id, secret and token URL are all set;
    otherwise Basic Auth with the integration user's username and password.

    Args:
        config: Settings holding the Fusion credentials.

    Returns:
        A headers dict containing Authorization.
    """
    if (
        config.fusion_client_id
        and config.fusion_client_secret
        and config.fusion_token_url
    ):
        response = requests.post(
            config.fusion_token_url,
            data={"grant_type": "client_credentials"},
            auth=(config.fusion_client_id, config.fusion_client_secret),
            timeout=30,
        )
        response.raise_for_status()
        return {"Authorization": f"Bearer {response.json()['access_token']}"}
    if config.fusion_username and config.fusion_password:
        token = base64.b64encode(
            f"{config.fusion_username}:{config.fusion_password}".encode()
        ).decode()
        return {"Authorization": f"Basic {token}"}
    raise RuntimeError(
        "No Fusion credentials: set FUSION_USERNAME and FUSION_PASSWORD, or "
        "FUSION_CLIENT_ID, FUSION_CLIENT_SECRET and FUSION_TOKEN_URL, in .env"
    )


def call_fusion_api(
    method: str,
    path: str,
    params: Optional[Dict[str, Any]] = None,
    json_body: Any = None,
) -> Dict[str, Any]:
    """Make one authenticated REST call to Fusion.

    Args:
        method: HTTP method, e.g. "GET" or "POST".
        path: Path after the base URL, e.g. SALES_ORDERS_PATH.
        params: Optional query parameters.
        json_body: Optional JSON request body.

    Returns:
        The parsed JSON response.
    """
    config = load_config()
    response = requests.request(
        method,
        f"{config.fusion_base_url}{path}",
        headers={**get_auth_headers(config), "Content-Type": "application/json"},
        params=params,
        json=json_body,
        timeout=60,
    )
    response.raise_for_status()
    return response.json()


def fetch_order_details_live(order_no: str) -> Dict[str, Any]:
    """Read one order (with lines) from the Sales Orders for Order Hub REST API.

    Args:
        order_no: Fusion order number.

    Returns:
        Fusion's raw JSON for the order.
    """
    response = call_fusion_api(
        "GET",
        SALES_ORDERS_PATH,
        params={
            "q": f"OrderNumber='{order_no}'",
            "expand": "lines",
            "onlyData": "true",
        },
    )
    items = response.get("items", [])
    if not items:
        raise LookupError(f"Order {order_no} not found in Fusion")
    return items[0]


def fetch_backlog_live() -> List[Order]:
    """Read orders awaiting release from Fusion Dev1.

    Returns:
        The orders, mapped to our Order model.
    """
    raise NotImplementedError(
        "Live Fusion fetch is pending: need the BIP path and parameters of the "
        "'Infoblox Orders Backlog Report', the status that marks an order ready to "
        "release, and the Fusion-to-Order field mapping. Use FUSION_MODE=mock until then."
    )


def fetch_backlog_mock() -> List[Order]:
    """Return the backlog used in test mode: only the dummy orders.

    Returns:
        The dummy orders.
    """
    return get_dummy_orders()


def fetch_new_orders() -> Optional[List[Order]]:
    """Fetch the current backlog from the configured source.

    Returns:
        Every order in the backlog (before filtering), or None in mail mode when no new
        backlog report mail arrived since the last check.
    """
    config = load_config()
    if config.fusion_mode == "live":
        return fetch_backlog_live()
    if config.fusion_mode == "mock":
        return fetch_backlog_mock()
    if config.fusion_mode == "mail":
        snapshot = fetch_backlog_snapshot()
        if snapshot is None:
            return None
        orders = snapshot.orders
    elif config.fusion_mode == "file":
        if not config.backlog_file_path:
            raise RuntimeError("FUSION_MODE=file needs BACKLOG_FILE_PATH in .env.")
        orders = read_backlog_report(config.backlog_file_path)
    else:
        raise RuntimeError(
            f"Unknown FUSION_MODE={config.fusion_mode!r}; use mail, file, mock or live."
        )
    return orders + get_dummy_orders() if config.include_demo_orders else orders


def handle_released_order(order: Order) -> str:
    """Deal with an order whose USPO is set, i.e. that Fusion released to Arrow.

    Args:
        order: The order from the backlog report.

    Returns:
        The outcome to count: "release_confirmed" (it was approved here),
        "released_outside_tool" (it was active but not approved here),
        "already_released" (released before), or "already_sent_to_arrow" (never seen;
        not stored, as there is nothing to review).
    """
    active = order_store.get_order(order.order_no)
    if active is None:
        if order_store.backfill_uspo(order.order_no, order.arrow_po):
            return "already_released"
        return "already_sent_to_arrow"
    if confirm_release_from_report(order.order_no, order.arrow_po):
        return "release_confirmed"
    order_store.mark_released(
        order.order_no, uspo=order.arrow_po, released_by=RELEASED_OUTSIDE_TOOL
    )
    return "released_outside_tool"


def note_vanished_orders(orders: List[Order], counts: Dict[str, int]) -> None:
    """Close orders that are no longer in the backlog report (mail mode only).

    Skipped when the new report has fewer than half the orders of the previous one,
    which looks like a broken report rather than real cancellations.

    Args:
        orders: Every order in the new report.
        counts: Sync counts, updated in place with "removed_from_backlog".

    Returns:
        None.
    """
    previous = int(get_app_setting(LAST_REPORT_ORDER_COUNT) or 0)
    set_app_setting(LAST_REPORT_ORDER_COUNT, str(len(orders)))
    if previous and len(orders) < previous / 2:
        logger.warning(
            "Backlog report has %d orders (previous %d); not closing missing orders",
            len(orders),
            previous,
        )
        return
    outcome = order_store.note_orders_missing_from_backlog(
        [order.order_no for order in orders]
    )
    counts["removed_from_backlog"] = outcome["removed"]


def sync_orders() -> Dict[str, int]:
    """Fetch the backlog, skip out-of-scope order types, and store each order.

    Orders without a USPO are new work: new and corrected ones get status NEW and the
    review workflow picks them up. Orders with a USPO were released by Fusion (see
    handle_released_order).

    Returns:
        Counts by outcome: new, updated, unchanged, already_released, excluded,
        already_sent_to_arrow, release_confirmed, released_outside_tool,
        removed_from_backlog, and new_report (0 when no new report mail arrived).
    """
    counts = {
        "new_report": 1,
        "new": 0,
        "updated": 0,
        "unchanged": 0,
        "already_released": 0,
        "excluded": 0,
        "already_sent_to_arrow": 0,
        "release_confirmed": 0,
        "released_outside_tool": 0,
        "removed_from_backlog": 0,
    }
    orders = fetch_new_orders()
    if orders is None:
        counts["new_report"] = 0
        logger.info("No new backlog report mail")
        return counts
    excluded_types = reference_store.get_reference_list(
        reference_store.EXCLUDED_ORDER_TYPES
    )
    for order in orders:
        if order.order_type.strip().upper() in excluded_types:
            counts["excluded"] += 1
        elif order.arrow_po:
            counts[handle_released_order(order)] += 1
        else:
            counts[order_store.upsert_order(order)] += 1
    if load_config().fusion_mode == "mail":
        note_vanished_orders(orders, counts)
    logger.info("Fusion sync: %s", counts)
    return counts
