from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import fitz
from pypdf import PdfWriter

from src.intelligence_layer.eus_document_review import (
    EXTRACTION_SYSTEM_PROMPT,
    build_extraction_request,
    REPEAT_CUSTOMER_CONFIDENCE,
    check_website_reachable,
    clean_website,
    evaluate_document_facts,
    extract_document_facts,
    validate_embedded_signature,
)

VERIFIED = (True, "Verified.")


def evaluate(documents, signature_result, **kwargs):
    kwargs.setdefault("website_result", VERIFIED)
    kwargs.setdefault("legitimacy_result", VERIFIED)
    return evaluate_document_facts(documents, signature_result, **kwargs)


def test_scanned_pdf_page_is_rendered_for_visual_extraction(tmp_path):
    pdf_path = tmp_path / "scanned-eus.pdf"
    writer = PdfWriter()
    writer.add_blank_page(width=612, height=792)
    with pdf_path.open("wb") as output:
        writer.write(output)

    content = build_extraction_request([pdf_path])

    assert [item["type"] for item in content] == ["text", "text", "image_url"]
    assert content[1]["text"].startswith("--- PAGE 1 IMAGE ---")
    assert content[2]["image_url"]["url"].startswith("data:image/jpeg;base64,")
    assert content[2]["image_url"]["detail"] == "high"


def test_each_pdf_is_sent_in_a_separate_ai_request():
    paths = [Path("eus.pdf"), Path("quote.pdf")]
    replies = [
        SimpleNamespace(
            content='{"documents":[{"file_name":"eus.pdf","document_type":"eus"}]}'
        ),
        SimpleNamespace(
            content='{"documents":[{"file_name":"quote.pdf","document_type":"quote"}]}'
        ),
    ]

    with (
        patch(
            "src.intelligence_layer.eus_document_review.llm_client.is_ai_configured",
            return_value=True,
        ),
        patch(
            "src.intelligence_layer.eus_document_review.build_extraction_request",
            side_effect=lambda pdfs: [{"type": "text", "text": pdfs[0].name}],
        ),
        patch(
            "src.intelligence_layer.eus_document_review.llm_client.ask_ai",
            side_effect=replies,
        ) as ask_ai,
    ):
        documents = extract_document_facts(paths)

    assert ask_ai.call_count == 2
    assert [document["file_name"] for document in documents] == [
        "eus.pdf",
        "quote.pdf",
    ]


def valid_eus():
    return {
        "file_name": "eus.pdf",
        "document_type": "eus",
        "quote_number": "QUOTE-123",
        "lines": [
            {"quantity": 2, "part": "PART-1", "partner_sku": "SKU-1"}
        ],
        "question_6_no_selected": True,
        "signer_title": "Senior Manager",
        "signer_email": "manager@example.com",
        "document_date": "2026-10-22",
        "signature_date": "2026-10-22",
        "signature_present": True,
        "end_user_name": "Example Bank",
        "end_user_address": "1 Main Street, Kyiv, Ukraine",
        "end_user_website": "www.example.com",
    }


def supporting_document(document_type, quote_number="QUOTE-123"):
    return {
        "file_name": f"{document_type}.pdf",
        "document_type": document_type,
        "quote_number": quote_number,
        "lines": [
            {"quantity": 2, "part": "PART-1", "partner_sku": "SKU-1"}
        ],
    }


def test_quote_is_optional_when_po_matches():
    review = evaluate(
        [valid_eus(), supporting_document("po")],
        (True, "Valid DocuSign signature."),
    )

    assert review.passed
    assert "quote-present" not in {check.code for check in review.checks}


def test_po_is_optional_when_quote_matches():
    review = evaluate(
        [valid_eus(), supporting_document("quote")],
        (True, "Valid DocuSign signature."),
    )

    assert review.passed


def test_eus_passes_when_either_po_or_quote_matches():
    review = evaluate(
        [
            valid_eus(),
            supporting_document("quote", quote_number="DIFFERENT"),
            supporting_document("po"),
        ],
        (True, "Valid DocuSign signature."),
    )

    comparison = next(
        check
        for check in review.checks
        if check.code == "supporting-document-details"
    )
    assert comparison.passed
    assert "PO" in comparison.details
    assert review.passed


