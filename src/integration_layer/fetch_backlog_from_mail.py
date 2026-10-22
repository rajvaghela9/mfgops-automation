"""Reads the newest "Infoblox Orders Backlog Report" mail that Fusion sends to the mailbox.

Fusion mails the full open backlog about every 15 minutes, so each report replaces the
previous one: only the newest unprocessed mail is read and older unprocessed ones are
marked "superseded". "No new mail" (None) is kept apart from "a report with no orders",
so a quiet poll never looks like every order disappeared.
"""

import hashlib
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, List, Optional

from src.integration_layer import graph_mail_client
from src.integration_layer.read_backlog_report import read_backlog_report
from src.knowledge_layer import mail_store
from src.utils.config import Config, load_config
from src.utils.logging_config import get_logger
from src.utils.models import Order

logger = get_logger(__name__)

# Look back a little before the newest recorded report, in case mail arrives late.
OVERLAP_HOURS = 2
FIRST_RUN_LOOKBACK_HOURS = 24


@dataclass
class BacklogSnapshot:
    """The orders in one backlog report mail."""

    orders: List[Order]
    received_at: str
    message_id: str
    attachment_path: str


class BacklogMailError(Exception):
    """A backlog report mail could not be read (the message says why)."""


def format_graph_time(moment: datetime) -> str:
    """Format a time the way Graph filters expect.

    Args:
        moment: A timezone-aware time.

    Returns:
        e.g. "2026-10-19T11:33:17Z".
    """
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def find_search_start() -> str:
    """Work out how far back to look for report mails.

    Returns:
        A Graph-formatted UTC time.
    """
    latest = mail_store.get_latest_report_received_at()
    if latest:
        start = datetime.fromisoformat(latest.replace("Z", "+00:00"))
        return format_graph_time(start - timedelta(hours=OVERLAP_HOURS))
    return format_graph_time(
        datetime.now(timezone.utc) - timedelta(hours=FIRST_RUN_LOOKBACK_HOURS)
    )


def read_sender(message: Dict) -> str:
    """Read a Graph message's sender address.

    Args:
        message: Graph message summary.

    Returns:
        The address in lower case, or "".
    """
    return (
        (message.get("from") or {}).get("emailAddress", {}).get("address", "").lower()
    )


def is_backlog_report(message: Dict, config: Config) -> bool:
    """Tell whether a mail is a Fusion backlog report.

    Args:
        message: Graph message summary.
        config: Settings with BACKLOG_SENDER and BACKLOG_SUBJECT.

    Returns:
        True when both sender and subject match.
    """
    subject = (message.get("subject") or "").strip().lower()
    return read_sender(message) == config.backlog_sender and subject.startswith(
        config.backlog_subject.lower()
    )


def pick_report_attachment(attachments: List[Dict], config: Config) -> Optional[Dict]:
    """Choose the report file among a mail's attachments.

    Args:
        attachments: Graph attachment summaries.
        config: Settings with BACKLOG_ATTACHMENT_NAME.

    Returns:
        The attachment named BACKLOG_ATTACHMENT_NAME, else the only .xls one, else None.
    """
    files = [item for item in attachments if not item.get("isInline")]
    wanted = config.backlog_attachment_name.lower()
    for item in files:
        if (item.get("name") or "").lower() == wanted:
            return item
    excel_files = [
        item for item in files if (item.get("name") or "").lower().endswith(".xls")
    ]
    return excel_files[0] if len(excel_files) == 1 else None


def save_report_file(content: bytes, received_at: str, message_id: str) -> Path:
    """Save a downloaded report under ATTACHMENTS_DIR/backlog and drop old copies.

    Args:
        content: The file bytes.
        received_at: When the mail arrived (used in the file name).
        message_id: Graph message id (shortened into the file name).

    Returns:
        Path of the saved file.
    """
    config = load_config()
    folder = Path(config.attachments_dir) / "backlog"
    folder.mkdir(parents=True, exist_ok=True)
    stamp = received_at.replace(":", "").replace("-", "")[:15]
    short_id = hashlib.sha256(message_id.encode()).hexdigest()[:8]
    path = folder / f"{stamp}_{short_id}.xls"
    path.write_bytes(content)
    saved = sorted(folder.glob("*_*.xls"))
    for old in saved[: max(0, len(saved) - config.backlog_attachment_keep)]:
        old.unlink(missing_ok=True)
    return path


def fetch_backlog_snapshot() -> Optional[BacklogSnapshot]:
    """Read the newest backlog report mail that hasn't been read yet.

    Returns:
        The report's orders, or None when there is no new report mail. Raises
        BacklogMailError when the newest mail can't be read (it is recorded as failed and
        not retried); Graph errors (GraphError) are not recorded, so they are retried.
    """
    config = load_config()
    graph_mail_client.check_graph_settings(config)
    mailbox = config.graph_api_mailbox
    messages = graph_mail_client.list_recent_messages(
        mailbox, config.graph_api_folder, find_search_start()
    )
    new_reports = sorted(
        (
            message
            for message in messages
            if is_backlog_report(message, config)
            and not mail_store.is_message_known(message["id"])
        ),
        key=lambda message: message["receivedDateTime"],
        reverse=True,
    )
    if not new_reports:
        return None
    newest = new_reports[0]
    for older in new_reports[1:]:
        mail_store.record_inbound_report(
            older["id"],
            older["receivedDateTime"],
            read_sender(older),
            older.get("subject", ""),
            mail_store.SUPERSEDED,
            internet_message_id=older.get("internetMessageId", ""),
        )

    def record_failure(error: str) -> None:
        """Record the newest mail as failed. Returns: None."""
        mail_store.record_inbound_report(
            newest["id"],
            newest["receivedDateTime"],
            read_sender(newest),
            newest.get("subject", ""),
            mail_store.FAILED,
            internet_message_id=newest.get("internetMessageId", ""),
            error=error,
        )

    attachment = pick_report_attachment(
        graph_mail_client.list_attachments(mailbox, newest["id"]), config
    )
    if attachment is None:
        error = f"No '{config.backlog_attachment_name}' attachment in the report mail."
        record_failure(error)
        raise BacklogMailError(error)
    content = graph_mail_client.download_attachment(
        mailbox, newest["id"], attachment["id"]
    )
    path = save_report_file(content, newest["receivedDateTime"], newest["id"])
    try:
        orders = read_backlog_report(str(path))
    except (ValueError, OSError) as error:
        record_failure(str(error))
        raise BacklogMailError(f"The backlog report couldn't be read: {error}")
    mail_store.record_inbound_report(
        newest["id"],
        newest["receivedDateTime"],
        read_sender(newest),
        newest.get("subject", ""),
        mail_store.PROCESSED,
        internet_message_id=newest.get("internetMessageId", ""),
        attachment_path=str(path),
        attachment_sha256=hashlib.sha256(content).hexdigest(),
        order_count=len(orders),
    )
    logger.info(
        "Backlog report of %s read: %d orders (%d older report mails skipped)",
        newest["receivedDateTime"],
        len(orders),
        len(new_reports) - 1,
    )
    return BacklogSnapshot(orders, newest["receivedDateTime"], newest["id"], str(path))
