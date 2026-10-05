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
    monkeypatch.setenv("ATTACHMENTS_DIR", str(tmp_path / "attachments"))
    monkeypatch.setenv("OUTBOX_DIR", str(tmp_path / "outbox"))
    monkeypatch.setenv("FUSION_MODE", "mock")
    monkeypatch.setenv("EMAIL_MODE", "mock")
    for name in [
        "LITELLM_BASE_URL",
        "LITELLM_API_KEY",
        "LITELLM_MODEL",
        "OM_TEAM_EMAIL",
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
