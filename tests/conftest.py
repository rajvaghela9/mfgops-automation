"""Shared test setup: every test gets its own temp database and never calls real services."""

import pytest

from src.app import prepare_database


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
    monkeypatch.setenv("ATTACHMENT_OUTPUT_DIR", str(tmp_path / "eus_attachments"))
    monkeypatch.setenv("OUTBOX_DIR", str(tmp_path / "outbox"))
    monkeypatch.setenv("FUSION_MODE", "mock")
    monkeypatch.setenv("EMAIL_MODE", "mock")
    monkeypatch.setenv("INCLUDE_DEMO_ORDERS", "true")
    for name in [
        "LITELLM_BASE_URL",
        "LITELLM_API_KEY",
        "LITELLM_MODEL",
        "BACKLOG_FILE_PATH",
        "OM_TEAM_EMAIL",
        # Tests must never reach the real Microsoft Graph mailbox.
        "GRAPH_TENANT_ID",
        "GRAPH_CLIENT_ID",
        "GRAPH_CLIENT_SECRET",
        "GRAPH_MAILBOX",
        "GRAPH_SENDER_EMAILS",
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