def test_order_number_is_ignored_when_quote_number_matches():
    po = supporting_document("po")
    po["order_number"] = "UNRELATED-PO-NUMBER"

    review = evaluate(
        [valid_eus(), po],
        (True, "Valid DocuSign signature."),
    )

    assert review.passed


def test_quote_number_ignores_hash_and_leading_zero_formatting():
    eus = valid_eus()
    eus["quote_number"] = "#49045"
    quote = supporting_document("quote", quote_number="00049045")

    review = evaluate(
        [eus, quote],
        (True, "Valid signature."),
    )

    comparison = next(
        check
        for check in review.checks
        if check.code == "supporting-document-details"
    )
    assert comparison.passed
    assert review.passed


def test_eus_requires_at_least_one_po_or_quote():
    review = evaluate(
        [valid_eus()],
        (True, "Valid DocuSign signature."),
    )

    assert not review.passed
    failed_codes = {check.code for check in review.checks if not check.passed}
    assert {
        "supporting-document-present",
        "supporting-document-details",
    } <= failed_codes


def test_extraction_prompt_does_not_require_fixed_eus_pages():
    assert "do not rely on a fixed page number" in EXTRACTION_SYSTEM_PROMPT
    assert "only from page 2" not in EXTRACTION_SYSTEM_PROMPT
    assert "only from page 3" not in EXTRACTION_SYSTEM_PROMPT


def test_either_document_or_signature_date_is_enough():
    eus_with_signature_date = valid_eus()
    eus_with_signature_date["document_date"] = None
    signature_date_review = evaluate(
        [eus_with_signature_date, supporting_document("quote")],
        (True, "Valid signature."),
    )

    eus_with_document_date = valid_eus()
    eus_with_document_date["signature_date"] = None
    document_date_review = evaluate(
        [eus_with_document_date, supporting_document("quote")],
        (True, "Valid signature."),
    )

    assert signature_date_review.passed
    assert document_date_review.passed


def test_missing_all_dates_fails():
    eus = valid_eus()
    eus["document_date"] = None
    eus["signature_date"] = None

    review = evaluate(
        [eus, supporting_document("quote")],
        (True, "Valid signature."),
    )

    assert not next(check for check in review.checks if check.code == "dates").passed


def test_valid_signature_does_not_require_trusted_or_docusign_certificate(
    tmp_path,
):
    pdf = tmp_path / "signed.pdf"
    pdf.write_bytes(b"signed pdf")
    signature = object()
    status = SimpleNamespace(
        intact=True,
        valid=True,
        bottom_line=False,
        summary=lambda: "certificate is not trusted",
    )

    with (
        patch(
            "src.intelligence_layer.eus_document_review.PdfFileReader",
            return_value=SimpleNamespace(
                embedded_regular_signatures=[signature]
            ),
        ),
        patch(
            "src.intelligence_layer.eus_document_review.validate_pdf_signature",
            return_value=status,
        ),
    ):
        valid, details = validate_embedded_signature(pdf)

    assert valid
    assert details == "The embedded digital signature is valid."


def test_visible_signature_passes_without_embedded_signature():
    review = evaluate(
        [valid_eus(), supporting_document("quote")],
        (False, "No embedded digital signature was found."),
    )

    signature_check = next(
        check for check in review.checks if check.code == "signature-valid"
    )
    assert signature_check.passed
    assert "visible handwritten or electronic signature" in signature_check.details
    assert review.passed


def test_missing_visible_and_embedded_signature_fails():
    eus = valid_eus()
    eus["signature_present"] = False

    review = evaluate(
        [eus, supporting_document("quote")],
        (False, "No embedded digital signature was found."),
    )

    signature_check = next(
        check for check in review.checks if check.code == "signature-valid"
    )
    assert not signature_check.passed
    assert "No visible signature was identified" in signature_check.details


def test_single_matching_product_line_is_enough():
    eus = valid_eus()
    eus["lines"] = [
        {"quantity": 2, "part": "PART-1", "partner_sku": "SKU-1"},
        {"quantity": 5, "part": "PART-9", "partner_sku": None},
    ]
    quote = supporting_document("quote")
    quote["lines"].append({"quantity": 1, "part": "PART-7", "partner_sku": None})

    review = evaluate([eus, quote], VERIFIED)

    assert review.passed


