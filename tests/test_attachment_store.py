from src.knowledge_layer import attachment_store


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
