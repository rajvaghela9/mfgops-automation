from src.knowledge_layer import attachment_store, eus_document_store


def test_attachment_content_round_trips_through_sqlite(tmp_path):
    output_dir = tmp_path / "attachments"
    relative_path = "20261022_120000_message-1/quote.pdf"
    content = b"quote bytes"

    attachment_store.store_attachment(
        message_id="message-1",
        attachment_id="attachment-1",
        mailbox="mailbox@example.com",
        sender_email="sender@example.com",
        folder_name="20261022_120000_message-1",
        relative_path=relative_path,
        file_name="quote.pdf",
        content_type="application/pdf",
        content=content,
        received_at="2026-10-22T12:00:00Z",
    )

    restored = attachment_store.materialize_attachments(output_dir)

    assert restored == [output_dir / relative_path]
    assert (output_dir / relative_path).read_bytes() == content


def test_existing_file_is_migrated_to_sqlite_and_restored(tmp_path):
    output_dir = tmp_path / "attachments"
    existing = output_dir / "mail-1" / "quote.pdf"
    existing.parent.mkdir(parents=True)
    existing.write_bytes(b"legacy bytes")

    assert attachment_store.migrate_files(output_dir) == 1
    existing.unlink()

    attachment_store.materialize_attachments(output_dir)

    assert existing.read_bytes() == b"legacy bytes"


def test_eus_documents_track_active_and_reviewed_documents():
    paths = ["mail-1/eus.pdf", "mail-1/quote.pdf", "mail-2/eus.pdf"]
    for index, path in enumerate(paths):
        folder_name, file_name = path.split("/")
        attachment_store.store_attachment(
            message_id=folder_name,
            attachment_id=str(index),
            mailbox="mailbox@example.com",
            sender_email="sender@example.com",
            folder_name=folder_name,
            relative_path=path,
            file_name=file_name,
            content_type="application/pdf",
            content=b"pdf",
            received_at="2026-10-22T12:00:00Z",
        )

    eus_document_store.sync_documents()
    attachment_store.record_decision("mail-2/eus.pdf", "reject", "Tester", "Bad")

    assert eus_document_store.get_active_folders() == {"mail-1"}


def test_repeat_customer_requires_an_approved_eus_in_another_email():
    for folder_name in ("mail-1", "mail-2"):
        attachment_store.store_attachment(
            message_id=folder_name,
            attachment_id="eus",
            mailbox="mailbox@example.com",
            sender_email="sender@example.com",
            folder_name=folder_name,
            relative_path=f"{folder_name}/eus.pdf",
            file_name="eus.pdf",
            content_type="application/pdf",
            content=b"pdf",
            received_at="2026-10-22T12:00:00Z",
        )
    eus_document_store.sync_documents()
    eus_document_store.set_end_user("mail-1", "eus.pdf", "JSC Ukrsibbank")

    assert not eus_document_store.is_repeat_customer("JSC UKRSIBBANK", "mail-2")
    attachment_store.record_decision("mail-1/eus.pdf", "approve", "Tester")
    assert eus_document_store.is_repeat_customer("JSC UKRSIBBANK", "mail-2")
    assert not eus_document_store.is_repeat_customer("JSC UKRSIBBANK", "mail-1")