def test_prefixed_eus_part_without_quantity_matches_po_partner_sku():
    eus = valid_eus()
    eus["quote_number"] = "#49045"
    eus["lines"] = [
        {"quantity": None, "part": "UKRSIBBANK-TE-1506-HW-AC", "partner_sku": None}
    ]
    po = supporting_document("po", quote_number="00049045")
    po["lines"] = [
        {"quantity": 2, "part": "Trinzic X6 1506", "partner_sku": "TE-1506-HW-AC"}
    ]

    review = evaluate([eus, po], VERIFIED)

    details = next(
        check for check in review.checks
        if check.code == "supporting-document-details"
    )
    assert details.passed, details.details


def test_question_6_details_include_evidence():
    eus = valid_eus()
    eus["question_6_evidence"] = "No box ticked, Yes box empty"

    review = evaluate([eus, supporting_document("po")], VERIFIED)

    question_6 = next(c for c in review.checks if c.code == "question-6")
    assert question_6.passed
    assert "No box ticked, Yes box empty" in question_6.details


def test_text_pages_are_also_sent_as_images(tmp_path):
    pdf_path = tmp_path / "text-eus.pdf"
    document = fitz.open()
    document.new_page().insert_text((72, 72), "End User Statement quote 49045")
    document.save(pdf_path)

    content = build_extraction_request([pdf_path])

    assert any("TEXT LAYER" in item.get("text", "") for item in content)
    assert any(item["type"] == "image_url" for item in content)


def test_clean_website_removes_ocr_artifacts():
    assert clean_website("ukrsıbbank. com") == "ukrsibbank.com"


def test_no_matching_product_line_fails():
    quote = supporting_document("quote")
    quote["lines"] = [{"quantity": 3, "part": "OTHER", "partner_sku": "OTHER"}]

    review = evaluate([valid_eus(), quote], VERIFIED)

    assert not review.passed


def test_end_user_checks_are_required_for_new_customers():
    eus = valid_eus()
    eus["end_user_website"] = None

    review = evaluate(
        [eus, supporting_document("quote")],
        VERIFIED,
        website_result=(False, "No end-user website was extracted."),
        legitimacy_result=(False, "Website does not match the name."),
    )

    failed = {check.code for check in review.checks if not check.passed}
    assert failed == {
        "end-user-details",
        "end-user-website",
        "end-user-legitimate",
    }
    assert not review.passed


def test_repeat_customer_passes_at_seventy_percent_confidence():
    eus = valid_eus()
    eus["signer_email"] = "not-an-email"
    eus["question_6_no_selected"] = False

    new_customer = evaluate([eus, supporting_document("quote")], VERIFIED)
    repeat_customer = evaluate(
        [eus, supporting_document("quote")], VERIFIED, repeat_customer=True
    )

    assert not new_customer.passed
    assert repeat_customer.confidence >= REPEAT_CUSTOMER_CONFIDENCE
    assert repeat_customer.passed


def test_repeat_customer_below_seventy_percent_fails():
    eus = valid_eus()
    for field in ("signer_email", "signer_title", "document_date", "signature_date"):
        eus[field] = None
    eus["question_6_no_selected"] = False

    review = evaluate(
        [eus, supporting_document("quote")],
        (False, "No embedded digital signature was found."),
        repeat_customer=True,
    )

    assert review.confidence < REPEAT_CUSTOMER_CONFIDENCE
    assert not review.passed


def test_website_check_accepts_responding_site():
    response = SimpleNamespace(status_code=200)
    with patch(
        "src.intelligence_layer.eus_document_review.requests.get",
        return_value=response,
    ) as get:
        passed, details = check_website_reachable("www.example.com")

    assert passed
    assert get.call_args.args[0] == "https://www.example.com"
    assert "HTTP 200" in details


def test_website_check_fails_when_unreachable():
    import requests

    with patch(
        "src.intelligence_layer.eus_document_review.requests.get",
        side_effect=requests.ConnectionError("no route"),
    ):
        passed, details = check_website_reachable("bad.invalid")

    assert not passed
    assert "did not respond" in details
