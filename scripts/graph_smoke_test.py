"""Read-only check of the mail connection: sign in, find the latest backlog mail, parse it.

Run from the repo root: `python scripts/graph_smoke_test.py`. It reads GRAPH_API_* and
BACKLOG_* from .env, lists recent mail in the monitored mailbox, downloads the newest
"Infoblox Orders Backlog Report" attachment into data/attachments/backlog/ (gitignored)
and prints a summary. It never sends, moves or deletes mail.
"""

import sys
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.integration_layer import graph_mail_client  # noqa: E402
from src.integration_layer.read_backlog_report import read_backlog_report  # noqa: E402
from src.utils.config import load_config  # noqa: E402


def main() -> None:
    """Run the check and print what was found.

    Returns:
        None.
    """
    config = load_config()
    print(f"Mailbox: {config.graph_api_mailbox}, folder: {config.graph_api_folder}")
    graph_mail_client.get_access_token()
    print("Sign-in: ok")
    since = (datetime.now(timezone.utc) - timedelta(hours=24)).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )
    messages = graph_mail_client.list_recent_messages(
        config.graph_api_mailbox, config.graph_api_folder, since
    )
    print(f"Messages in the last 24 h: {len(messages)}")
    reports = [
        message
        for message in messages
        if (message.get("from") or {})
        .get("emailAddress", {})
        .get("address", "")
        .lower()
        == config.backlog_sender
        and (message.get("subject") or "").startswith(config.backlog_subject)
    ]
    print(f"Backlog report mails: {len(reports)}")
    for message in reports[:5]:
        print(f"  {message['receivedDateTime']}  {message['subject']}")
    if not reports:
        return
    newest = reports[0]
    attachments = graph_mail_client.list_attachments(
        config.graph_api_mailbox, newest["id"]
    )
    for attachment in attachments:
        print(
            f"  attachment: {attachment['name']} ({attachment.get('contentType')}, "
            f"{attachment.get('size')} bytes)"
        )
    report = next((a for a in attachments if a["name"].lower().endswith(".xls")), None)
    if report is None:
        return
    folder = Path(config.attachments_dir) / "backlog"
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / "smoke_test_latest.xls"
    path.write_bytes(
        graph_mail_client.download_attachment(
            config.graph_api_mailbox, newest["id"], report["id"]
        )
    )
    orders = read_backlog_report(str(path))
    print(f"Parsed {len(orders)} orders, {sum(len(o.lines) for o in orders)} lines")
    print("Order types:", dict(Counter(order.order_type for order in orders)))
    print("With USPO:", sum(1 for order in orders if order.arrow_po))
    print("Without USPO:", sum(1 for order in orders if not order.arrow_po))


if __name__ == "__main__":
    main()
