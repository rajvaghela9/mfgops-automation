from types import SimpleNamespace
from unittest.mock import patch

import pytest

from src.app import run_pipeline_once
from src.integration_layer import send_back_to_om
from src.integration_layer.outbound_mail import MailNotSentError
from src.intelligence_layer.order_review_workflow import (
    confirm_release_from_report,
    get_workflow,
    submit_reviewer_decision,
)
from src.knowledge_layer import learning_request_store, mail_store, order_store
from src.utils.models import OrderStatus

# The AI is off in tests, so verdicts come from the fixed checks only.


def all_orders():
    return {row["order_no"]: row for row in order_store.get_all_orders()}


def next_steps(order_no):
    order = order_store.get_order(order_no)
    return (
        get_workflow()
        .get_state({"configurable": {"thread_id": order.review_thread_id}})
        .next
    )


def test_reviewed_orders_wait_without_running(seeded_db, sample_backlog):
    run_pipeline_once()
    order = order_store.get_order("REN-1103701")
    assert order.status == OrderStatus.WAITING_FOR_REVIEW.value
    assert order.ai_verdict == "approve"
    assert next_steps("REN-1103701") == ("wait_for_reviewer",)


def test_approved_order_waits_for_the_uspo_then_is_released(seeded_db, sample_backlog):
    run_pipeline_once()
    submit_reviewer_decision("REN-1103701", "approve", "Tester")
    waiting = order_store.get_order("REN-1103701")
    assert waiting.status == OrderStatus.APPROVED.value and waiting.approved_at
    assert next_steps("REN-1103701") == ("wait_for_uspo",)
    assert all_orders()["REN-1103701"]["agreed"] == 1
    with pytest.raises(ValueError, match="isn't waiting"):
        submit_reviewer_decision(
            "REN-1103701", "approve", "Tester"
        )  # no double approve

    assert run_pipeline_once()["sync"]["release_confirmed"] == 0  # no USPO yet
    sample_backlog["REN-1103701"] = "USPO46100"  # Fusion released it
    assert run_pipeline_once()["sync"]["release_confirmed"] == 1
    assert order_store.get_order("REN-1103701") is None
    record = all_orders()["REN-1103701"]
    assert (record["status"], record["uspo"], record["released_by"]) == (
        "RELEASED",
        "USPO46100",
        "Tester",
    )
    assert learning_request_store.get_open_learning_requests() == []


def test_dummy_orders_are_released_straight_after_approval(seeded_db):
    run_pipeline_once()  # mock mode: the five dummy orders
    submit_reviewer_decision("dummy_DIST-0005", "approve", "Tester", "EUS by email")
    assert order_store.get_order("dummy_DIST-0005") is None
    record = all_orders()["dummy_DIST-0005"]
    assert (record["status"], record["uspo"]) == ("RELEASED", "")


def test_special_instruction_order_mails_arrow_once_released(
    seeded_db, sample_backlog, isolated_environment
):
    run_pipeline_once()
    submit_reviewer_decision(
        "DIST-MOCK-0010", "approve", "Tester", "Order mail checked by hand"
    )
    assert not (isolated_environment / "outbox").exists()  # nothing before the USPO
    sample_backlog["DIST-MOCK-0010"] = "USPO45773"
    run_pipeline_once()
    mail = next((isolated_environment / "outbox").iterdir()).read_text()
    assert "USPO45773" in mail and "PARTIAL SHIPMENT NOT ALLOWED" in mail
    assert "arrow@example.com" in mail
    assert [row["kind"] for row in mail_store.get_recent_outbound()] == [
        mail_store.ARROW_NOTICE
    ]


def test_release_is_confirmed_even_if_the_saved_workflow_is_gone(
    seeded_db, sample_backlog
):
    run_pipeline_once()
    submit_reviewer_decision("REN-1103701", "approve", "Tester")
    order_store.update_order("REN-1103701", review_thread_id="lost-thread")
    assert confirm_release_from_report("REN-1103701", "USPO9")
    assert all_orders()["REN-1103701"]["uspo"] == "USPO9"
    assert not confirm_release_from_report("REN-1103701", "USPO9")  # already done


def test_embargoed_order_can_never_be_released(seeded_db, sample_backlog):
    run_pipeline_once()
    with pytest.raises(ValueError, match="never be released"):
        submit_reviewer_decision(
            "DIST-MOCK-0005", "approve", "Tester", "Please release"
        )
    assert (
        order_store.get_order("DIST-MOCK-0005").status
        == OrderStatus.WAITING_FOR_REVIEW.value
    )


