import json
import os
import re
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import quote

import msal
import requests
from dotenv import load_dotenv

from src.knowledge_layer import attachment_store

GRAPH_BASE_URL = "https://graph.microsoft.com/v1.0"
GRAPH_SCOPE = "https://graph.microsoft.com/.default"
MAIL_METADATA_FILENAME = ".mail.json"
SPECIAL_INSTRUCTIONS_PATTERN = re.compile(
    r"\b(?:special|shipping)\s+instructions?\b\s*[:\-–—]?\s*",
    re.IGNORECASE,
)
MAIL_SECTION_BOUNDARY_PATTERN = re.compile(
    r"^(?:from|sent|to|cc|subject):|^-{2,}\s*original message\s*-{2,}$|"
    r"^(?:thanks|thank you|regards|best regards|kind regards|sincerely)[,!]?$",
    re.IGNORECASE,
)
SIGNATURE_TITLE_PATTERN = re.compile(
    r"\b(?:engineer|intern|manager|director|analyst|specialist|coordinator|"
    r"administrator|consultant|officer|president|lead|head)\b",
    re.IGNORECASE,
)


class _EmailBodyTextParser(HTMLParser):
    """Convert email HTML into line-oriented text for section extraction."""

    BLOCK_TAGS = {
        "br",
        "div",
        "p",
        "li",
        "table",
        "tr",
        "h1",
        "h2",
        "h3",
        "h4",
        "h5",
        "h6",
    }

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []

    def handle_starttag(self, tag, attrs):
        if tag.lower() in self.BLOCK_TAGS:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag.lower() in self.BLOCK_TAGS:
            self.parts.append("\n")

    def handle_data(self, data):
        self.parts.append(data)

    def text(self):
        return "".join(self.parts)


def extract_special_instructions(
    body: str, content_type: str, sender_email: str = ""
) -> str:
    """Return only the marked special/shipping-instruction email section."""
    if content_type.lower() == "html":
        parser = _EmailBodyTextParser()
        parser.feed(body)
        parser.close()
        text = parser.text()
    else:
        text = body
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    marker = SPECIAL_INSTRUCTIONS_PATTERN.search(text)
    if marker is None:
        return ""

    lines = text[marker.end():].splitlines()
    instructions = []
    for line in lines:
        cleaned = re.sub(r"[ \t]+", " ", line).strip()
        if MAIL_SECTION_BOUNDARY_PATTERN.match(cleaned):
            break
        if cleaned:
            instructions.append(cleaned)
        elif instructions and instructions[-1] != "":
            instructions.append("")
    while instructions and not instructions[-1]:
        instructions.pop()
    sender_email = sender_email.strip().lower()
    if sender_email:
        nonempty_indexes = [
            index for index, line in enumerate(instructions) if line
        ]
        if (
            len(nonempty_indexes) >= 3
            and instructions[nonempty_indexes[-1]].lower() == sender_email
            and SIGNATURE_TITLE_PATTERN.search(
                instructions[nonempty_indexes[-2]]
            )
        ):
            instructions = instructions[:nonempty_indexes[-3]]
            while instructions and not instructions[-1]:
                instructions.pop()
    return "\n".join(instructions).strip()


def acquire_access_token(tenant_id, client_id, client_secret):
    application = msal.ConfidentialClientApplication(
        client_id=client_id,
        authority=f"https://login.microsoftonline.com/{tenant_id}",
        client_credential=client_secret,
    )
    result = application.acquire_token_for_client(scopes=[GRAPH_SCOPE])
    access_token = result.get("access_token")
    if not access_token:
        details = result.get("error_description", "No access token was returned")
        raise RuntimeError(f"Microsoft Graph authentication failed: {details}")
    return access_token


