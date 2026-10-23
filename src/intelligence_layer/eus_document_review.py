"""Automated checks for an EUS, its quote, and an optional customer PO."""

import base64
import json
import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

import fitz
import requests
from pyhanko.pdf_utils.misc import PdfReadError
from pyhanko.pdf_utils.reader import PdfFileReader
from pyhanko.sign.validation import validate_pdf_signature
from pyhanko.sign.validation.errors import SignatureValidationError
from pypdf import PdfReader
from pypdf.errors import PdfReadError as PyPdfReadError

from src.intelligence_layer import llm_client

MAX_PAGE_CHARACTERS = 12000
MAX_IMAGE_PAGES = 8
MIN_CONTAINED_IDENTIFIER_LENGTH = 6
EMAIL_PATTERN = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
SENIOR_TITLE_PATTERN = re.compile(
    r"\b("
    r"(?:senior\s+)?manager|director|(?:vice\s+president|vp)|head|president|"
    r"owner|partner|officer|chief|ceo|cfo|coo|cto|cio|cmo"
    r")\b",
    re.IGNORECASE,
)
JUNIOR_MANAGER_TITLE_PATTERN = re.compile(
    r"\b(?:assistant|associate|deputy)\s+manager\b", re.IGNORECASE
)

EXTRACTION_SYSTEM_PROMPT = """You extract facts from sales document text and page images
for a strict, evidence-based EUS review. Never infer a value that is absent or illegible.
Use null for unknown scalar values and [] for unknown line items.

Classify each document as "eus", "quote", "po", or "other". For the EUS, extract order
details wherever they appear; do not rely on a fixed page number.

Every page is supplied as an image, sometimes with an extracted or OCR text layer.
The text layer may be garbled and never shows checkbox state or tables drawn as
images, so always read checkboxes, tables, and handwriting from the page image.

Question 6 asks whether the product will be used for weapons, nuclear, missile, or
UAV purposes. It is answered with two boxes printed as "No: [ ] Yes: [ ]". The box
immediately after the word "No" belongs to No; the box immediately after the word
"Yes" belongs to Yes. A tick, cross, or fill in the No box means No is selected.
Set question_6_no_selected to true when the No box is marked and the Yes box is
empty, false when the Yes box is marked, and null only when neither is legible.
Put the exact visual evidence in question_6_evidence, for example "No box ticked,
Yes box empty".

Also extract the signer title, signer email, document date, and signature date exactly
as printed. Set signature_present to true only when a handwritten signature, signature
image, stamp with signature, or electronic signature mark is visibly present. A typed
name or title alone is not a signature. For the EUS, also extract the end user (end
customer) organisation name, its full postal address, and its website. Write the
website as a clean URL without spaces, correcting obvious OCR artifacts.

Line items: extract every product line, including lines in tables drawn as images.
"part" is the Infoblox/manufacturer part number (e.g. "TE-1506-HW-AC") and
"partner_sku" is the partner/reseller SKU column value. Copy each code exactly as
printed but without surrounding customer names, quote numbers, or descriptions; for
example "#49045 UKRSIBBANK-TE-1506-HW-AC Trinzic X6 1506" has part "TE-1506-HW-AC".
Use null for quantity when no quantity is printed. In the EUS, the products are
usually listed next to the "Purchase Order/Quote Number, Product Name and Part
Number" field, together with the quote number.

Reply with only this JSON shape:
{"documents":[{"file_name":"exact input file name","document_type":"eus|quote|po|other",
"quote_number":null,"lines":[{"quantity":null,"part":null,"partner_sku":null}],
"question_6_no_selected":null,"question_6_evidence":null,
"signer_title":null,"signer_email":null,
"document_date":null,"signature_date":null,"signature_present":null,
"end_user_name":null,"end_user_address":null,"end_user_website":null}]}"""

LEGITIMACY_SYSTEM_PROMPT = """You assess whether an end user named on an End-Use
Statement appears to be a real, legitimate organisation. Use only the supplied name,
address, and website plus your general knowledge. Consider whether the website domain
plausibly belongs to the named organisation (or its parent/brand), whether the address
is a complete and plausible business address, and whether anything looks like a
placeholder, free-mail domain, or mismatch. Reply with only this JSON:
{"legitimate": true|false, "reason": "one short sentence"}"""

REPEAT_CUSTOMER_CONFIDENCE = 0.7
WEBSITE_TIMEOUT_SECONDS = 10
# Bot protection often answers real sites with these codes; the server still exists.
WEBSITE_RESTRICTED_STATUSES = {401, 403, 405, 429}


class EusReviewError(Exception):
    """Raised when the documents cannot be extracted or reviewed."""


