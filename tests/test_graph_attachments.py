import json

import pytest

from src.graph_attachments import (
    GraphAttachmentRetriever,
    extract_special_instructions,
    load_configuration,
)


class FakeResponse:
    def __init__(self, data=None, content=b""):
        self.data = data or {}
        self.content = content

    def raise_for_status(self):
        pass

    def json(self):
        return self.data

    def iter_content(self, chunk_size):
        yield self.content


class FakeSession:
    def __init__(self):
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        if url.endswith("/messages"):
            return FakeResponse(
                {
                    "value": [
                        {
                            "id": "message/1",
                            "subject": "Urgent shipping instructions",
                            "from": {
                                "emailAddress": {
                                    "address": "supplier@example.com"
                                }
                            },
                            "receivedDateTime": "2026-10-15T12:15:00Z",
                            "body": {
                                "contentType": "html",
                                "content": (
                                    "<p>Hello team,</p>"
                                    "<p><b>Special instructions:</b><br>"
                                    "Use the attached PO and quote.<br>"
                                    "Ship through the approved carrier.</p>"
                                    "<p>Regards,<br>Supplier</p>"
                                ),
                            },
                        }
                    ],
                    "@odata.nextLink": "https://graph.microsoft.com/v1.0/messages?page=2",
                }
            )
        if url.endswith("messages?page=2"):
            return FakeResponse({"value": [{"id": "message-2"}]})
        if url.endswith("/message%2F1/attachments"):
            return FakeResponse(
                {
                    "value": [
                        {
                            "id": "attachment/1",
                            "name": "../invoice.pdf",
                            "@odata.type": "#microsoft.graph.fileAttachment",
                        },
                        {
                            "id": "attachment/2",
                            "name": "packing-list.xlsx",
                            "@odata.type": "#microsoft.graph.fileAttachment",
                        },
                        {
                            "id": "reference-1",
                            "name": "shared document",
                            "@odata.type": "#microsoft.graph.referenceAttachment",
                        },
                        {
                            "id": "inline-1",
                            "name": "image001.png",
                            "isInline": False,
                            "contentId": "image001@signature",
                            "@odata.type": "#microsoft.graph.fileAttachment",
                        },
                        {
                            "id": "inline-2",
                            "name": "img-0d9a9e54-fd0e-40ac-b928-7ff9434e9586.png",
                            "isInline": False,
                            "@odata.type": "#microsoft.graph.fileAttachment",
                        },
                    ]
                }
            )
        if url.endswith("/message-2/attachments"):
            return FakeResponse({"value": []})
        if url.endswith("/attachment%2F1/$value"):
            return FakeResponse(content=b"invoice data")
        if url.endswith("/attachment%2F2/$value"):
            return FakeResponse(content=b"packing list data")
        raise AssertionError(f"Unexpected Graph request: {url}")


def test_downloads_sender_attachments_safely_and_follows_pages(tmp_path):
    session = FakeSession()
    retriever = GraphAttachmentRetriever("test-token", session=session)

    downloaded = retriever.download_sender_attachments(
        "mailbox@example.com",
        "supplier@example.com",
        tmp_path,
    )

    assert len(downloaded) == 2
    assert [path.relative_to(tmp_path).as_posix() for path in downloaded] == [
        "20261015_121500_message_1/invoice.pdf",
        "20261015_121500_message_1/packing-list.xlsx",
    ]
    assert downloaded[0].read_bytes() == b"invoice data"
    assert downloaded[1].read_bytes() == b"packing list data"
    assert any(url.endswith("messages?page=2") for url, _ in session.calls)
    message_request = next(
        kwargs for url, kwargs in session.calls if url.endswith("/messages")
    )
    assert message_request["params"]["$filter"] == (
        "from/emailAddress/address eq 'supplier@example.com' "
        "and hasAttachments eq true"
    )
    assert [path.name for path in downloaded] == [
        "invoice.pdf",
        "packing-list.xlsx",
    ]
    assert len({path.parent for path in downloaded}) == 1
    metadata = json.loads(
        (tmp_path / "20261015_121500_message_1" / ".mail.json").read_text(
            encoding="utf-8"
        )
    )
    assert metadata["subject"] == "Urgent shipping instructions"
    assert metadata["body"] == (
        "Use the attached PO and quote.\n"
        "Ship through the approved carrier."
    )
    assert "Hello team" not in metadata["body"]
    assert "Supplier" not in metadata["body"]
    print(
        "Success: simulated attachment retrieval passed; "
        "this test does not access the live mailbox."
    )


