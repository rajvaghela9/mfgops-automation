import json
import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from streamlit.testing.v1 import AppTest

from src.app import run_pipeline_once
from src.intelligence_layer.eus_document_review import EusCheck, EusReview
from src.knowledge_layer import (
    attachment_store,
    order_store,
    special_instruction_store,
)

UI_SCRIPT = str(Path(__file__).parents[1] / "src" / "ui_layer" / "app.py")


def open_ui(reviewer=""):
    app = AppTest.from_file(UI_SCRIPT, default_timeout=60).run()
    if reviewer:
        app.sidebar.text_input(key="reviewer").set_value(reviewer).run()
    return app


def test_ui_shows_every_tab_without_errors(seeded_db, sample_backlog):
    run_pipeline_once()
    app = open_ui()
    assert not app.exception
    assert [tab.label for tab in app.tabs][:4] == [
        "📊 Dashboard",
        "📝 Orders to Review",
        "📦 Order Tracker",
        "💬 Rules Assistant",
    ]
    assert len(app.expander) >= 12  # one card per order to review


def card_titles(app):
    return [e.label for e in app.expander if "·" in e.label]


def test_dummy_orders_come_last_and_no_uspo_is_shown_before_release(
    seeded_db, sample_backlog
):
    from src.utils.mock_data import get_dummy_orders

    for order in get_dummy_orders():
        order_store.upsert_order(order)  # dummy orders seen first ...
    run_pipeline_once()  # ... and the sample (real) orders after them
    app = open_ui()
    titles = card_titles(app)
    dummy_positions = [i for i, title in enumerate(titles) if "dummy_" in title]
    assert dummy_positions == list(range(len(titles) - 5, len(titles)))
    assert all("🧪" in titles[i] for i in dummy_positions)
    page = " ".join(str(element.value) for element in app.caption)
    assert "USPO" not in page and "Arrow PO" not in page


def test_reviewer_can_approve_but_never_an_embargoed_order(seeded_db, sample_backlog):
    run_pipeline_once()
    app = open_ui("Tester")
    approve = {button.key: button for button in app.button if "Approve" in button.label}
    embargoed = next(key for key in approve if "DIST-MOCK-0005" in key)
    clean = next(key for key in approve if "REN-1103701" in key)
    assert approve[embargoed].disabled
    approve[clean].click().run()
    assert not app.exception
    assert any(
        "REN-1103701 is approved" in box.value and "USPO" in box.value
        for box in app.success
    )
    assert order_store.get_order("REN-1103701").status == "APPROVED"
    waiting = [e for e in app.expander if "waiting for the USPO" in e.label]
    assert waiting and "(1)" in waiting[0].label

    sample_backlog["REN-1103701"] = "USPO46100"
    run_pipeline_once()
    app = open_ui("Tester")
    assert not [e for e in app.expander if "waiting for the USPO" in e.label]
    assert not any("REN-1103701" in title for title in card_titles(app))
    all_orders = next(df.value for df in app.dataframe if "USPO" in df.value)
    assert (
        all_orders[all_orders["Order"] == "REN-1103701"]["USPO"].item() == "USPO46100"
    )


def test_new_rule_shows_up_marked_right_after_saving(seeded_db):
    new_rule = {
        "name": "Saudi commercial invoice",
        "rule_type": "soft",
        "outcome": "hold",
        "description": "Saudi Arabia orders need a commercial invoice.",
        "guidance": "Hold unless the invoice is attached.",
    }
    function = SimpleNamespace(name="add_rule", arguments=json.dumps(new_rule))
    asks_to_add = SimpleNamespace(
        content=None, tool_calls=[SimpleNamespace(id="c1", function=function)]
    )
    confirms = SimpleNamespace(content="Saved it.", tool_calls=None)
    app = open_ui("Tester")
    marks_before = [df.value[""].tolist() for df in app.dataframe if "Rule" in df.value]
    assert not any(any(marks) for marks in marks_before)

    with patch(
        "src.intelligence_layer.llm_client.ask_ai", side_effect=[asks_to_add, confirms]
    ):
        app.chat_input[0].set_value("Saudi orders need an invoice").run()
        assert any("Add this new rule?" in box.value for box in app.info)
        next(b for b in app.button if "Yes, save it" in b.label).click().run()

    assert not app.exception
    rules_table = next(df.value for df in app.dataframe if "Rule" in df.value)
    assert (
        rules_table[rules_table["Rule"] == "Saudi commercial invoice"][""].item()
        == "🆕 New"
    )
    assert [e for e in app.expander if "Current business rules" in e.label][
        0
    ].proto.expanded