def test_eus_hold_can_be_approved_with_a_comment(seeded_db, sample_backlog):
    run_pipeline_once()
    assert order_store.get_order("DIST-MOCK-0004").status == OrderStatus.ON_HOLD.value
    with pytest.raises(ValueError, match="comment"):
        submit_reviewer_decision("DIST-MOCK-0004", "approve", "Tester")
    submit_reviewer_decision("DIST-MOCK-0004", "hold", "Tester")
    submit_reviewer_decision(
        "DIST-MOCK-0004", "approve", "Tester", "EUS sent to Arrow by email"
    )
    assert order_store.get_order("DIST-MOCK-0004").status == "APPROVED"
    assert (
        learning_request_store.get_open_learning_requests() == []
    )  # a hold isn't a "no"


def test_disagreement_asks_the_ai_to_learn(seeded_db, sample_backlog):
    run_pipeline_once()
    submit_reviewer_decision(
        "FEDR-1103736", "approve", "Tester", "Customer has 10GE gear"
    )
    requests = learning_request_store.get_open_learning_requests()
    assert [
        (r["order_no"], r["ai_verdict"], r["reviewer_decision"]) for r in requests
    ] == [("FEDR-1103736", "reject", "approve")]
    assert "SFPPLUS" in requests[0]["order_details"]


def test_send_to_om_emails_and_the_corrected_order_is_reviewed_again(
    seeded_db, sample_backlog, isolated_environment
):
    run_pipeline_once()
    with pytest.raises(ValueError, match="OM should correct"):
        submit_reviewer_decision("DIST-MOCK-0003", "send_to_om", "Tester")
    submit_reviewer_decision(
        "DIST-MOCK-0003", "send_to_om", "Tester", "Add method and PO"
    )
    assert (
        order_store.get_order("DIST-MOCK-0003").status == OrderStatus.SENT_TO_OM.value
    )
    emails = list((isolated_environment / "outbox").iterdir())
    assert len(emails) == 1  # sent before the workflow moved on, and not again by it
    email = emails[0].read_text()
    assert "Add method and PO" in email and "IB-POWER-CORD-14G-EU" in email
    assert "[Order DIST-MOCK-0003]" in email and "om-team@example.com" in email

    summary = run_pipeline_once()  # mock OM fixed it in Fusion
    assert summary["sync"]["updated"] == 1 and summary["reviewed"] == 1
    corrected = order_store.get_order("DIST-MOCK-0003")
    assert (corrected.ai_verdict, corrected.rework_count) == ("approve", 1)
    assert corrected.review_thread_id.startswith("DIST-MOCK-0003-")


def test_a_failed_email_to_om_changes_nothing(seeded_db, sample_backlog):
    run_pipeline_once()
    failure = MailNotSentError("The email could not be sent: 403.")
    with patch.object(send_back_to_om, "deliver_mail", side_effect=failure):
        with pytest.raises(ValueError, match="Nothing was changed"):
            submit_reviewer_decision(
                "DIST-MOCK-0003", "send_to_om", "Tester", "Add method and PO"
            )
    assert order_store.get_order("DIST-MOCK-0003").status == "WAITING_FOR_REVIEW"
    assert next_steps("DIST-MOCK-0003") == ("wait_for_reviewer",)
    submit_reviewer_decision(  # works once mail works again
        "DIST-MOCK-0003", "send_to_om", "Tester", "Add method and PO"
    )
    assert order_store.get_order("DIST-MOCK-0003").status == "SENT_TO_OM"


def test_reviewer_name_is_required(seeded_db, sample_backlog):
    run_pipeline_once()
    with pytest.raises(ValueError, match="name"):
        submit_reviewer_decision("REN-1103701", "approve", " ")


def test_orders_checked_while_the_ai_was_down_are_reviewed_again(
    seeded_db, sample_backlog, monkeypatch
):
    run_pipeline_once()  # AI is off in tests: fixed rules only
    waiting = order_store.get_order("REN-1103701")
    assert waiting.ai_used is False and "AI review not available" in waiting.ai_summary
    first_thread = waiting.review_thread_id

    reply = SimpleNamespace(
        content='{"verdict": "approve", "summary": "All good.", "reasons": []}'
    )
    for name, value in [("LITELLM_BASE_URL", "http://x"), ("LITELLM_API_KEY", "k")]:
        monkeypatch.setenv(name, value)
    monkeypatch.setenv("LITELLM_MODEL", "test-model")
    with patch("src.intelligence_layer.llm_client.ask_ai", return_value=reply):
        summary = run_pipeline_once()
    assert summary["reviewed"] == 14  # every order that had been checked without the AI
    again = order_store.get_order("REN-1103701")
    assert (again.ai_used, again.ai_summary) == (True, "All good.")
    assert again.review_thread_id != first_thread

    with patch("src.intelligence_layer.llm_client.ask_ai", return_value=reply):
        assert run_pipeline_once()["reviewed"] == 0  # nothing left to redo
    submit_reviewer_decision("REN-1103701", "approve", "Tester")  # still decidable
    assert order_store.get_order("REN-1103701").status == "APPROVED"
