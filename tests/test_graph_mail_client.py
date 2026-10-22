from unittest.mock import MagicMock, patch

import pytest

from src.integration_layer import graph_mail_client
from src.integration_layer.graph_mail_client import GraphError


@pytest.fixture(autouse=True)
def graph_settings(monkeypatch):
    for name, value in [
        ("GRAPH_API_TENANT_ID", "tenant"),
        ("GRAPH_API_CLIENT_ID", "client"),
        ("GRAPH_API_CLIENT_SECRET", "secret"),
        ("GRAPH_API_MAILBOX", "mfgopstest@example.com"),
    ]:
        monkeypatch.setenv(name, value)
    graph_mail_client._cached_token.clear()
    monkeypatch.setattr(graph_mail_client.time, "sleep", lambda seconds: None)


def response(status, body=None, headers=None, content=b""):
    reply = MagicMock(status_code=status, headers=headers or {}, content=content)
    reply.json.return_value = body if body is not None else {}
    return reply


TOKEN = response(200, {"access_token": "t1", "expires_in": 3600})


def test_missing_settings_are_named(monkeypatch):
    monkeypatch.setenv("GRAPH_API_CLIENT_SECRET", "")
    with pytest.raises(GraphError, match="GRAPH_API_CLIENT_SECRET"):
        graph_mail_client.get_access_token()


def test_token_is_cached_until_it_expires():
    with patch.object(graph_mail_client.requests, "post", return_value=TOKEN) as post:
        assert graph_mail_client.get_access_token() == "t1"
        assert graph_mail_client.get_access_token() == "t1"
    assert post.call_count == 1
    sent = post.call_args.kwargs["data"]
    assert sent["grant_type"] == "client_credentials"
    assert sent["scope"] == "https://graph.microsoft.com/.default"


def test_refused_sign_in_is_explained():
    refused = response(401, {"error_description": "AADSTS7000215: Invalid secret."})
    with patch.object(graph_mail_client.requests, "post", return_value=refused):
        with pytest.raises(GraphError, match="Invalid secret"):
            graph_mail_client.get_access_token()


def test_throttled_call_is_retried_and_401_refreshes_the_token():
    throttled = response(429, headers={"Retry-After": "1"})
    expired = response(401, {"error": {"code": "InvalidAuthenticationToken"}})
    ok = response(200, {"value": [{"id": "m1"}]})
    with patch.object(graph_mail_client.requests, "post", return_value=TOKEN) as post:
        with patch.object(
            graph_mail_client.requests, "request", side_effect=[throttled, expired, ok]
        ):
            messages = graph_mail_client.list_recent_messages(
                "mfgopstest@example.com", "inbox", "2026-10-19T00:00:00Z"
            )
    assert messages == [{"id": "m1"}]
    assert post.call_count == 2  # token fetched again after the 401


def test_graph_errors_carry_graphs_message():
    denied = response(403, {"error": {"code": "ErrorAccessDenied", "message": "No."}})
    with patch.object(graph_mail_client.requests, "post", return_value=TOKEN):
        with patch.object(graph_mail_client.requests, "request", return_value=denied):
            with pytest.raises(GraphError, match="ErrorAccessDenied: No.") as error:
                graph_mail_client.list_attachments("box@example.com", "m1")
    assert error.value.status_code == 403


def test_send_saves_a_draft_then_sends_it():
    draft = response(
        201, {"id": "d1", "conversationId": "c1", "internetMessageId": "i1"}
    )
    with patch.object(graph_mail_client.requests, "post", return_value=TOKEN):
        with patch.object(
            graph_mail_client.requests, "request", side_effect=[draft, response(202)]
        ) as request:
            sent = graph_mail_client.send_html_mail(
                "mfgopstest@example.com",
                "a@example.com; b@example.com",
                "Hi",
                "<p>x</p>",
            )
    assert sent == {
        "message_id": "d1",
        "conversation_id": "c1",
        "internet_message_id": "i1",
    }
    create, send = request.call_args_list
    assert create.args[1].endswith("/users/mfgopstest@example.com/messages")
    assert [
        r["emailAddress"]["address"] for r in create.kwargs["json"]["toRecipients"]
    ] == [
        "a@example.com",
        "b@example.com",
    ]
    assert create.kwargs["json"]["body"]["contentType"] == "HTML"
    assert send.args[1].endswith("/messages/d1/send")
