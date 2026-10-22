"""Clear all order data so the next check starts fresh from the Fusion backlog mails.

Run from the repo root: `python scripts/reset_order_data.py` (or `make reset-order-data`).
It first copies DB_PATH and WORKFLOW_DB_PATH to data/backup/<time>/, then deletes active
orders, all-orders history, reviewer decisions, learning requests and the mail log, and
removes the saved workflows (this also clears Rules Assistant chat history). Business
rules and reference lists are kept, including anything learned through the chat.
"""

import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.knowledge_layer.db import get_connection, init_db  # noqa: E402
from src.utils.config import load_config  # noqa: E402

ORDER_TABLES = [
    "rule_findings",
    "order_lines",
    "orders",
    "all_orders",
    "order_decisions",
    "learning_requests",
    "mail_messages",
]


def back_up(paths) -> Path:
    """Copy the database files into a new timestamped backup folder.

    Args:
        paths: Database files to copy (missing ones are skipped).

    Returns:
        The backup folder.
    """
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    folder = Path("data") / "backup" / stamp
    folder.mkdir(parents=True, exist_ok=True)
    for path in paths:
        if path.exists():
            shutil.copy2(path, folder / path.name)
    return folder


def main() -> None:
    """Back up, then clear the order data.

    Returns:
        None.
    """
    config = load_config()
    db_path, workflow_path = Path(config.db_path), Path(config.workflow_db_path)
    folder = back_up([db_path, workflow_path])
    print(f"Backup written to {folder}")
    init_db()
    with get_connection() as connection:
        for table in ORDER_TABLES:
            deleted = connection.execute(f"DELETE FROM {table}").rowcount
            print(f"  {table}: {deleted} row(s) deleted")
        connection.execute(
            "DELETE FROM app_settings WHERE setting_name = "
            "'last_backlog_report_order_count'"
        )
    if workflow_path.exists():
        workflow_path.unlink()
        print(f"  {workflow_path}: removed (saved workflows and chats)")
    print("Done. Rules and reference lists were kept.")


if __name__ == "__main__":
    main()
