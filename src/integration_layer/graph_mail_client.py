"""Reads and sends mail through Microsoft Graph, using the app registration in .env.

The service signs in as itself (app-only, OAuth client credentials) with
GRAPH_API_TENANT_ID, GRAPH_API_CLIENT_ID and GRAPH_API_CLIENT_SECRET, and only touches
the mailboxes it is given. Secrets, tokens and mail bodies are never logged.
"""

import time
from typing import Any, Dict, List, Optional

import requests

from src.utils.config import Config, load_config
from src.utils.logging_config import get_logger

logger = get_logger(__name__)

GRAPH_BASE_URL = "https://graph.microsoft.com/v1.0"
TOKEN_URL = "https://login.microsoftonline.com/{tenant}/oauth2/v2.0/token"
GRAPH_SCOPE = "https://graph.microsoft.com/.default"
MAX_ATTEMPTS = 3
# Renew the token this many seconds before Microsoft says it expires.
TOKEN_EXPIRY_MARGIN_SECONDS = 120

_cached_token: Dict[str, Any] = {}


class GraphError(Exception):
    """A Graph call failed; the message says why in plain words."""

    def __init__(self, status_code: Optional[int], message: str):
        """Keep the HTTP status next to the message.

        Args:
            status_code: HTTP status from Graph, or None for a network failure.
            message: What went wrong.

        Returns:
            None.
        """
        super().__init__(message)
        self.status_code = status_code


def check_graph_settings(config: Config) -> None:
    """Make sure every Graph setting is present.

    Args:
        config: The loaded settings.

    Returns:
        None. Raises GraphError naming the missing settings.
    """
    missing = [
        name
        for name, value in [
            ("GRAPH_API_TENANT_ID", config.graph_api_tenant_id),
            ("GRAPH_API_CLIENT_ID", config.graph_api_client_id),
            ("GRAPH_API_CLIENT_SECRET", config.graph_api_client_secret),
            ("GRAPH_API_MAILBOX", config.graph_api_mailbox),
        ]
        if not value
    ]
    if missing:
        raise GraphError(None, f"Mail settings missing in .env: {', '.join(missing)}.")


def get_access_token(force_refresh: bool = False) -> str:
    """Get an app-only Graph token, reusing the cached one until shortly before expiry.

    Args:
        force_refresh: Ignore the cached token (used after a 401).

    Returns:
        The access token.
    """
    config = load_config()
    check_graph_settings(config)
    cache_key = f"{config.graph_api_tenant_id}/{config.graph_api_client_id}"
    cached = _cached_token.get(cache_key)
    if cached and not force_refresh and cached["expires_at"] > time.time():
        return cached["token"]
    try:
        response = requests.post(
            TOKEN_URL.format(tenant=config.graph_api_tenant_id),
            data={
                "grant_type": "client_credentials",
                "client_id": config.graph_api_client_id,
                "client_secret": config.graph_api_client_secret,
                "scope": GRAPH_SCOPE,
            },
            timeout=30,
        )
    except requests.RequestException as error:
        raise GraphError(None, f"Could not reach Microsoft sign-in: {error}") from error
    if response.status_code != 200:
        raise GraphError(
            response.status_code,
            "Microsoft sign-in refused the app (check GRAPH_API_* in .env): "
            + describe_graph_error(response),
        )
    body = response.json()
    _cached_token[cache_key] = {
        "token": body["access_token"],
        "expires_at": time.time()
        + int(body.get("expires_in", 3600))
        - TOKEN_EXPIRY_MARGIN_SECONDS,
    }
    return body["access_token"]


def describe_graph_error(response: requests.Response) -> str:
    """Pull the readable error out of a failed Graph response.

    Args:
        response: The failed response.

    Returns:
        Graph's error code and message, or the HTTP status if there is no JSON error.
    """
    try:
        body = response.json()
    except ValueError:
        return f"HTTP {response.status_code}"
    error = body.get("error")
    if isinstance(error, dict):
        return f"{error.get('code', '')}: {error.get('message', '')}".strip(": ")
    return str(body.get("error_description") or error or f"HTTP {response.status_code}")


