from dataclasses import replace
from unittest.mock import patch

import pytest

from src.integration_layer import outbound_mail
from src.integration_layer.graph_mail_client import GraphError
from src.integration_layer.outbound_mail import MailNotSentError
from src.integration_layer.send_back_to_om import build_om_email, email_om_team
from src.integration_layer.send_order_mail_to_arrow import build_arrow_email
from src.knowledge_layer import mail_store
from src.utils.models import RuleFinding
from tests.sample_orders import get_sample_orders

FINDING = RuleFinding(1, "Too few power cords", "reject", "Needs 4 cords, has 2.")


def dubai_order():
    return next(o for o in get_sample_orders() if o.order_no == "DIST-MOCK-0010")


def test_om_email_has_the_details_and_escapes_fusion_text():
    order = replace(
        dubai_order(), ai_reasons=["Check cords."], shipping_instructions="<b>DHL</b>"
    )
    subject, body = build_om_email(order, [FINDING], "Raj", "Add 2 UK cords")
    assert subject.startswith("[Order DIST-MOCK-0010]")
    for text in [
        "Add 2 UK cords",
        "Too few power cords: Needs 4 cords, has 2.",
        "Check cords.",
        "Collect",  # freight terms
        "EXCLUSIVE NETWORKS ME",  # bill to
        "IB-POWER-CORD-14G-UK",
        "Raj",
    ]:
        assert text in body, text
    assert "&lt;b&gt;DHL&lt;/b&gt;" in body and "<b>DHL</b>" not in body


def test_arrow_email_never_mentions_the_customer_po():
    order = replace(dubai_order(), customer_po="CUSTOMER-PO-123")
    subject, body = build_arrow_email(order, "USPO45773")
    assert "USPO45773" in subject and "PARTIAL SHIPMENT NOT ALLOWED" in body
    assert "CUSTOMER-PO-123" not in body


def test_the_same_correction_is_mailed_once(seeded_db, isolated_environment):
    order = dubai_order()
    email_om_team(order, [], "Raj", "Fix it")
    email_om_team(order, [], "Raj", "Fix it")
    assert len(list((isolated_environment / "outbox").iterdir())) == 1
    email_om_team(replace(order, rework_count=1), [], "Raj", "Fix it again")
    assert len(list((isolated_environment / "outbox").iterdir())) == 2


def test_redirect_sends_everything_to_the_tester(
    seeded_db, isolated_environment, monkeypatch
):
    monkeypatch.setenv("EMAIL_REDIRECT_TO", "rvaghela@example.com")
    email_om_team(dubai_order(), [], "Raj", "Fix it")
    mail = next((isolated_environment / "outbox").iterdir()).read_text()
    assert "To: rvaghela@example.com" in mail
    assert "meant for <b>om-team@example.com</b>" in mail
    assert mail_store.get_recent_outbound()[0]["recipient"] == "rvaghela@example.com"


def test_live_mode_sends_through_graph(seeded_db, monkeypatch):
    monkeypatch.setenv("EMAIL_MODE", "live")
    monkeypatch.setenv("GRAPH_API_MAILBOX", "mfgopstest@example.com")
    sent = {"message_id": "g1", "conversation_id": "c1", "internet_message_id": "i1"}
    graph = outbound_mail.graph_mail_client
    with patch.object(graph, "send_html_mail", return_value=sent) as send:
        assert email_om_team(dubai_order(), [], "Raj", "Fix it") == "g1"
        assert email_om_team(dubai_order(), [], "Raj", "Fix it") == "g1"  # not twice
    assert send.call_count == 1
    sender, recipient, subject, _ = send.call_args.args
    assert (sender, recipient) == ("mfgopstest@example.com", "om-team@example.com")
    row = mail_store.get_recent_outbound()[0]
    assert (row["status"], row["conversation_id"]) == ("sent", "c1")


def test_a_graph_failure_is_reported_and_logged(seeded_db, monkeypatch):
    monkeypatch.setenv("EMAIL_MODE", "live")
    monkeypatch.setenv("GRAPH_API_MAILBOX", "mfgopstest@example.com")
    graph = outbound_mail.graph_mail_client
    with patch.object(graph, "send_html_mail", side_effect=GraphError(403, "Denied")):
        with pytest.raises(MailNotSentError, match="Denied"):
            email_om_team(dubai_order(), [], "Raj", "Fix it")
    assert mail_store.get_recent_outbound()[0]["status"] == "failed"


def test_no_recipient_is_explained(seeded_db, monkeypatch):
    monkeypatch.setenv("OM_TEAM_EMAIL", "")
    with pytest.raises(MailNotSentError, match="OM_TEAM_EMAIL"):
        email_om_team(dubai_order(), [], "Raj", "Fix it")
