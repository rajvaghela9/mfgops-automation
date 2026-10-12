"""The AI judge: looks at one order and recommends Release, Send to OM, or Hold.

Guard rails: the fixed (hard) rules have already run and the AI can't weaken them. If a
hard rule rejects the order, the verdict is reject; if one holds it, at least hold. The AI
applies the soft rules on top and explains everything in plain words.
"""

import json
from typing import Any, Dict, List

from src.intelligence_layer import llm_client
from src.intelligence_layer.rule_checks import (
    RuleCheckResult,
    stricter_verdict,
    verdict_from_findings,
)
from src.utils.logging_config import get_logger
from src.utils.models import ROUTE_LABELS, Judgement, Order, Verdict

logger = get_logger(__name__)

SYSTEM_PROMPT = """You review sales orders for Infoblox Manufacturing Operations (MfgOps) \
before they are released to Arrow, the contract manufacturer that builds and ships them.

Give one verdict:
- "approve": safe to release to Arrow.
- "reject": the OM team must correct the order in Fusion.
- "hold": a person must check something first (missing document, unclear or complex \
order, or you are not confident).

Fixed-check results are final. If any fixed check says reject, your verdict must be \
reject. If any says hold, your verdict must be hold or reject.
Then apply the judgement rules using their guidance. Ignore a judgement rule that \
doesn't fit the order.

Notes on the data: it comes from the Fusion backlog report, which doesn't include the \
customer's own PO number or the end-customer country, so "not provided" for those is \
normal, not a problem. "Ship from: ARROW" is just the warehouse and says nothing about \
whether the order was sent. Only mention that an order was already sent to Arrow if the \
order says so explicitly; judge the order on its content.

Write for operations people, not IT staff: plain words, no field names, no code, no \
JSON in the text. Keep the summary to one or two sentences. Give each reason as one \
short sentence saying what is wrong and who must act.

Reply with only this JSON object:
{"verdict": "approve|reject|hold", "summary": "...", "reasons": ["..."], \
"confidence": "high|medium|low"}"""


def describe_order(order: Order) -> str:
    """Write the order out as readable text for the AI.

    Args:
        order: The order, with its lines.

    Returns:
        The order details.
    """
    arrow_status = (
        f"Already sent to Arrow (Arrow PO {order.arrow_po})"
        if order.arrow_po
        else "Not sent to Arrow yet"
    )
    lines = "\n".join(
        f"  - line {line.line_number}: {line.ordered_qty} x {line.sku} "
        f"({line.item_description}; {line.item_class})"
        for line in order.lines
    )
    return (
        f"Order {order.order_no} ({order.order_type}), booked {order.booked_date}\n"
        f"Category: {ROUTE_LABELS.get(order.route, order.route)} — {order.route_reason}\n"
        f"Bill to: {order.bill_to_customer}, {order.bill_to_address}\n"
        f"Ship to: {order.ship_to_customer}, {order.ship_to_address}, "
        f"{order.ship_to_country}\n"
        f"Ship from: {order.ship_from or 'not given'}\n"
        f"Customer PO: {order.customer_po or 'not provided'} | "
        f"{arrow_status} | FOB: {order.fob or 'missing'} | "
        f"Freight terms: {order.freight_terms or 'missing'} | "
        f"Shipping method: {order.shipping_method or 'missing'}\n"
        f"Shipping instructions: {order.shipping_instructions or 'none'}\n"
        f"Lines:\n{lines}"
    )


def build_review_request(order: Order, checks: RuleCheckResult) -> str:
    """Write the user message: the order, its fixed-check results, and the judgement rules.

    Args:
        order: The order.
        checks: Output of rule_checks.check_order_against_rules.

    Returns:
        The message text.
    """
    findings = "\n".join(
        f"- [{finding.outcome}] {finding.rule_name}: {finding.message}"
        for finding in checks.findings
    )
    judgement_rules = "\n".join(
        f"- {rule.name} (usual outcome: {rule.outcome}): {rule.guidance}"
        for rule in checks.judgement_rules
    )
    return (
        f"{describe_order(order)}\n\n"
        f"Facts worked out by code:\n{json.dumps(checks.facts, default=str)}\n\n"
        f"Fixed-check results:\n{findings or '- All fixed checks passed.'}\n\n"
        f"Judgement rules for this order:\n{judgement_rules or '- None.'}"
    )


def judge_without_ai(checks: RuleCheckResult, problem: str) -> Judgement:
    """Build a verdict from the fixed checks alone (when the AI is off or failed).

    Args:
        checks: Output of rule_checks.check_order_against_rules.
        problem: Why the AI wasn't used.

    Returns:
        A Judgement based on the hard rules only.
    """
    reasons = [finding.message for finding in checks.findings]
    if checks.judgement_rules:
        names = ", ".join(rule.name for rule in checks.judgement_rules)
        reasons.append(f"Please check these yourself: {names}.")
    return Judgement(
        verdict=verdict_from_findings(checks.findings),
        summary=f"AI review not available ({problem}); only the fixed checks were run.",
        reasons=reasons,
        confidence="low",
        used_ai=False,
    )


def ask_for_verdict(request: List[Dict[str, str]]) -> Dict[str, Any]:
    """Ask the AI for its verdict as JSON, trying once more if the reply is unusable.

    Args:
        request: The system and user messages.

    Returns:
        The AI's answer (it has a valid "verdict"). Raises ValueError if both replies fail.
    """
    problem = ""
    for _attempt in range(2):
        reply = llm_client.ask_ai(request, max_tokens=1500)
        try:
            answer = llm_client.read_json_reply(reply.content)
        except ValueError as error:
            problem = str(error)
            continue
        if str(answer.get("verdict", "")).lower() in [
            option.value for option in Verdict
        ]:
            return answer
        problem = f"unexpected verdict {answer.get('verdict')!r}"
    raise ValueError(problem)


def judge_order(order: Order, checks: RuleCheckResult) -> Judgement:
    """Ask the AI for a verdict on one order, never weaker than the fixed checks allow.

    Args:
        order: The order (with route set).
        checks: Output of rule_checks.check_order_against_rules.

    Returns:
        The Judgement shown to the reviewer.
    """
    if not llm_client.is_ai_configured():
        return judge_without_ai(checks, "the AI is switched off")
    request = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": build_review_request(order, checks)},
    ]
    try:
        answer = ask_for_verdict(request)
    except llm_client.AiUnavailableError as error:
        logger.warning("Judging %s without AI: %s", order.order_no, error)
        return judge_without_ai(checks, str(error))
    except ValueError as error:
        logger.warning("Judging %s without AI: %s", order.order_no, error)
        return judge_without_ai(checks, "the AI replied in an unexpected format")
    verdict = str(answer["verdict"]).lower()

    reasons: List[str] = [str(reason) for reason in answer.get("reasons") or []]
    fixed_verdict = verdict_from_findings(checks.findings)
    final_verdict = stricter_verdict(verdict, fixed_verdict)
    if final_verdict != verdict:
        reasons.insert(
            0, "A fixed check makes this order stricter than the AI suggested."
        )
    return Judgement(
        verdict=final_verdict,
        summary=str(answer.get("summary", "")).strip(),
        reasons=reasons,
        confidence=str(answer.get("confidence", "")),
    )
