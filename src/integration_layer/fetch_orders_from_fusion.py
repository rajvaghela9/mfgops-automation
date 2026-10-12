"""Fetches orders from Fusion and stores them; the review workflow takes it from there.

FUSION_MODE=file reads the backlog report Excel file at BACKLOG_FILE_PATH (a real export
from Fusion), plus 10 demo problem orders unless INCLUDE_DEMO_ORDERS=false.
FUSION_MODE=mock (default) reads src/utils/mock_data.py. FUSION_MODE=live will call Fusion
Dev1 once the backlog report path and field mapping are confirmed by Fusion IT; until then
the live fetch raises NotImplementedError saying what is missing.
"""

import base64
from typing import Any, Dict, List, Optional

import requests

from src.knowledge_layer import order_store, reference_store
from src.integration_layer.read_backlog_report import read_backlog_report
from src.utils.config import Config, load_config
from src.utils.logging_config import get_logger
from src.utils.mock_data import get_mock_orders, get_problem_demo_orders
from src.utils.models import Order, OrderStatus

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
    """Read the sample backlog, with OM's fixes applied to sent-back orders.

    Returns:
        The sample orders.
    """
    sent_to_om = order_store.get_active_orders([OrderStatus.SENT_TO_OM.value])
    return get_mock_orders(order.order_no for order in sent_to_om)


def fetch_new_orders() -> List[Order]:
    """Fetch the current backlog from Fusion (or the mock data).

    Returns:
        Every order currently in the backlog, before filtering or routing.
    """
    config = load_config()
    if config.fusion_mode == "live":
        return fetch_backlog_live()
    if config.fusion_mode == "file":
        if not config.backlog_file_path:
            raise RuntimeError("FUSION_MODE=file needs BACKLOG_FILE_PATH in .env.")
        orders = read_backlog_report(config.backlog_file_path)
        return (
            orders + get_problem_demo_orders() if config.include_demo_orders else orders
        )
    return fetch_backlog_mock()


def sync_orders() -> Dict[str, int]:
    """Fetch the backlog, skip out-of-scope order types, and store each order.

    New and corrected orders get status NEW; the review workflow picks them up.

    Returns:
        Counts by outcome: new, updated, unchanged, already_released, excluded.
    """
    excluded_types = reference_store.get_reference_list(
        reference_store.EXCLUDED_ORDER_TYPES
    )
    counts = {
        "new": 0,
        "updated": 0,
        "unchanged": 0,
        "already_released": 0,
        "excluded": 0,
    }
    for order in fetch_new_orders():
        if order.order_type.strip().upper() in excluded_types:
            counts["excluded"] += 1
            continue
        counts[order_store.upsert_order(order)] += 1
    logger.info("Fusion sync: %s", counts)
    return counts