def prepare_eus_attachment(name="eus_20261015_120000.txt"):
    folder = Path(os.environ["ATTACHMENT_OUTPUT_DIR"])
    path = folder / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"eus data")
    return name


def prepare_eus_mail_folder(folder="20261015_120000_message_1"):
    """Save two attachments in one per-email folder, as the downloader does."""
    root = Path(os.environ["ATTACHMENT_OUTPUT_DIR"])
    (root / folder).mkdir(parents=True, exist_ok=True)
    (root / folder / "invoice.pdf").write_bytes(b"invoice data")
    (root / folder / "packing-list.txt").write_bytes(b"packing list data")
    (root / folder / ".mail.json").write_text(
        json.dumps(
            {
                "subject": "PO 123 special instructions",
                "sender_email": "om@example.com",
                "received_at": "2026-10-15T12:00:00Z",
                "body_content_type": "text",
                "body": "Ship using the attached PO and Quote.",
            }
        ),
        encoding="utf-8",
    )
    return folder


def patched_sender():
    config = {
        "tenant_id": "t",
        "client_id": "c",
        "client_secret": "s",
        "mailbox": "mfgopstest@infoblox.com",
    }
    return (
        patch("src.send_attachment.load_send_configuration", return_value=config),
        patch("src.graph_attachments.acquire_access_token", return_value="token"),
        patch("src.send_attachment.send_attachment"),
    )


def patched_package_sender():
    config = {
        "tenant_id": "t",
        "client_id": "c",
        "client_secret": "s",
        "mailbox": "mfgopstest@infoblox.com",
    }
    return (
        patch("src.send_attachment.load_send_configuration", return_value=config),
        patch("src.graph_attachments.acquire_access_token", return_value="token"),
        patch("src.send_attachment.send_attachments"),
    )


def graph_config():
    return {
        "tenant_id": "t",
        "client_id": "c",
        "client_secret": "s",
        "mailbox": "mfgopstest@infoblox.com",
        "sender_emails": ["usahu@infoblox.com"],
        "output_dir": os.environ["ATTACHMENT_OUTPUT_DIR"],
    }


def patched_mailbox(downloaded_name):
    """Pretend the mailbox holds one attachment with the given name.

    AppTest imports the UI script as its own module, so the patches have to
    target the source modules the script imports from.
    """
    folder = Path(os.environ["ATTACHMENT_OUTPUT_DIR"]) / "20261015_153000_message_1"

    def download(
        mailbox,
        sender_email,
        output_dir,
        processed_message_ids=None,
        on_message_processed=None,
    ):
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / downloaded_name
        path.write_bytes(b"eus data")
        if on_message_processed is not None:
            on_message_processed(
                {
                    "id": "message-1",
                    "receivedDateTime": "2026-10-15T15:30:00Z",
                }
            )
        return [path]

    retriever = SimpleNamespace(download_sender_attachments=download)
    return (
        patch(
            "src.graph_attachments.load_configuration", return_value=graph_config()
        ),
        patch("src.graph_attachments.acquire_access_token", return_value="token"),
        patch(
            "src.graph_attachments.GraphAttachmentRetriever", return_value=retriever
        ),
    )


def click_check_mailbox(app):
    next(b for b in app.button if "Check mailbox" in b.label).click().run()


def test_eus_fetch_button_downloads_newly_shared_attachments(seeded_db):
    config, token, retriever = patched_mailbox("eus_new_20261015_153000.pdf")
    review = EusReview(
        checks=[EusCheck("document", "Documents extracted", True, "Ready.")],
        document_types={"eus_new_20261015_153000.pdf": "eus"},
    )
    with (
        config,
        token,
        retriever,
        patch(
            "src.intelligence_layer.eus_document_review.review_mail_documents",
            return_value=review,
        ) as run_review,
    ):
        app = open_ui("Tester")
        click_check_mailbox(app)
    assert not app.exception
    run_review.assert_called_once()
    assert any(
        value.startswith("1 new EUS attachment(s) downloaded from the mailbox to ")
        for value in [box.value for box in app.success]
    )
    assert any(
        "eus_new_20261015_153000.pdf" in expander.label for expander in app.expander
    )