class GraphAttachmentRetriever:
    def __init__(self, access_token, session=None):
        self.access_token = access_token
        self.session = session or requests.Session()

    def _headers(self, accept="application/json"):
        return {
            "Authorization": f"Bearer {self.access_token}",
            "Accept": accept,
        }

    def _iter_collection(self, url, params=None):
        while url:
            response = self.session.get(
                url,
                headers=self._headers(),
                params=params,
                timeout=30,
            )
            response.raise_for_status()
            data = response.json()
            yield from data.get("value", [])
            url = data.get("@odata.nextLink")
            params = None

    def _messages_from_sender(self, mailbox, sender_email):
        mailbox_path = quote(mailbox, safe="")
        url = f"{GRAPH_BASE_URL}/users/{mailbox_path}/messages"
        escaped_sender = sender_email.replace("'", "''")
        params = {
            "$filter": (
                f"from/emailAddress/address eq '{escaped_sender}' "
                "and hasAttachments eq true"
            ),
            "$select": "id,subject,from,receivedDateTime,hasAttachments,body",
            "$top": 50,
        }
        return self._iter_collection(url, params=params)

    def _attachments_for_message(self, mailbox, message_id):
        mailbox_path = quote(mailbox, safe="")
        message_path = quote(message_id, safe="")
        url = (
            f"{GRAPH_BASE_URL}/users/{mailbox_path}/messages/"
            f"{message_path}/attachments"
        )
        return self._iter_collection(url)

    @staticmethod
    def _safe_component(value):
        component = re.sub(r"[^A-Za-z0-9_-]", "_", value)
        return component or "item"

    @classmethod
    def _safe_filename(cls, name, attachment_id):
        basename = (name or "").replace("\\", "/").rsplit("/", 1)[-1]
        basename = re.sub(r'[\x00-\x1f<>:"|?*]', "_", basename).strip(" .")
        if not basename:
            basename = f"attachment-{cls._safe_component(attachment_id)}"
        return basename

    @classmethod
    def _is_inline_attachment(cls, attachment):
        """Identify body resources even when Graph omits the isInline flag."""
        if attachment.get("isInline"):
            return True
        if attachment.get("contentId") or attachment.get("contentLocation"):
            return True
        name = cls._safe_filename(attachment.get("name"), attachment.get("id", ""))
        stem = Path(name).stem.lower()
        suffix = Path(name).suffix.lower()
        return bool(
            suffix in {".png", ".jpg", ".jpeg", ".gif", ".bmp", ".webp"}
            and (
                re.fullmatch(r"image\d+", stem)
                or re.fullmatch(
                    r"img-[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-"
                    r"[0-9a-f]{4}-[0-9a-f]{12}",
                    stem,
                )
                or any(word in stem for word in ("logo", "icon", "signature"))
            )
        )

    @classmethod
    def _message_folder_name(cls, message_id, received_datetime):
        received_at = datetime.fromisoformat(
            received_datetime.replace("Z", "+00:00")
        ).astimezone(timezone.utc)
        timestamp = received_at.strftime("%Y%m%d_%H%M%S")
        message_component = cls._safe_component(message_id)[:80]
        return f"{timestamp}_{message_component}"

    @staticmethod
    def _write_mail_metadata(message_dir, message, sender_email):
        body = message.get("body") or {}
        actual_sender = str(
            ((message.get("from") or {}).get("emailAddress") or {}).get(
                "address"
            )
            or sender_email
        )
        special_instructions = extract_special_instructions(
            str(body.get("content") or ""),
            str(body.get("contentType") or "text"),
            actual_sender,
        )
        metadata_path = message_dir / MAIL_METADATA_FILENAME
        if not special_instructions:
            metadata_path.unlink(missing_ok=True)
            return
        metadata = {
            "message_id": message["id"],
            "subject": str(message.get("subject") or ""),
            "sender_email": actual_sender,
            "received_at": str(message.get("receivedDateTime") or ""),
            "body_content_type": "text",
            "body": special_instructions,
        }
        metadata_path.write_text(
            json.dumps(metadata, ensure_ascii=True, indent=2),
            encoding="utf-8",
        )

    def download_sender_attachments(
        self,
        mailbox,
        sender_email,
        output_dir,
        processed_message_ids=None,
        on_message_processed=None,
    ):
        """Save every attachment of each matching email in its own mail folder.

        One folder per email, named after the email's UTC arrival time, holds its
        real file attachments. Inline body resources such as signature logos and
        icons are ignored.

        Reference attachments are the only kind left out: they are links to files
        in OneDrive or SharePoint, so Microsoft Graph serves no content for them
        through the attachment `$value` endpoint and there is nothing to save.

        Args:
            mailbox: Mailbox address or user ID to search.
            sender_email: Only emails from this address are read.
            output_dir: Root folder the per-email folders are created in.
            processed_message_ids: Message IDs to skip because a prior check
                already handled them.
            on_message_processed: Optional callback invoked with each message
                after all of its attachments have been handled.

        Returns:
            The saved attachment paths, grouped by the folder of their email.
        """
        output_dir = Path(output_dir)
        downloaded = []
        processed_message_ids = set(processed_message_ids or ())

        for message in self._messages_from_sender(mailbox, sender_email):
            message_id = message["id"]
            if message_id in processed_message_ids:
                existing_dir = output_dir / self._message_folder_name(
                    message_id, message["receivedDateTime"]
                )
                if existing_dir.is_dir():
                    self._write_mail_metadata(
                        existing_dir, message, sender_email
                    )
                continue
            used_filenames = set()
            message_dir = None

            for attachment in self._attachments_for_message(mailbox, message_id):
                attachment_type = attachment.get("@odata.type", "")
                if attachment_type.endswith("referenceAttachment"):
                    continue
                if self._is_inline_attachment(attachment):
                    continue

                if message_dir is None:
                    message_dir = output_dir / self._message_folder_name(
                        message_id, message["receivedDateTime"]
                    )
                    message_dir.mkdir(parents=True, exist_ok=True)
                attachment_id = attachment["id"]
                attachment_path = quote(attachment_id, safe="")
                mailbox_path = quote(mailbox, safe="")
                message_path = quote(message_id, safe="")
                url = (
                    f"{GRAPH_BASE_URL}/users/{mailbox_path}/messages/"
                    f"{message_path}/attachments/{attachment_path}/$value"
                )
                response = self.session.get(
                    url,
                    headers=self._headers("application/octet-stream"),
                    stream=True,
                    timeout=60,
                )
                response.raise_for_status()

                filename = self._safe_filename(attachment.get("name"), attachment_id)
                if filename in used_filenames:
                    suffix = Path(filename).suffix
                    stem = filename[: -len(suffix)] if suffix else filename
                    filename = (
                        f"{stem}_{self._safe_component(attachment_id)}{suffix}"
                    )
                used_filenames.add(filename)
                destination = message_dir / filename
                content = bytearray()
                with destination.open("wb") as output_file:
                    for chunk in response.iter_content(chunk_size=1024 * 1024):
                        if chunk:
                            output_file.write(chunk)
                            content.extend(chunk)
                attachment_store.store_attachment(
                    message_id=message_id,
                    attachment_id=attachment_id,
                    mailbox=mailbox,
                    sender_email=sender_email,
                    folder_name=message_dir.name,
                    relative_path=destination.relative_to(output_dir).as_posix(),
                    file_name=filename,
                    content_type=attachment.get("contentType")
                    or "application/octet-stream",
                    content=bytes(content),
                    received_at=message["receivedDateTime"],
                )
                downloaded.append(destination)

            if message_dir is not None:
                self._write_mail_metadata(message_dir, message, sender_email)

            if on_message_processed is not None:
                on_message_processed(message)

        return downloaded


