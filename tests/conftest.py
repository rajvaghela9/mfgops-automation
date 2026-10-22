"""Shared test setup: every test gets its own temp database and never calls real services."""

from dataclasses import replace

import pytest

from src.app import prepare_database
from src.integration_layer import fetch_orders_from_fusion
from src.knowledge_layer import order_store
from src.utils.models import OrderStatus
from tests.sample_orders import get_sample_orders


@pytest.fixture(autouse=True)
def isolated_environment(tmp_path, monkeypatch):
    """Point all storage at a temp folder and switch every external call to mock/off.

    Blank values override anything in the developer's .env (load_dotenv never
    replaces variables that are already set).

    Args:
        tmp_path: pytest's per-test temp directory.
        monkeypatch: pytest's environment patcher.

    Returns:
        The temp directory.
    """
    monkeypatch.setenv("DB_PATH", str(tmp_path / "test.db"))
    monkeypatch.setenv("WORKFLOW_DB_PATH", str(tmp_path / "workflow.db"))
    monkeypatch.setenv("ATTACHMENTS_DIR", str(tmp_path / "attachments"))
    monkeypatch.setenv("OUTBOX_DIR", str(tmp_path / "outbox"))
    monkeypatch.setenv("FUSION_MODE", "mock")
    monkeypatch.setenv("EMAIL_MODE", "mock")
    monkeypatch.setenv("INCLUDE_DEMO_ORDERS", "true")
    monkeypatch.setenv("OM_TEAM_EMAIL", "om-team@example.com")
    monkeypatch.setenv("ARROW_RECIPIENT_EMAIL", "arrow@example.com")
    for name in [
        "LITELLM_BASE_URL",
        "LITELLM_API_KEY",
        "LITELLM_MODEL",
        "BACKLOG_FILE_PATH",
        "GRAPH_API_TENANT_ID",
        "GRAPH_API_CLIENT_ID",
        "GRAPH_API_CLIENT_SECRET",
        "GRAPH_API_MAILBOX",
        "GRAPH_API_FOLDER",
        "GRAPH_API_SENDER_MAILBOX",
        "BACKLOG_SENDER",
        "BACKLOG_SUBJECT",
        "BACKLOG_ATTACHMENT_NAME",
        "BACKLOG_ATTACHMENT_KEEP",
        "EMAIL_REDIRECT_TO",
        "USPO_WAIT_WARNING_MINUTES",
    ]:
        monkeypatch.setenv(name, "")
    return tmp_path


@pytest.fixture
def seeded_db():
    """Create the schema and load seed rules and reference lists.

    Returns:
        None.
    """
    prepare_database()


@pytest.fixture
def sample_backlog(monkeypatch):
    """Use the sample orders (tests/sample_orders.py) as the mock backlog.

    Orders sent back to OM come back corrected, as if OM fixed them in Fusion. Put an
    order number and USPO in the returned dict to make the next backlog show that order
    as released by Fusion.

    Args:
        monkeypatch: pytest's patcher.

    Returns:
        The dict of order number to USPO.
    """
    uspos = {}

    def fetch_sample_backlog():
        """Return the sample orders, with OM's fixes and Fusion's USPOs applied."""
        sent_to_om = order_store.get_active_orders([OrderStatus.SENT_TO_OM.value])
        orders = get_sample_orders(order.order_no for order in sent_to_om)
        return [
            (
                replace(order, arrow_po=uspos[order.order_no])
                if order.order_no in uspos
                else order
            )
            for order in orders
        ]

    monkeypatch.setattr(
        fetch_orders_from_fusion, "fetch_backlog_mock", fetch_sample_backlog
    )
    return uspos
