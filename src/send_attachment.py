import argparse
import base64
import mimetypes
import os
from pathlib import Path
from urllib.parse import quote

import requests
from dotenv import load_dotenv

from src.graph_attachments import GRAPH_BASE_URL, acquire_access_token

MAX_SIMPLE_ATTACHMENT_SIZE = 3 * 1024 * 1024
MAX_ATTACHMENT_SIZE = 150 * 1024 * 1024
UPLOAD_CHUNK_SIZE = 10 * 320 * 1024


def load_send_configuration(environ=None):
    environ = os.environ if environ is None else environ
    required = (
        "GRAPH_TENANT_ID",
        "GRAPH_CLIENT_ID",
        "GRAPH_CLIENT_SECRET",
        "GRAPH_MAILBOX",
    )
    missing = [name for name in required if not environ.get(name)]
    if missing:
        raise ValueError("Missing required settings: " + ", ".join(missing))

    return {
        "tenant_id": environ["GRAPH_TENANT_ID"],
        "client_id": environ["GRAPH_CLIENT_ID"],
        "client_secret": environ["GRAPH_CLIENT_SECRET"],
        "mailbox": environ["GRAPH_MAILBOX"],
    }


def prompt_for_comments():
    print("Enter comments. Finish with a line containing only a period (.).")
    lines = []
    while True:
        try:
            line = input()
        except EOFError:
            break
        if line == ".":
            break
        lines.append(line)

    comments = "\n".join(lines).strip()
    if not comments:
        raise ValueError("Comments cannot be empty")
    return comments


def _attachment_message(subject, comments, recipient, body_content_type="Text"):
    return {
        "subject": subject,
        "body": {
            "contentType": body_content_type,
            "content": comments,
        },
        "toRecipients": [
            {
                "emailAddress": {
                    "address": recipient,
                }
            }
        ],
    }


def _file_attachment_payload(attachment_path, content_type):
    return {
        "@odata.type": "#microsoft.graph.fileAttachment",
        "name": attachment_path.name,
        "contentType": content_type,
        "contentBytes": base64.b64encode(attachment_path.read_bytes()).decode("ascii"),
    }


def _send_in_one_request(session, headers, mailbox_path, message, attachments):
    payload = {
        "message": {
            **message,
            "attachments": [
                _file_attachment_payload(path, content_type)
                for path, _, content_type in attachments
            ],
        },
        "saveToSentItems": True,
    }
    response = session.post(
        f"{GRAPH_BASE_URL}/users/{mailbox_path}/sendMail",
        headers=headers,
        json=payload,
        timeout=60,
    )
    response.raise_for_status()


def _upload_attachment_chunks(session, upload_url, attachment_path, size):
    with attachment_path.open("rb") as source:
        start = 0
        while start < size:
            chunk = source.read(UPLOAD_CHUNK_SIZE)
            if not chunk:
                raise OSError(f"Attachment ended early: {attachment_path}")
            end = start + len(chunk) - 1
            # The upload URL carries its own token, so no Authorization header.
            response = session.put(
                upload_url,
                headers={
                    "Content-Type": "application/octet-stream",
                    "Content-Range": f"bytes {start}-{end}/{size}",
                },
                data=chunk,
                timeout=300,
            )
            response.raise_for_status()
            start = end + 1


def _send_with_upload_sessions(
    session, headers, mailbox_path, message, attachments
):
    draft = session.post(
        f"{GRAPH_BASE_URL}/users/{mailbox_path}/messages",
        headers=headers,
        json=message,
        timeout=60,
    )
    draft.raise_for_status()
    message_path = quote(draft.json()["id"], safe="")
    draft_url = f"{GRAPH_BASE_URL}/users/{mailbox_path}/messages/{message_path}"
    try:
        for attachment_path, size, content_type in attachments:
            if size < MAX_SIMPLE_ATTACHMENT_SIZE:
                attached = session.post(
                    f"{draft_url}/attachments",
                    headers=headers,
                    json=_file_attachment_payload(attachment_path, content_type),
                    timeout=60,
                )
                attached.raise_for_status()
                continue
            upload_session = session.post(
                f"{draft_url}/attachments/createUploadSession",
                headers=headers,
                json={
                    "AttachmentItem": {
                        "attachmentType": "file",
                        "name": attachment_path.name,
                        "contentType": content_type,
                        "size": size,
                    }
                },
                timeout=60,
            )
            upload_session.raise_for_status()
            _upload_attachment_chunks(
                session,
                upload_session.json()["uploadUrl"],
                attachment_path,
                size,
            )
        sent = session.post(f"{draft_url}/send", headers=headers, timeout=120)
        sent.raise_for_status()
    except Exception:
        # Do not leave a half-built draft behind in the mailbox.
        session.delete(draft_url, headers=headers, timeout=60)
        raise