@dataclass
class EusCheck:
    """One rule result shown to the EUS reviewer."""

    code: str
    label: str
    passed: bool
    details: str


@dataclass
class EusReview:
    """All automated checks for one email's document set."""

    checks: List[EusCheck]
    document_types: Dict[str, str]
    end_user_name: Optional[str] = None
    repeat_customer: bool = False

    @property
    def confidence(self) -> float:
        """Return the share of automated checks that passed."""
        if not self.checks:
            return 0.0
        return sum(check.passed for check in self.checks) / len(self.checks)

    @property
    def passed(self) -> bool:
        """Repeat customers need 70% confidence; new customers need every check."""
        if not self.checks:
            return False
        if self.repeat_customer:
            return self.confidence >= REPEAT_CUSTOMER_CONFIDENCE
        return all(check.passed for check in self.checks)


def extract_pdf_pages(path: Path) -> List[str]:
    """Extract available text from every PDF page."""
    try:
        reader = PdfReader(path)
        pages = [
            (page.extract_text() or "").strip()[:MAX_PAGE_CHARACTERS]
            for page in reader.pages
        ]
    except (OSError, PyPdfReadError) as error:
        raise EusReviewError(f"Could not read {path.name}: {error}") from error
    return pages


def render_pdf_page(path: Path, page_index: int) -> str:
    """Render one scanned PDF page as a compact base64 JPEG data URL."""
    try:
        with fitz.open(path) as document:
            page = document.load_page(page_index)
            pixmap = page.get_pixmap(matrix=fitz.Matrix(1.8, 1.8), alpha=False)
            image = pixmap.tobytes("jpeg", jpg_quality=70)
    except (OSError, RuntimeError, ValueError) as error:
        raise EusReviewError(
            f"Could not render page {page_index + 1} of {path.name}: {error}"
        ) from error
    encoded = base64.b64encode(image).decode("ascii")
    return f"data:image/jpeg;base64,{encoded}"


def build_extraction_request(pdf_paths: List[Path]) -> List[Dict[str, Any]]:
    """Build multimodal page-labelled content for structured AI extraction."""
    content: List[Dict[str, Any]] = []
    for path in pdf_paths:
        content.append(
            {"type": "text", "text": f"=== FILE: {path.name} ==="}
        )
        pages = extract_pdf_pages(path)
        for page_index, text in enumerate(pages):
            page_number = page_index + 1
            if text:
                content.append(
                    {
                        "type": "text",
                        "text": (
                            f"--- PAGE {page_number} TEXT LAYER (may be garbled) ---\n"
                            f"{text}"
                        ),
                    }
                )
            if page_index >= MAX_IMAGE_PAGES:
                continue
            # Text layers miss checkbox state and tables embedded as images.
            content.extend(
                [
                    {
                        "type": "text",
                        "text": f"--- PAGE {page_number} IMAGE ---",
                    },
                    {
                        "type": "image_url",
                        "image_url": {
                            "url": render_pdf_page(path, page_index),
                            "detail": "high",
                        },
                    },
                ]
            )
    return content


def extract_document_facts(pdf_paths: List[Path]) -> List[Dict[str, Any]]:
    """Use the configured AI to classify documents and extract review facts."""
    if not pdf_paths:
        raise EusReviewError("No PDF attachments were found in this email.")
    if not llm_client.is_ai_configured():
        raise EusReviewError(
            "Automated EUS review needs the LiteLLM settings in .env."
        )
    extracted = []
    for path in pdf_paths:
        request = [
            {"role": "system", "content": EXTRACTION_SYSTEM_PROMPT},
            {"role": "user", "content": build_extraction_request([path])},
        ]
        try:
            reply = llm_client.ask_ai(request, max_tokens=2000)
            answer = llm_client.read_json_reply(reply.content)
        except (llm_client.AiUnavailableError, ValueError, json.JSONDecodeError) as error:
            raise EusReviewError(
                f"Document extraction failed for {path.name}: {error}"
            ) from error
        documents = answer.get("documents")
        if not isinstance(documents, list):
            raise EusReviewError(
                f"Document extraction returned no document list for {path.name}."
            )
        extracted.extend(
            document
            for document in documents
            if isinstance(document, dict)
            and str(document.get("file_name", "")) == path.name
        )
    if not extracted:
        raise EusReviewError(
            "Document extraction did not identify any of the attached PDF files."
        )
    return extracted


def _normalise_identifier(value: Any) -> str:
    return re.sub(r"[^A-Z0-9]", "", str(value or "").upper())


