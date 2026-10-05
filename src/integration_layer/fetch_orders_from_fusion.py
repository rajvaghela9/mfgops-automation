"""Fetches new orders from Fusion, routes them, and stores them for validation.

FUSION_MODE=mock (default) reads src/utils/mock_data.py. FUSION_MODE=live will call
Fusion Dev1 once the backlog report path and field mapping are confirmed by Fusion IT;
until then the live fetch raises NotImplementedError saying what is missing.
"""

import base64
from typing import Any, Dict, List, Optional

import requests

from src.intelligence_layer.order_router import decide_route
from src.knowledge_layer import order_store, reference_store
from src.utils.config import Config, load_config
from src.utils.logging_config import get_logger
from src.utils.mock_data import get_mock_orders
from src.utils.models import Order

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
    return get_mock_orders(order_store.get_sent_back_order_nos())


def fetch_new_orders() -> List[Order]:
    """Fetch the current backlog from Fusion (or the mock data).

    Returns:
        Every order currently in the backlog, before filtering or routing.
    """
    if load_config().fusion_mode == "live":
        return fetch_backlog_live()
    return fetch_backlog_mock()


def sync_orders() -> Dict[str, int]:
    """Fetch the backlog, skip out-of-scope order types, route, and store each order.

    Returns:
        Counts by outcome: inserted, updated_in_place, unchanged, already_closed, excluded.
    """
    lists = reference_store.get_all_reference_lists()
    excluded_types = lists.get(reference_store.EXCLUDED_ORDER_TYPES, {})
    counts = {"inserted": 0, "updated_in_place": 0, "unchanged": 0, "already_closed": 0}
    counts["excluded"] = 0
    for order in fetch_new_orders():
        if order.order_type.strip().upper() in excluded_types:
            counts["excluded"] += 1
            continue
        route, reason = decide_route(order, lists)
        order.route, order.route_reason = route.value, reason
        counts[order_store.upsert_order(order)] += 1
    logger.info("Fusion sync: %s", counts)
    return counts