def test_eus_fetch_button_reports_when_nothing_is_new(seeded_db):
    prepare_eus_attachment("20261015_153000_message_1/eus_old_20261015_120000.pdf")
    name = "eus_old_20261015_120000.pdf"
    config, token, retriever = patched_mailbox(name)
    with config, token, retriever:
        app = open_ui("Tester")
        click_check_mailbox(app)
    assert not app.exception
    assert any(
        value.startswith("No new EUS attachments. 1 already downloaded in ")
        for value in [box.value for box in app.success]
    )


def test_eus_attachments_are_grouped_under_the_email_that_brought_them(seeded_db):
    folder = prepare_eus_mail_folder()

    app = open_ui("Tester")

    assert not app.exception
    headings = [heading.value for heading in app.subheader]
    assert "📧 Email received 15 Oct 2026, 12:00:00 UTC" in headings
    captions = [caption.value for caption in app.caption]
    assert f"2 attachment(s) in {folder}/" in captions
    labels = [expander.label for expander in app.expander]
    assert any("invoice.pdf" in label for label in labels)
    assert any("packing-list.txt" in label for label in labels)


def test_eus_automated_checks_show_pass_and_fail_results(seeded_db):
    prepare_eus_mail_folder()
    review = EusReview(
        checks=[
            EusCheck("quote-details", "EUS details match quote", True, "All match."),
            EusCheck(
                "question-6",
                'Question 6 has "No" selected',
                False,
                '"No" was not clearly selected.',
            ),
        ],
        document_types={"invoice.pdf": "eus", "packing-list.txt": "quote"},
    )
    with patch(
        "src.intelligence_layer.eus_document_review.review_mail_documents",
        return_value=review,
    ):
        app = open_ui("Tester")
        next(
            button
            for button in app.button
            if "Re-run automated EUS checks" in button.label
        ).click().run()

    assert not app.exception
    assert any("automated checks failed" in warning.value for warning in app.warning)
    failures = "\n".join(error.value for error in app.error)
    assert '**Question 6 has "No" selected**' in failures
    assert '"No" was not clearly selected.' in failures
    results = "\n".join(markdown.value for markdown in app.markdown)
    assert "✅ **EUS details match quote**" in results
    assert '❌ **Question 6 has "No" selected**' in results
    check_expanders = [
        expander
        for expander in app.expander
        if expander.label.startswith("🧾 Automated EUS checks")
    ]
    assert check_expanders
    assert all(
        expander.label.endswith("1 check(s) failed") for expander in check_expanders
    )
    all_tab = next(tab for tab in app.tabs if tab.label == "All EUS documents")
    assert len(
        [button for button in all_tab.button if "Approve & forward" in button.label]
    ) == 1


def test_eus_approve_forwards_the_attachment(seeded_db):
    relative_path = prepare_eus_attachment(
        "20261015_120000_message_1/eus_20261015_120000.txt"
    )
    name = Path(relative_path).name
    load, token, send = patched_sender()
    with load, token, send as sender:
        app = open_ui("Tester")
        next(b for b in app.button if "Approve & forward" in b.label).click().run()
    assert not app.exception
    assert sender.call_count == 1
    args = sender.call_args.args
    assert args[2] == "nkp@infoblox.com"
    assert args[3] == f"EUS Approved: {name}"
    assert "EUS review outcome: Approved." in args[4]
    assert args[5].name == name
    assert f"{name} was approved and forwarded to nkp@infoblox.com." in [
        box.value for box in app.success
    ]
    saved = attachment_store.get_decision(relative_path)
    assert saved["decision"] == "approve"
    assert saved["decided_by"] == "Tester"
    assert saved["comment"] == ""


def test_special_instructions_without_eus_can_be_shared(seeded_db):
    folder = prepare_eus_mail_folder()
    review = EusReview(
        checks=[EusCheck("documents", "Documents classified", True, "Ready.")],
        document_types={"invoice.pdf": "quote", "packing-list.txt": "po"},
    )
    load, token, send = patched_package_sender()
    with (
        patch(
            "src.intelligence_layer.eus_document_review.review_mail_documents",
            return_value=review,
        ),
        load,
        token,
        send as sender,
    ):
        app = open_ui("Tester")
        next(
            button
            for button in app.button
            if "Re-run automated EUS checks" in button.label
        ).click().run()
        share = next(
            button
            for button in app.button
            if "Share special instructions" in button.label
        )
        assert not share.disabled
        share.click().run()

    assert not app.exception
    sender.assert_called_once()
    args = sender.call_args.args
    assert args[2] == "nkp@infoblox.com"
    assert args[3] == "Special instructions: PO 123 special instructions"
    assert {path.name for path in args[5]} == {"invoice.pdf", "packing-list.txt"}
    assert sender.call_args.kwargs["body_content_type"] == "text"
    shared = special_instruction_store.get_share(folder)
    assert shared["shared_by"] == "Tester"