def _normalise_quote_number(value: Any) -> str:
    """Normalize quote formatting while preserving alphanumeric identifiers."""
    identifier = _normalise_identifier(value)
    if identifier.isdigit():
        return identifier.lstrip("0") or "0"
    return identifier


def _normalise_quantity(value: Any) -> str:
    try:
        return str(Decimal(str(value).replace(",", "")).normalize())
    except (InvalidOperation, ValueError):
        return _normalise_identifier(value)


def _normalise_lines(document: Dict[str, Any]) -> set:
    lines = document.get("lines")
    if not isinstance(lines, list):
        return set()
    normalised = set()
    for line in lines:
        if not isinstance(line, dict):
            continue
        normalised.add(
            (
                _normalise_quantity(line.get("quantity")),
                _normalise_identifier(line.get("part")),
                _normalise_identifier(line.get("partner_sku")),
            )
        )
    return {line for line in normalised if any(line)}


def _identifiers_match(first: str, second: str) -> bool:
    """Match equal codes, or a code embedded in a prefixed/suffixed code."""
    if not first or not second:
        return False
    if first == second:
        return True
    shorter, longer = sorted((first, second), key=len)
    return len(shorter) >= MIN_CONTAINED_IDENTIFIER_LENGTH and shorter in longer


def _lines_share_product(eus_lines: set, source_lines: set) -> str:
    """Return the first EUS product code found in the supporting document, or ""."""
    for eus_quantity, eus_part, eus_sku in eus_lines:
        for source_quantity, source_part, source_sku in source_lines:
            if eus_quantity and source_quantity and eus_quantity != source_quantity:
                continue
            # Part numbers often appear in the partner SKU column, so compare across.
            for eus_code in (eus_part, eus_sku):
                for source_code in (source_part, source_sku):
                    if _identifiers_match(eus_code, source_code):
                        return source_code
    return ""


def _documents_of_type(
    documents: List[Dict[str, Any]], document_type: str
) -> List[Dict[str, Any]]:
    return [
        document
        for document in documents
        if str(document.get("document_type", "")).lower() == document_type
    ]


def title_is_manager_or_higher(value: Any) -> bool:
    """Return whether a signer title clearly represents manager level or above."""
    title = str(value or "")
    return bool(
        SENIOR_TITLE_PATTERN.search(title)
        and not JUNIOR_MANAGER_TITLE_PATTERN.search(title)
    )


def _comparison_check(
    code: str,
    label: str,
    eus: Optional[Dict[str, Any]],
    source: Optional[Dict[str, Any]],
) -> EusCheck:
    if eus is None or source is None:
        return EusCheck(code, label, False, "The required documents were not identified.")
    eus_quote = _normalise_quote_number(eus.get("quote_number"))
    source_quote = _normalise_quote_number(source.get("quote_number"))
    eus_lines = _normalise_lines(eus)
    source_lines = _normalise_lines(source)
    quote_matches = bool(
        eus_quote and source_quote and eus_quote == source_quote
    )
    lines_match = _lines_share_product(eus_lines, source_lines)
    mismatches = []
    if not quote_matches:
        mismatches.append(
            f"quote number differs ({eus.get('quote_number')!r} vs "
            f"{source.get('quote_number')!r})"
        )
    if not lines_match:
        mismatches.append(
            "no EUS product was found in it (EUS products: "
            + (", ".join(sorted(code for line in eus_lines for code in line[1:] if code)) or "none extracted")
            + ")"
        )
    return EusCheck(
        code,
        label,
        bool(quote_matches and lines_match),
        f"Quote number and product {lines_match} match."
        if quote_matches and lines_match
        else "; ".join(mismatches),
    )


def validate_embedded_signature(path: Path) -> Tuple[bool, str]:
    """Verify that an embedded PDF signature is cryptographically valid."""
    try:
        with path.open("rb") as source:
            reader = PdfFileReader(source)
            signatures = reader.embedded_regular_signatures
            if not signatures:
                return False, "No embedded digital signature was found."
            failures = []
            for signature in signatures:
                status = validate_pdf_signature(signature)
                if status.intact and status.valid:
                    return True, "The embedded digital signature is valid."
                failures.append(status.summary())
    except (OSError, PdfReadError, SignatureValidationError, ValueError) as error:
        return False, f"Signature validation failed: {error}"
    return False, "; ".join(failures) or "No valid embedded signature was found."


validate_docusign_signature = validate_embedded_signature


def clean_website(website: Any) -> str:
    """Remove OCR artifacts such as spaces and dotless i from a website."""
    address = re.sub(r"\s+", "", str(website or ""))
    return address.replace("ı", "i").replace("İ", "I").strip()