def call_graph(
    method: str,
    path: str,
    params: Optional[Dict[str, Any]] = None,
    json_body: Any = None,
) -> requests.Response:
    """Make one Graph call, retrying throttled or failed calls and refreshing an expired token.

    Args:
        method: HTTP method.
        path: Path after /v1.0, e.g. "/users/a@b.com/messages".
        params: Optional query parameters.
        json_body: Optional JSON body.

    Returns:
        The successful response. Raises GraphError when it keeps failing.
    """
    refreshed = False
    for attempt in range(1, MAX_ATTEMPTS + 1):
        headers = {"Authorization": f"Bearer {get_access_token(refreshed)}"}
        try:
            response = requests.request(
                method,
                f"{GRAPH_BASE_URL}{path}",
                headers=headers,
                params=params,
                json=json_body,
                timeout=60,
            )
        except requests.RequestException as error:
            if attempt == MAX_ATTEMPTS:
                raise GraphError(None, f"Could not reach Microsoft Graph: {error}")
            time.sleep(2**attempt)
            continue
        if response.status_code < 400:
            return response
        if response.status_code == 401 and not refreshed:
            refreshed = True
            continue
        if response.status_code in (429, 500, 502, 503, 504) and attempt < MAX_ATTEMPTS:
            wait = response.headers.get("Retry-After", "")
            time.sleep(int(wait) if wait.isdigit() else 2**attempt)
            continue
        raise GraphError(response.status_code, describe_graph_error(response))
    raise GraphError(None, "Microsoft Graph kept failing.")


def list_recent_messages(mailbox: str, folder: str, since_iso: str) -> List[Dict]:
    """List messages received in a folder since a time, newest first.

    Only the receive time is filtered by Graph; sender and subject are matched by the
    caller, because Graph rejects some filter and sort combinations on other fields.

    Args:
        mailbox: Mailbox address, e.g. "mfgopstest@infoblox.com".
        folder: Folder id or well-known name, e.g. "inbox".
        since_iso: ISO-8601 UTC time, e.g. "2026-10-19T00:00:00Z".

    Returns:
        Message summaries (id, subject, from, receivedDateTime, hasAttachments,
        internetMessageId).
    """
    path = f"/users/{mailbox}/mailFolders/{folder}/messages"
    params: Optional[Dict[str, Any]] = {
        "$filter": f"receivedDateTime ge {since_iso}",
        "$orderby": "receivedDateTime desc",
        "$top": "50",
        "$select": "id,subject,from,receivedDateTime,hasAttachments,internetMessageId",
    }
    messages: List[Dict] = []
    while path:
        body = call_graph("GET", path, params=params).json()
        messages += body.get("value", [])
        next_link = body.get("@odata.nextLink")
        if not next_link or len(messages) >= 200:
            break
        path, params = next_link.replace(GRAPH_BASE_URL, ""), None
    return messages


def list_attachments(mailbox: str, message_id: str) -> List[Dict]:
    """List a message's attachments (names and sizes, not the content).

    Args:
        mailbox: Mailbox address.
        message_id: Graph message id.

    Returns:
        Attachment summaries (id, name, contentType, size, isInline).
    """
    body = call_graph(
        "GET",
        f"/users/{mailbox}/messages/{message_id}/attachments",
        params={"$select": "id,name,contentType,size,isInline"},
    ).json()
    return body.get("value", [])


def download_attachment(mailbox: str, message_id: str, attachment_id: str) -> bytes:
    """Download one attachment's raw bytes.

    Args:
        mailbox: Mailbox address.
        message_id: Graph message id.
        attachment_id: Graph attachment id.

    Returns:
        The file content.
    """
    return call_graph(
        "GET",
        f"/users/{mailbox}/messages/{message_id}/attachments/{attachment_id}/$value",
    ).content


def send_html_mail(sender: str, to_address: str, subject: str, html_body: str) -> Dict:
    """Send an HTML mail: save it as a draft first, then send it.

    Saving a draft first gives back the message and conversation ids (sendMail returns
    none), so replies to it can be matched later.

    Args:
        sender: Mailbox the mail is sent from.
        to_address: Recipient address (one, or several separated by ";" or ",").
        subject: Subject line.
        html_body: Body as HTML.

    Returns:
        {"message_id": ..., "conversation_id": ..., "internet_message_id": ...}.
    """
    recipients = [
        {"emailAddress": {"address": address.strip()}}
        for address in to_address.replace(",", ";").split(";")
        if address.strip()
    ]
    draft = call_graph(
        "POST",
        f"/users/{sender}/messages",
        json_body={
            "subject": subject,
            "body": {"contentType": "HTML", "content": html_body},
            "toRecipients": recipients,
        },
    ).json()
    call_graph("POST", f"/users/{sender}/messages/{draft['id']}/send")
    logger.info("Mail sent from %s (subject: %s)", sender, subject)
    return {
        "message_id": draft["id"],
        "conversation_id": draft.get("conversationId", ""),
        "internet_message_id": draft.get("internetMessageId", ""),
    }