def send_attachments(
    access_token,
    mailbox,
    recipient,
    subject,
    comments,
    attachment_paths,
    session=None,
    body_content_type="Text",
):
    paths = [Path(path).expanduser().resolve() for path in attachment_paths]
    if not paths:
        raise ValueError("At least one attachment is required")
    missing = next((path for path in paths if not path.is_file()), None)
    if missing is not None:
        raise FileNotFoundError(f"Attachment does not exist: {missing}")
    attachments = [
        (
            path,
            path.stat().st_size,
            mimetypes.guess_type(path.name)[0] or "application/octet-stream",
        )
        for path in paths
    ]
    total_size = sum(size for _, size, _ in attachments)
    if any(size > MAX_ATTACHMENT_SIZE for _, size, _ in attachments):
        largest = max(size for _, size, _ in attachments)
        limit_mb = MAX_ATTACHMENT_SIZE // (1024 * 1024)
        raise ValueError(
            f"Attachment is {largest / (1024 * 1024):.1f} MB; Microsoft Graph "
            f"accepts at most {limit_mb} MB"
        )
    if total_size > MAX_ATTACHMENT_SIZE:
        limit_mb = MAX_ATTACHMENT_SIZE // (1024 * 1024)
        raise ValueError(
            f"Attachments total {total_size / (1024 * 1024):.1f} MB; "
            f"Microsoft Graph accepts at most {limit_mb} MB"
        )
    if not recipient.strip():
        raise ValueError("Recipient cannot be empty")
    if not subject.strip():
        raise ValueError("Subject cannot be empty")
    if not comments.strip():
        raise ValueError("Comments cannot be empty")
    if body_content_type.lower() not in {"text", "html"}:
        raise ValueError("Body content type must be Text or HTML")

    graph_body_type = (
        "HTML" if body_content_type.lower() == "html" else "Text"
    )
    message = _attachment_message(
        subject, comments, recipient, graph_body_type
    )
    mailbox_path = quote(mailbox, safe="")
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json",
    }
    session = session or requests.Session()
    if total_size < MAX_SIMPLE_ATTACHMENT_SIZE:
        _send_in_one_request(session, headers, mailbox_path, message, attachments)
    else:
        _send_with_upload_sessions(
            session, headers, mailbox_path, message, attachments
        )
    return paths


def send_attachment(
    access_token,
    mailbox,
    recipient,
    subject,
    comments,
    attachment_path,
    session=None,
):
    return send_attachments(
        access_token,
        mailbox,
        recipient,
        subject,
        comments,
        [attachment_path],
        session=session,
    )[0]


def build_parser():
    parser = argparse.ArgumentParser(
        description="Send one attachment and interactive comments by email."
    )
    parser.add_argument("--recipient", required=True, help="Recipient email address")
    parser.add_argument("--attachment", required=True, help="Path to the attachment")
    parser.add_argument("--subject", required=True, help="Email subject")
    return parser


def main():
    parser = build_parser()
    args = parser.parse_args()
    load_dotenv()

    try:
        config = load_send_configuration()
        comments = prompt_for_comments()
        access_token = acquire_access_token(
            config["tenant_id"],
            config["client_id"],
            config["client_secret"],
        )
        sent_path = send_attachment(
            access_token,
            config["mailbox"],
            args.recipient,
            args.subject,
            comments,
            args.attachment,
        )
    except (FileNotFoundError, RuntimeError, ValueError, requests.RequestException) as error:
        parser.exit(1, f"Error: {error}\n")

    print(
        f"Microsoft Graph accepted the email to {args.recipient} "
        f"with attachment {sent_path.name}."
    )


if __name__ == "__main__":
    main()
