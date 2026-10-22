"""Reads every runtime setting from environment variables and the local .env file."""

import os
from dataclasses import dataclass
from typing import Optional

from dotenv import load_dotenv


@dataclass
class Config:
    """All settings the service needs. Build it with load_config(), never by hand."""

    fusion_mode: str
    backlog_file_path: Optional[str]
    include_demo_orders: bool
    email_mode: str
    fusion_base_url: str
    fusion_username: Optional[str]
    fusion_password: Optional[str]
    fusion_client_id: Optional[str]
    fusion_client_secret: Optional[str]
    fusion_token_url: Optional[str]
    db_path: str
    workflow_db_path: str
    attachments_dir: str
    outbox_dir: str
    poll_interval_seconds: int
    healthz_port: int
    om_team_email: Optional[str]
    minutes_saved_per_order: float
    litellm_base_url: Optional[str]
    litellm_api_key: Optional[str]
    litellm_model: Optional[str]
    graph_api_tenant_id: Optional[str]
    graph_api_client_id: Optional[str]
    graph_api_client_secret: Optional[str]
    graph_api_mailbox: Optional[str]
    graph_api_folder: str
    graph_api_sender_mailbox: Optional[str]
    backlog_sender: str
    backlog_subject: str
    backlog_attachment_name: str
    backlog_attachment_keep: int
    email_redirect_to: Optional[str]
    arrow_recipient_email: Optional[str]
    uspo_wait_warning_minutes: int

    @property
    def llm_enabled(self) -> bool:
        """Tell whether every LiteLLM setting needed for a call is present.

        Returns:
            True when base URL, API key and model are all set.
        """
        return bool(
            self.litellm_base_url and self.litellm_api_key and self.litellm_model
        )

    @property
    def graph_enabled(self) -> bool:
        """Tell whether every MS Graph setting needed to read or send mail is present.

        Returns:
            True when tenant id, client id, client secret and mailbox are all set.
        """
        return bool(
            self.graph_api_tenant_id
            and self.graph_api_client_id
            and self.graph_api_client_secret
            and self.graph_api_mailbox
        )

    @property
    def sender_mailbox(self) -> Optional[str]:
        """The mailbox outgoing mail is sent from.

        Returns:
            GRAPH_API_SENDER_MAILBOX, or the monitored mailbox when it isn't set.
        """
        return self.graph_api_sender_mailbox or self.graph_api_mailbox


def read_setting(name: str, default: Optional[str] = None) -> Optional[str]:
    """Read one environment variable, treating blank values as missing.

    Args:
        name: Environment variable name.
        default: Value to use when the variable is missing or blank.

    Returns:
        The stripped value, or the default.
    """
    value = os.environ.get(name, "").strip()
    return value or default


def load_config() -> Config:
    """Load settings from .env (if present) and the environment.

    Variables already set in the environment win over values in .env.

    Returns:
        A Config with defaults filled in (mock Fusion mode, LLM off).
    """
    load_dotenv()
    return Config(
        fusion_mode=read_setting("FUSION_MODE", "mock").lower(),
        backlog_file_path=read_setting("BACKLOG_FILE_PATH"),
        include_demo_orders=read_setting("INCLUDE_DEMO_ORDERS", "true").lower()
        == "true",
        email_mode=read_setting("EMAIL_MODE", "mock").lower(),
        fusion_base_url=read_setting(
            "FUSION_BASE_URL", "https://efpv-dev1.fa.us6.oraclecloud.com"
        ).rstrip("/"),
        fusion_username=read_setting("FUSION_USERNAME"),
        fusion_password=read_setting("FUSION_PASSWORD"),
        fusion_client_id=read_setting("FUSION_CLIENT_ID"),
        fusion_client_secret=read_setting("FUSION_CLIENT_SECRET"),
        fusion_token_url=read_setting("FUSION_TOKEN_URL"),
        db_path=read_setting("DB_PATH", "data/mfgops.db"),
        workflow_db_path=read_setting(
            "WORKFLOW_DB_PATH", "data/workflow_checkpoints.db"
        ),
        attachments_dir=read_setting("ATTACHMENTS_DIR", "data/attachments"),
        outbox_dir=read_setting("OUTBOX_DIR", "data/outbox"),
        poll_interval_seconds=int(read_setting("POLL_INTERVAL_SECONDS", "300")),
        healthz_port=int(read_setting("HEALTHZ_PORT", "8080")),
        om_team_email=read_setting("OM_TEAM_EMAIL"),
        minutes_saved_per_order=float(read_setting("MINUTES_SAVED_PER_ORDER", "10")),
        litellm_base_url=read_setting("LITELLM_BASE_URL"),
        litellm_api_key=read_setting("LITELLM_API_KEY"),
        litellm_model=read_setting("LITELLM_MODEL"),
        graph_api_tenant_id=read_setting("GRAPH_API_TENANT_ID"),
        graph_api_client_id=read_setting("GRAPH_API_CLIENT_ID"),
        graph_api_client_secret=read_setting("GRAPH_API_CLIENT_SECRET"),
        graph_api_mailbox=read_setting("GRAPH_API_MAILBOX"),
        graph_api_folder=read_setting("GRAPH_API_FOLDER", "inbox"),
        graph_api_sender_mailbox=read_setting("GRAPH_API_SENDER_MAILBOX"),
        backlog_sender=read_setting(
            "BACKLOG_SENDER", "efpv.bi.sender@workflow.mail.us6.oraclecloud.com"
        ).lower(),
        backlog_subject=read_setting(
            "BACKLOG_SUBJECT", "Infoblox Orders Backlog Report"
        ),
        backlog_attachment_name=read_setting(
            "BACKLOG_ATTACHMENT_NAME", "Infoblox Orders Backlog Report.xls"
        ),
        backlog_attachment_keep=int(read_setting("BACKLOG_ATTACHMENT_KEEP", "20")),
        email_redirect_to=read_setting("EMAIL_REDIRECT_TO"),
        arrow_recipient_email=read_setting("ARROW_RECIPIENT_EMAIL"),
        uspo_wait_warning_minutes=int(read_setting("USPO_WAIT_WARNING_MINUTES", "60")),
    )
