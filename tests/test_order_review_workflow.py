from types import SimpleNamespace
from unittest.mock import patch

import pytest

from src.app import run_pipeline_once
from src.intelligence_layer.order_review_workflow import (
    get_workflow,
    submit_reviewer_decision,
)
from src.knowledge_layer import learning_request_store, order_store
from src.utils.models import OrderStatus

# The AI is off in tests, so verdicts come from the fixed checks only.


def all_orders():
    return {row["order_no"]: row for row in order_store.get_all_orders()}


def test_reviewed_orders_wait_without_running(seeded_db):
    run_pipeline_once()
    order = order_store.get_order("REN-1103701")
    assert order.status == OrderStatus.WAITING_FOR_REVIEW.value
    assert order.ai_verdict == "approve"
    snapshot = get_workflow().get_state(
        {"configurable": {"thread_id": order.review_thread_id}}
    )
    assert snapshot.next == ("wait_for_reviewer",)


def test_approve_releases_and_keeps_the_order_in_all_orders(seeded_db):
    run_pipeline_once()
    submit_reviewer_decision("REN-1103701", "approve", "Tester")
    assert order_store.get_order("REN-1103701") is None
    record = all_orders()["REN-1103701"]
    assert record["status"] == OrderStatus.RELEASED.value
    assert record["agreed"] == 1
    assert record["release_request_id"] == "MOCK-ESS-REN-1103701"
    assert learning_request_store.get_open_learning_requests() == []
    with pytest.raises(ValueError, match="isn't waiting"):
        submit_reviewer_decision(
            "REN-1103701", "approve", "Tester"
        )  # no double release


def test_embargoed_order_can_never_be_released(seeded_db):
    run_pipeline_once()
    with pytest.raises(ValueError, match="never be released"):
        submit_reviewer_decision(
            "DIST-MOCK-0005", "approve", "Tester", "Please release"
        )
    assert (
        order_store.get_order("DIST-MOCK-0005").status
        == OrderStatus.WAITING_FOR_REVIEW.value
    )


def test_eus_hold_can_be_approved_with_a_comment(seeded_db):
    run_pipeline_once()
    assert order_store.get_order("DIST-MOCK-0004").status == OrderStatus.ON_HOLD.value
    with pytest.raises(ValueError, match="comment"):
        submit_reviewer_decision("DIST-MOCK-0004", "approve", "Tester")
    submit_reviewer_decision("DIST-MOCK-0004", "hold", "Tester")
    submit_reviewer_decision(
        "DIST-MOCK-0004", "approve", "Tester", "EUS sent to Arrow by email"
    )
    assert all_orders()["DIST-MOCK-0004"]["status"] == OrderStatus.RELEASED.value
    assert (
        learning_request_store.get_open_learning_requests() == []
    )  # a hold isn't a "no"


def test_disagreement_asks_the_ai_to_learn(seeded_db):
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
    seeded_db, isolated_environment
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
    email = next((isolated_environment / "outbox").iterdir()).read_text()
    assert "Add method and PO" in email and "IB-POWER-CORD-14G-EU" in email

    summary = run_pipeline_once()  # mock OM fixed it in Fusion
    assert summary["sync"]["updated"] == 1 and summary["reviewed"] == 1
    corrected = order_store.get_order("DIST-MOCK-0003")
    assert (corrected.ai_verdict, corrected.rework_count) == ("approve", 1)
    assert corrected.review_thread_id.startswith("DIST-MOCK-0003-")


def test_reviewer_name_is_required(seeded_db):
    run_pipeline_once()
    with pytest.raises(ValueError, match="name"):
        submit_reviewer_decision("REN-1103701", "approve", " ")


def test_orders_checked_while_the_ai_was_down_are_reviewed_again(
    seeded_db, monkeypatch
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
    assert order_store.get_order("REN-1103701") is None