def check_website_reachable(website: Any) -> Tuple[bool, str]:
    """Verify that the end user's website responds over the internet."""
    address = clean_website(website)
    if not address:
        return False, "No end-user website was extracted."
    if not re.match(r"^https?://", address, re.IGNORECASE):
        address = f"https://{address}"
    candidates = [address]
    if address.lower().startswith("https://"):
        candidates.append("http://" + address[len("https://"):])
    last_error = ""
    for url in candidates:
        try:
            response = requests.get(
                url,
                timeout=WEBSITE_TIMEOUT_SECONDS,
                allow_redirects=True,
                headers={"User-Agent": "Mozilla/5.0 (MfgOps EUS verification)"},
            )
        except requests.RequestException as error:
            last_error = str(error)
            continue
        if response.status_code < 400:
            return True, f"{url} responded (HTTP {response.status_code})."
        if response.status_code in WEBSITE_RESTRICTED_STATUSES:
            return True, (
                f"{url} responded with restricted access "
                f"(HTTP {response.status_code})."
            )
        last_error = f"HTTP {response.status_code}"
    return False, f"Website {address} did not respond: {last_error}"


def assess_end_user_legitimacy(
    name: Any, address: Any, website: Any
) -> Tuple[bool, str]:
    """Ask the AI whether the end-user name, address, and website look legitimate."""
    if not any(str(value or "").strip() for value in (name, address, website)):
        return False, "No end-user details were extracted."
    if not llm_client.is_ai_configured():
        return False, "End-user legitimacy needs the LiteLLM settings in .env."
    request = [
        {"role": "system", "content": LEGITIMACY_SYSTEM_PROMPT},
        {
            "role": "user",
            "content": json.dumps(
                {"name": name, "address": address, "website": website}
            ),
        },
    ]
    try:
        reply = llm_client.ask_ai(request, max_tokens=300)
        answer = llm_client.read_json_reply(reply.content)
    except (llm_client.AiUnavailableError, ValueError, json.JSONDecodeError) as error:
        return False, f"End-user legitimacy check failed: {error}"
    return answer.get("legitimate") is True, str(
        answer.get("reason") or "No reason was given."
    )