def test_inline_signature_images_and_icons_are_not_downloaded(tmp_path):
    session = FakeSession()
    retriever = GraphAttachmentRetriever("test-token", session=session)

    downloaded = retriever.download_sender_attachments(
        "mailbox@example.com",
        "supplier@example.com",
        tmp_path,
    )

    assert "image001.png" not in [path.name for path in downloaded]
    assert not any("img-0d9a9e54" in path.name for path in downloaded)
    assert not any("inline-1/$value" in url for url, _ in session.calls)
    print("Success: inline signature images and icons were ignored.")


def test_reference_attachments_have_no_content_to_save(tmp_path):
    session = FakeSession()
    retriever = GraphAttachmentRetriever("test-token", session=session)

    downloaded = retriever.download_sender_attachments(
        "mailbox@example.com",
        "supplier@example.com",
        tmp_path,
    )

    # Reference attachments are OneDrive/SharePoint links; Graph serves no file
    # content for them, so there is nothing to write into the mail folder.
    assert not any("reference-1/$value" in url for url, _ in session.calls)
    assert "shared document" not in [path.name for path in downloaded]
    print("Success: link-only reference attachments were left out as documented.")


def test_processed_messages_are_not_downloaded_again(tmp_path):
    session = FakeSession()
    retriever = GraphAttachmentRetriever("test-token", session=session)
    processed = []
    existing = tmp_path / "20261015_121500_message_1"
    existing.mkdir()
    (existing / "invoice.pdf").write_bytes(b"existing invoice")

    downloaded = retriever.download_sender_attachments(
        "mailbox@example.com",
        "supplier@example.com",
        tmp_path,
        processed_message_ids={"message/1"},
        on_message_processed=lambda message: processed.append(message["id"]),
    )

    assert downloaded == []
    assert processed == ["message-2"]
    assert not any(
        url.endswith("/message%2F1/attachments") for url, _ in session.calls
    )
    assert (existing / ".mail.json").is_file()


def test_email_body_without_special_instructions_is_not_retained(tmp_path):
    existing = tmp_path / "mail"
    existing.mkdir()
    (existing / ".mail.json").write_text('{"body": "old body"}', encoding="utf-8")

    GraphAttachmentRetriever._write_mail_metadata(
        existing,
        {
            "id": "message-1",
            "receivedDateTime": "2026-10-15T12:15:00Z",
            "body": {
                "contentType": "html",
                "content": "<p>Please review the attached quote.</p>",
            },
        },
        "supplier@example.com",
    )

    assert not (existing / ".mail.json").exists()


def test_extracts_inline_special_instruction_marker():
    body = "Order is attached. Special Instruction - Use DHL account 123."

    assert extract_special_instructions(body, "text") == "Use DHL account 123."


def test_extracts_shipping_instruction_marker_used_by_order_emails():
    body = (
        "Hello,\n"
        "Kindly find the shipping instruction for order REN-1104372 below,\n"
        "Use the customer's DHL account.\n"
        "Do not split the shipment.\n"
        "Regards,\n"
        "Order Management"
    )

    assert extract_special_instructions(body, "text") == (
        "for order REN-1104372 below,\n"
        "Use the customer's DHL account.\n"
        "Do not split the shipment."
    )


def test_removes_sender_signature_without_removing_instruction_contacts():
    body = (
        "Shipping instructions:\n"
        "Send documents to logistics@example.com.\n\n"
        "Nischith K P\n"
        "Software Engineer Intern\n"
        "nkp@infoblox.com"
    )

    assert extract_special_instructions(
        body, "text", "nkp@infoblox.com"
    ) == "Send documents to logistics@example.com."


def test_load_configuration_reports_missing_settings():
    with pytest.raises(ValueError, match="GRAPH_TENANT_ID.*GRAPH_MAILBOX"):
        load_configuration({})
    print("Success: missing Graph configuration settings were reported correctly.")