# --- TEMPORARY TEST HELPER: delete this block once verification is done. ---
def open_downloaded_attachments(paths):
    """Open downloaded files in the OS default application for manual checking."""
    import subprocess
    import sys

    opened = []
    for path in paths:
        path = Path(path)
        if not path.is_file():
            print(f"Skipped (not a file): {path}")
            continue
        try:
            if sys.platform == "darwin":
                subprocess.run(["open", str(path)], check=True)
            elif sys.platform == "win32":
                os.startfile(str(path))
            else:
                subprocess.run(["xdg-open", str(path)], check=True)
        except (OSError, subprocess.SubprocessError) as error:
            print(f"Could not open {path}: {error}")
            continue
        opened.append(path)
        print(f"Opened: {path}")
    return opened


# --- END TEMPORARY TEST HELPER ---


def load_configuration(environ=None):
    environ = os.environ if environ is None else environ
    required = (
        "GRAPH_TENANT_ID",
        "GRAPH_CLIENT_ID",
        "GRAPH_CLIENT_SECRET",
        "GRAPH_MAILBOX",
        "GRAPH_SENDER_EMAILS",
    )
    missing = [name for name in required if not environ.get(name)]
    if missing:
        raise ValueError("Missing required settings: " + ", ".join(missing))

    sender_emails = [
        address.strip()
        for address in environ["GRAPH_SENDER_EMAILS"].split(",")
        if address.strip()
    ]
    if not sender_emails:
        raise ValueError("GRAPH_SENDER_EMAILS must contain at least one email address")

    return {
        "tenant_id": environ["GRAPH_TENANT_ID"],
        "client_id": environ["GRAPH_CLIENT_ID"],
        "client_secret": environ["GRAPH_CLIENT_SECRET"],
        "mailbox": environ["GRAPH_MAILBOX"],
        "sender_emails": sender_emails,
        "output_dir": environ.get("ATTACHMENT_OUTPUT_DIR", "attachments"),
    }


def main():
    load_dotenv()
    config = load_configuration()
    access_token = acquire_access_token(
        config["tenant_id"],
        config["client_id"],
        config["client_secret"],
    )
    retriever = GraphAttachmentRetriever(access_token)

    for sender_email in config["sender_emails"]:
        output_dir = Path(config["output_dir"]).expanduser().resolve()
        downloaded = retriever.download_sender_attachments(
            config["mailbox"],
            sender_email,
            output_dir,
        )
        print(
            f"{sender_email}: downloaded {len(downloaded)} attachment(s) "
            f"to {output_dir}"
        )
        for path in downloaded:
            print(f"  - {path}")

        # TEMPORARY: open attachments for manual verification. Delete with helper.
        if os.environ.get("OPEN_ATTACHMENTS") == "1":
            open_downloaded_attachments(downloaded)


if __name__ == "__main__":
    main()