def test_special_instructions_wait_for_eus_approval(seeded_db):
    folder = prepare_eus_mail_folder()
    review = EusReview(
        checks=[EusCheck("documents", "Documents classified", True, "Ready.")],
        document_types={"invoice.pdf": "eus", "packing-list.txt": "quote"},
    )
    with patch(
        "src.intelligence_layer.eus_document_review.review_mail_documents",
        return_value=review,
    ):
        app = open_ui("Tester")
        next(
            button
            for button in app.button
            if "Re-run automated EUS checks" in button.label
        ).click().run()

    share = next(
        button
        for button in app.button
        if "Share special instructions" in button.label
    )
    assert share.disabled
    attachment_store.record_decision(
        f"{folder}/invoice.pdf", "approve", "Tester"
    )
    load, token, send = patched_package_sender()
    # Callbacks use the previous run's globals, so patch before that run.
    with load, token, send as sender:
        app.run()
        share = next(
            button
            for button in app.button
            if "Share special instructions" in button.label
        )
        assert not share.disabled
        share.click().run()

    assert not app.exception
    sender.assert_called_once()
    assert [path.name for path in sender.call_args.args[5]] == ["packing-list.txt"]


def test_eus_reject_without_a_comment_sends_nothing(seeded_db):
    name = prepare_eus_attachment()
    load, token, send = patched_sender()
    with load, token, send as sender:
        app = open_ui("Tester")
        next(b for b in app.button if "Reject & forward" in b.label).click().run()
    assert not app.exception
    sender.assert_not_called()
    assert f"{name}: add a comment before rejecting." in [
        box.value for box in app.error
    ]


def test_eus_reject_forwards_the_comment(seeded_db):
    name = prepare_eus_attachment()
    load, token, send = patched_sender()
    with load, token, send as sender:
        app = open_ui("Tester")
        app.text_area(key=f"eus_comment_{name}").set_value("Signature missing")
        next(b for b in app.button if "Reject & forward" in b.label).click().run()
    assert not app.exception
    assert sender.call_count == 1
    args = sender.call_args.args
    assert args[2] == "nkp@infoblox.com"
    assert args[3] == f"EUS Rejected: {name}"
    assert "Signature missing" in args[4]
    assert "Reviewed by: Tester." in args[4]
    saved = attachment_store.get_decision(name)
    assert saved["decision"] == "reject"
    assert saved["decided_by"] == "Tester"
    assert saved["comment"] == "Signature missing"


def test_eus_approval_has_active_then_all_document_tabs(seeded_db):
    approved = prepare_eus_attachment("20261015_120000_message_1/approved.pdf")
    sibling = prepare_eus_attachment("20261015_120000_message_1/quote.pdf")
    active = prepare_eus_attachment("20261016_120000_message_2/active.pdf")
    prepare_eus_attachment("20261016_120000_message_2/active-po.pdf")

    app = open_ui("Tester")
    attachment_store.record_decision(approved, "approve", "Tester")
    app.run()

    assert not app.exception
    tab_labels = [tab.label for tab in app.tabs]
    assert tab_labels.index("Active EUS documents") + 1 == tab_labels.index(
        "All EUS documents"
    )
    all_tab = next(tab for tab in app.tabs if tab.label == "All EUS documents")
    active_tab = next(tab for tab in app.tabs if tab.label == "Active EUS documents")
    all_labels = [expander.label for expander in all_tab.expander]
    active_labels = [expander.label for expander in active_tab.expander]
    for path in (approved, sibling, active):
        assert any(Path(path).name in label for label in all_labels)
    # One decided file moves its whole email folder out of Active.
    assert not any(Path(approved).name in label for label in active_labels)
    assert not any(Path(sibling).name in label for label in active_labels)
    assert any(Path(active).name in label for label in active_labels)
    assert "1 EUS document(s) waiting for approval." in [
        box.value for box in app.sidebar.info
    ]
    assert not any("<iframe" in markdown.value for markdown in app.markdown)