def evaluate_document_facts(
    documents: List[Dict[str, Any]],
    signature_result: Tuple[bool, str],
    website_result: Tuple[bool, str] = (False, "The website was not checked."),
    legitimacy_result: Tuple[bool, str] = (
        False,
        "End-user legitimacy was not assessed.",
    ),
    repeat_customer: bool = False,
) -> EusReview:
    """Apply deterministic EUS rules to extracted document facts."""
    eus_documents = _documents_of_type(documents, "eus")
    quote_documents = _documents_of_type(documents, "quote")
    po_documents = _documents_of_type(documents, "po")
    eus = eus_documents[0] if len(eus_documents) == 1 else None
    quote = quote_documents[0] if len(quote_documents) == 1 else None
    po = po_documents[0] if len(po_documents) == 1 else None
    visible_signature_present = bool(
        eus and eus.get("signature_present") is True
    )
    signature_passed = signature_result[0] or visible_signature_present
    if signature_result[0]:
        signature_details = signature_result[1]
    elif visible_signature_present:
        signature_details = (
            "A visible handwritten or electronic signature is present. "
            "The PDF has no embedded signature object for cryptographic validation."
        )
    else:
        signature_details = (
            f"{signature_result[1]} No visible signature was identified."
        )

    checks = [
        EusCheck(
            "eus-present",
            "Exactly one EUS document identified",
            len(eus_documents) == 1,
            f"Found {len(eus_documents)} EUS document(s).",
        ),
        EusCheck(
            "supporting-document-present",
            "At least one PO or Quote identified",
            bool(quote_documents or po_documents),
            (
                f"Found {len(quote_documents)} quote(s) and "
                f"{len(po_documents)} PO document(s)."
            ),
        ),
    ]
    if quote_documents:
        checks.append(
            EusCheck(
                "quote-unique",
                "At most one Quote identified",
                len(quote_documents) == 1,
                f"Found {len(quote_documents)} quote(s).",
            )
        )
    if po_documents:
        checks.append(
            EusCheck(
                "po-unique",
                "At most one PO identified",
                len(po_documents) == 1,
                f"Found {len(po_documents)} PO document(s).",
            )
        )

    comparisons = []
    if quote is not None:
        comparisons.append(
            (
                "Quote",
                _comparison_check("quote-details", "", eus, quote),
            )
        )
    if po is not None:
        comparisons.append(
            ("PO", _comparison_check("po-details", "", eus, po))
        )
    matched_sources = [name for name, check in comparisons if check.passed]
    if matched_sources:
        comparison_details = (
            "EUS quote number and at least one product line match "
            + " and ".join(matched_sources)
            + "."
        )
    elif comparisons:
        comparison_details = "; ".join(
            f"{name}: {check.details}" for name, check in comparisons
        )
    else:
        comparison_details = "No unique PO or Quote was available for comparison."
    checks.append(
        EusCheck(
            "supporting-document-details",
            "EUS details match either PO or Quote",
            bool(matched_sources),
            comparison_details,
        )
    )
    checks.extend(
        [
            EusCheck(
                "question-6",
                'EUS question 6 has "No" selected',
                bool(eus and eus.get("question_6_no_selected") is True),
                ('"No" is selected.'
                if eus and eus.get("question_6_no_selected") is True
                else '"No" was not clearly selected.')
                + (
                    f" Evidence: {eus.get('question_6_evidence')}"
                    if eus and eus.get("question_6_evidence")
                    else ""
                ),
            ),
            EusCheck(
                "signer-title",
                "Signer title is manager or higher",
                bool(eus and title_is_manager_or_higher(eus.get("signer_title"))),
                f"Extracted title: {eus.get('signer_title')!r}"
                if eus
                else "No EUS signer title was extracted.",
            ),
            EusCheck(
                "signer-email",
                "Signer email address has a valid format",
                bool(
                    eus
                    and EMAIL_PATTERN.fullmatch(str(eus.get("signer_email") or ""))
                ),
                f"Extracted email: {eus.get('signer_email')!r}"
                if eus
                else "No EUS signer email was extracted.",
            ),
            EusCheck(
                "dates",
                "A document or signature date is present",
                bool(
                    eus
                    and (
                        str(eus.get("document_date") or "").strip()
                        or str(eus.get("signature_date") or "").strip()
                    )
                ),
                (
                    f"Document date: {eus.get('document_date')!r}; "
                    f"signature date: {eus.get('signature_date')!r}"
                )
                if eus
                else "No EUS dates were extracted.",
            ),
            EusCheck(
                "signature-valid",
                "Signature is present or digitally valid",
                signature_passed,
                signature_details,
            ),
        ]
    )
    end_user = {
        field: str((eus or {}).get(f"end_user_{field}") or "").strip()
        for field in ("name", "address", "website")
    }
    missing_fields = [field for field, value in end_user.items() if not value]
    checks.extend(
        [
            EusCheck(
                "end-user-details",
                "End-user name, address, and website are present",
                not missing_fields,
                (
                    f"{end_user['name']!r}; {end_user['address']!r}; "
                    f"{end_user['website']!r}"
                )
                if not missing_fields
                else "Missing end-user " + ", ".join(missing_fields) + ".",
            ),
            EusCheck(
                "end-user-website",
                "End-user website is reachable",
                website_result[0],
                website_result[1],
            ),
            EusCheck(
                "end-user-legitimate",
                "End user appears legitimate",
                legitimacy_result[0],
                legitimacy_result[1],
            ),
        ]
    )
    document_types = {
        str(document.get("file_name", "unknown")): str(
            document.get("document_type", "other")
        )
        for document in documents
    }
    return EusReview(
        checks=checks,
        document_types=document_types,
        end_user_name=end_user["name"] or None,
        repeat_customer=repeat_customer,
    )


def review_mail_documents(
    paths: List[Path],
    is_repeat_customer: Optional[Callable[[str], bool]] = None,
) -> EusReview:
    """Extract and validate all PDF attachments from one email folder."""
    pdf_paths = [path for path in paths if path.suffix.lower() == ".pdf"]
    documents = extract_document_facts(pdf_paths)
    eus_documents = _documents_of_type(documents, "eus")
    eus = eus_documents[0] if len(eus_documents) == 1 else {}
    eus_name = str(eus.get("file_name", ""))
    eus_path = next((path for path in pdf_paths if path.name == eus_name), None)
    signature_result = (
        validate_docusign_signature(eus_path)
        if eus_path
        else (False, "The EUS PDF could not be identified for signature validation.")
    )
    end_user_name = str(eus.get("end_user_name") or "").strip()
    return evaluate_document_facts(
        documents,
        signature_result,
        website_result=check_website_reachable(eus.get("end_user_website")),
        legitimacy_result=assess_end_user_legitimacy(
            eus.get("end_user_name"),
            eus.get("end_user_address"),
            clean_website(eus.get("end_user_website")),
        ),
        repeat_customer=bool(
            end_user_name and is_repeat_customer and is_repeat_customer(end_user_name)
        ),
    )
