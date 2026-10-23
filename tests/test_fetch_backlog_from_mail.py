from unittest.mock import patch

import pytest

from src.integration_layer import fetch_backlog_from_mail
from src.integration_layer.fetch_backlog_from_mail import (
    BacklogMailError,
    fetch_backlog_snapshot,
)
from src.knowledge_layer import mail_store
from src.knowledge_layer.db import get_connection
from tests.sample_orders import get_sample_orders

FUSION = "efpv.bi.sender@workflow.mail.us6.oraclecloud.com"
REPORT = {"id": "a1", "name": "Infoblox Orders Backlog Report.xls", "isInline": False}


@pytest.fixture(autouse=True)
def graph_settings(monkeypatch):
    for name, value in [
        ("GRAPH_API_TENANT_ID", "tenant"),
        ("GRAPH_API_CLIENT_ID", "client"),
        ("GRAPH_API_CLIENT_SECRET", "secret"),
        ("GRAPH_API_MAILBOX", "mfgopstest@example.com"),
    ]:
        monkeypatch.setenv(name, value)


def mail(message_id, received, sender=FUSION, subject="Infoblox Orders Backlog Report"):
    return {
        "id": message_id,
        "receivedDateTime": received,
        "subject": subject,
        "from": {"emailAddress": {"address": sender}},
        "internetMessageId": f"<{message_id}@oracle>",
    }


def run_fetch(messages, attachments=(REPORT,), orders=None, read_error=None):
    client = fetch_backlog_from_mail.graph_mail_client
    with patch.object(
        client, "list_recent_messages", return_value=messages
    ), patch.object(
        client, "list_attachments", return_value=list(attachments)
    ), patch.object(
        client, "download_attachment", return_value=b"xls bytes"
    ), patch.object(
        fetch_backlog_from_mail,
        "read_backlog_report",
        side_effect=read_error,
        return_value=orders if orders is not None else get_sample_orders()[:2],
    ) as reader:
        return fetch_backlog_snapshot(), reader


def statuses():
    with get_connection() as connection:
        rows = connection.execute(
            "SELECT graph_message_id, status FROM mail_messages ORDER BY graph_message_id"
        ).fetchall()
    return {row["graph_message_id"]: row["status"] for row in rows}


def test_newest_report_is_read_and_older_ones_are_skipped(
    seeded_db, isolated_environment
):
    messages = [
        mail("m1", "2026-10-19T11:02:07Z"),
        mail("m3", "2026-10-19T11:33:17Z"),
        mail("m2", "2026-10-19T11:17:27Z"),
        mail("x", "2026-10-19T11:40:00Z", sender="someone@infoblox.com"),
        mail("y", "2026-10-19T11:41:00Z", subject="Re: hello"),
    ]
    snapshot, reader = run_fetch(messages)
    assert snapshot.message_id == "m3" and len(snapshot.orders) == 2
    assert statuses() == {"m1": "superseded", "m2": "superseded", "m3": "processed"}
    saved = reader.call_args.args[0]
    assert saved.startswith(str(isolated_environment / "attachments" / "backlog"))
    assert open(saved, "rb").read() == b"xls bytes"
    status = mail_store.get_mail_sync_status()
    assert status["last_processed"]["order_count"] == 2 and status["last_error"] is None


def test_no_new_report_means_none(seeded_db):
    run_fetch([mail("m1", "2026-10-19T11:02:07Z")])
    snapshot, _ = run_fetch([mail("m1", "2026-10-19T11:02:07Z")])  # already read
    assert snapshot is None
    snapshot, _ = run_fetch([])
    assert snapshot is None


def test_an_empty_report_is_not_the_same_as_no_mail(seeded_db):
    snapshot, _ = run_fetch([mail("m1", "2026-10-19T11:02:07Z")], orders=[])
    assert snapshot is not None and snapshot.orders == []


def test_unreadable_report_is_recorded_and_not_retried(seeded_db):
    with pytest.raises(BacklogMailError, match="missing these backlog report columns"):
        run_fetch(
            [mail("m1", "2026-10-19T11:02:07Z")],
            read_error=ValueError("missing these backlog report columns: FOB"),
        )
    assert statuses() == {"m1": "failed"}
    assert "FOB" in mail_store.get_mail_sync_status()["last_error"]
    snapshot, _ = run_fetch([mail("m1", "2026-10-19T11:02:07Z")])
    assert snapshot is None


def test_report_without_the_attachment_is_explained(seeded_db):
    with pytest.raises(BacklogMailError, match="attachment"):
        run_fetch([mail("m1", "2026-10-19T11:02:07Z")], attachments=[])


def test_attachment_is_found_by_name_or_as_the_only_xls(seeded_db):
    config = fetch_backlog_from_mail.load_config()
    other = {"id": "a2", "name": "logo.png"}
    renamed = {"id": "a3", "name": "Backlog 19 Oct.xls"}
    pick = fetch_backlog_from_mail.pick_report_attachment
    assert pick([other, REPORT], config) == REPORT
    assert pick([other, renamed], config) == renamed
    assert pick([renamed, {**renamed, "id": "a4"}], config) is None  # ambiguous


def test_search_starts_just_before_the_newest_known_report(seeded_db):
    assert fetch_backlog_from_mail.find_search_start() < "9999"
    run_fetch([mail("m1", "2026-10-19T11:02:07Z")])
    assert fetch_backlog_from_mail.find_search_start() == "2026-10-19T09:02:07Z"
