import base64

import pytest
import requests

from src import send_attachment as sender_module
from src.send_attachment import send_attachment, send_attachments


class FakeResponse:
    def __init__(self, data=None):
        self._data = data or {}

    def raise_for_status(self):
        pass

    def json(self):
        return self._data


class FakeSession:
    def __init__(self):
        self.calls = []
        self.puts = []
        self.deleted = []

    def post(self, url, **kwargs):
        self.calls.append((url, kwargs))
        if url.endswith("/messages"):
            return FakeResponse({"id": "draft/1"})
        if url.endswith("/createUploadSession"):
            return FakeResponse({"uploadUrl": "https://upload.example/session"})
        return FakeResponse()

    def put(self, url, **kwargs):
        self.puts.append((url, kwargs))
        return FakeResponse()

    def delete(self, url, **kwargs):
        self.deleted.append(url)
        return FakeResponse()


def test_sends_attachment_with_subject_and_comments(tmp_path):
    attachment = tmp_path / "invoice_20261015_121500.txt"
    attachment.write_bytes(b"invoice data")
    session = FakeSession()

    sent_path = send_attachment(
        "test-token",
        "mailbox@example.com",
        "recipient@example.com",
        "Invoice review",
        "Please review the attached invoice.",
        attachment,
        session=session,
    )

    assert sent_path == attachment
    assert len(session.calls) == 1
    url, request = session.calls[0]
    assert url.endswith("/users/mailbox%40example.com/sendMail")
    assert request["headers"]["Authorization"] == "Bearer test-token"
    message = request["json"]["message"]
    assert message["subject"] == "Invoice review"
    assert message["body"] == {
        "contentType": "Text",
        "content": "Please review the attached invoice.",
    }
    assert message["toRecipients"][0]["emailAddress"]["address"] == (
        "recipient@example.com"
    )
    graph_attachment = message["attachments"][0]
    assert graph_attachment["name"] == attachment.name
    assert base64.b64decode(graph_attachment["contentBytes"]) == b"invoice data"
    assert request["json"]["saveToSentItems"] is True


def test_sends_special_instructions_with_all_attachments(tmp_path):
    po = tmp_path / "customer-po.pdf"
    quote = tmp_path / "quote.pdf"
    eus = tmp_path / "verified-eus.pdf"
    po.write_bytes(b"po")
    quote.write_bytes(b"quote")
    eus.write_bytes(b"eus")
    session = FakeSession()

    sent_paths = send_attachments(
        "test-token",
        "mailbox@example.com",
        "recipient@example.com",
        "Special instructions: Order 123",
        "<p>Use the attached documents.</p>",
        [po, quote, eus],
        session=session,
        body_content_type="HTML",
    )

    assert sent_paths == [po.resolve(), quote.resolve(), eus.resolve()]
    message = session.calls[0][1]["json"]["message"]
    assert message["body"]["contentType"] == "HTML"
    assert [item["name"] for item in message["attachments"]] == [
        "customer-po.pdf",
        "quote.pdf",
        "verified-eus.pdf",
    ]


def test_rejects_missing_attachment(tmp_path):
    with pytest.raises(FileNotFoundError, match="Attachment does not exist"):
        send_attachment(
            "test-token",
            "mailbox@example.com",
            "recipient@example.com",
            "Invoice review",
            "Please review the attached invoice.",
            tmp_path / "missing.txt",
        )


def big_attachment(tmp_path, monkeypatch, payload=b"0123456789"):
    """Make a file the sender has to upload in chunks instead of in one request."""
    monkeypatch.setattr(sender_module, "MAX_SIMPLE_ATTACHMENT_SIZE", 5)
    monkeypatch.setattr(sender_module, "UPLOAD_CHUNK_SIZE", 4)
    attachment = tmp_path / "eus_20261015_121500.pdf"
    attachment.write_bytes(payload)
    return attachment


def test_large_attachment_is_uploaded_in_chunks_and_sent(tmp_path, monkeypatch):
    payload = b"0123456789"
    attachment = big_attachment(tmp_path, monkeypatch, payload)
    session = FakeSession()

    send_attachment(
        "test-token",
        "mailbox@example.com",
        "recipient@example.com",
        "EUS Approved",
        "EUS review outcome: Approved.",
        attachment,
        session=session,
    )

    urls = [url for url, _ in session.calls]
    assert urls[0].endswith("/users/mailbox%40example.com/messages")
    assert urls[1].endswith("/messages/draft%2F1/attachments/createUploadSession")
    assert urls[2].endswith("/messages/draft%2F1/send")

    item = session.calls[1][1]["json"]["AttachmentItem"]
    assert item == {
        "attachmentType": "file",
        "name": attachment.name,
        "contentType": "application/pdf",
        "size": len(payload),
    }

    ranges = [kwargs["headers"]["Content-Range"] for _, kwargs in session.puts]
    assert ranges == ["bytes 0-3/10", "bytes 4-7/10", "bytes 8-9/10"]
    assert b"".join(kwargs["data"] for _, kwargs in session.puts) == payload
    # The pre-authenticated upload URL must not carry the Graph bearer token.
    assert all("Authorization" not in kwargs["headers"] for _, kwargs in session.puts)
    assert session.deleted == []


def test_failed_upload_removes_the_draft(tmp_path, monkeypatch):
    attachment = big_attachment(tmp_path, monkeypatch)

    class FailingSession(FakeSession):
        def put(self, url, **kwargs):
            raise requests.RequestException("upload interrupted")

    session = FailingSession()
    with pytest.raises(requests.RequestException):
        send_attachment(
            "test-token",
            "mailbox@example.com",
            "recipient@example.com",
            "EUS Approved",
            "EUS review outcome: Approved.",
            attachment,
            session=session,
        )

    assert len(session.deleted) == 1
    assert session.deleted[0].endswith("/messages/draft%2F1")
    assert not any(url.endswith("/send") for url, _ in session.calls)


def test_rejects_attachment_above_the_graph_limit(tmp_path, monkeypatch):
    monkeypatch.setattr(sender_module, "MAX_ATTACHMENT_SIZE", 4)
    attachment = tmp_path / "huge.pdf"
    attachment.write_bytes(b"12345")

    with pytest.raises(ValueError, match="Microsoft Graph"):
        send_attachment(
            "test-token",
            "mailbox@example.com",
            "recipient@example.com",
            "EUS Approved",
            "EUS review outcome: Approved.",
            attachment,
            session=FakeSession(),
        )
