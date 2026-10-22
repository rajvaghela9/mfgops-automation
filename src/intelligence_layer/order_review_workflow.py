"""The order review workflow, one LangGraph graph per order:

    decide category -> run fixed checks -> AI verdict -> send to review queue
        -> [PAUSE: wait for the reviewer]
        -> approve (released in Fusion by hand) | email OM | keep on hold (-> wait again)
        -> learn from disagreement
        -> (approved) [PAUSE: wait for Fusion's USPO] -> finish release (+ Arrow mail)
        -> done

While paused nothing runs: the graph is saved to WORKFLOW_DB_PATH and the process can
stop. A button click in the UI resumes it (submit_reviewer_decision); the USPO showing up
in a backlog report mail resumes an approved order (confirm_release_from_report). Dummy
orders never exist in Fusion, so they are released straight after approval.
Only the "AI verdict" step uses the AI; every other step is plain code.
"""

import sqlite3
import uuid
from dataclasses import asdict
from pathlib import Path
from typing import Dict, List, Optional, TypedDict

from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt

from src.integration_layer.outbound_mail import MailNotSentError
from src.integration_layer.push_orders_to_b2b import (
    ESS_RELEASE,
    release_order_to_arrow,
    release_strategy,
)
from src.integration_layer.send_back_to_om import email_om_team
from src.integration_layer.send_order_mail_to_arrow import email_arrow, needs_arrow_mail
from src.intelligence_layer.ai_order_judge import describe_order, judge_order
from src.intelligence_layer.llm_client import is_ai_configured
from src.intelligence_layer.order_router import decide_route
from src.intelligence_layer.rule_checks import (
    RuleCheckResult,
    check_order_against_rules,
    find_release_blocker,
)
from src.knowledge_layer import (
    learning_request_store,
    order_store,
    reference_store,
    rule_store,
)
from src.utils.config import load_config
from src.utils.logging_config import get_logger
from src.utils.models import (
    REVIEWABLE_STATUSES,
    OrderStatus,
    ReviewerDecision,
    RuleFinding,
    Verdict,
    is_dummy_order,
)
from src.utils.time_helpers import utc_now_iso

logger = get_logger(__name__)


class OrderReviewState(TypedDict, total=False):
    """What the workflow carries from step to step for one order."""

    order_no: str
    facts: Dict
    findings: List[Dict]
    judgement_rule_ids: List[int]
    ai_verdict: str
    ai_used: bool
    ai_summary: str
    ai_reasons: List[str]
    order_details: str
    reviewer_decision: str
    reviewer_comment: str
    reviewer: str
    release_request_id: str
    uspo: str


def decide_order_category(state: OrderReviewState) -> Dict:
    """Step 1: Standard, Special Instructions, EUS, or EUS + Special Instructions.

    Args:
        state: The workflow state.

    Returns:
        No state changes (the category is saved on the order).
    """
    order = order_store.get_order(state["order_no"])
    route, reason = decide_route(order)
    order_store.update_order(order.order_no, route=route.value, route_reason=reason)
    return {}


def run_fixed_checks(state: OrderReviewState) -> Dict:
    """Step 2: run the hard rules and pick the soft rules that apply to this order.

    Args:
        state: The workflow state.

    Returns:
        The facts, hard-rule findings and soft-rule ids.
    """
    order = order_store.get_order(state["order_no"])
    checks = check_order_against_rules(
        order, rule_store.get_rules(), reference_store.get_all_reference_lists()
    )
    order_store.save_rule_findings(order.order_no, checks.findings)
    return {
        "facts": checks.facts,
        "findings": [asdict(finding) for finding in checks.findings],
        "judgement_rule_ids": [rule.id for rule in checks.judgement_rules],
    }


def ask_ai_for_verdict(state: OrderReviewState) -> Dict:
    """Step 3: the AI judges the order on top of the fixed checks.

    Args:
        state: The workflow state.

    Returns:
        The AI verdict, summary and reasons.
    """
    order = order_store.get_order(state["order_no"])
    checks = RuleCheckResult(
        facts=state["facts"],
        findings=[RuleFinding(**finding) for finding in state["findings"]],
        judgement_rules=[
            rule
            for rule in map(rule_store.get_rule, state["judgement_rule_ids"])
            if rule is not None
        ],
    )
    judgement = judge_order(order, checks)
    return {
        "ai_verdict": judgement.verdict,
        "ai_used": judgement.used_ai,
        "ai_summary": judgement.summary,
        "ai_reasons": judgement.reasons,
    }


def send_to_review_queue(state: OrderReviewState) -> Dict:
    """Step 4: save the verdict so the order shows up in the reviewer's queue.

    Args:
        state: The workflow state.

    Returns:
        The order details as text (kept in case the AI must learn from this order later).
    """
    status = (
        OrderStatus.ON_HOLD.value
        if state["ai_verdict"] == Verdict.HOLD.value
        else OrderStatus.WAITING_FOR_REVIEW.value
    )
    order_store.update_order(
        state["order_no"],
        ai_verdict=state["ai_verdict"],
        ai_used=state["ai_used"],
        ai_summary=state["ai_summary"],
        ai_reasons=state["ai_reasons"],
        status=status,
    )
    return {"order_details": describe_order(order_store.get_order(state["order_no"]))}


def wait_for_reviewer(state: OrderReviewState) -> Dict:
    """Step 5: pause until a MfgOps user clicks Approve, Send to OM, or Hold.

    Args:
        state: The workflow state.

    Returns:
        The reviewer's decision, comment and name.
    """
    answer = interrupt({"order_no": state["order_no"]})
    return {
        "reviewer_decision": answer["decision"],
        "reviewer_comment": answer.get("comment") or "",
        "reviewer": answer["reviewer"],
    }


def choose_next_step(state: OrderReviewState) -> str:
    """Pick the step that carries out the reviewer's decision.

    Args:
        state: The workflow state.

    Returns:
        The name of the next step.
    """
    return {
        ReviewerDecision.APPROVE.value: "approve_and_wait_for_uspo",
        ReviewerDecision.SEND_TO_OM.value: "send_back_to_om_team",
        ReviewerDecision.HOLD.value: "keep_on_hold",
    }[state["reviewer_decision"]]


def approve_and_wait_for_uspo(state: OrderReviewState) -> Dict:
    """Record the approval. The reviewer has released the order in Fusion by hand (or, in
    FUSION_MODE=live, the release job is submitted here); it stays listed as "Approved"
    until Fusion's USPO for it appears in a backlog report.

    Args:
        state: The workflow state.

    Returns:
        The release job id ("" unless the release was submitted to Fusion).
    """
    order_no = state["order_no"]
    order_store.record_reviewer_decision(
        order_no,
        state["reviewer_decision"],
        state["reviewer"],
        state["reviewer_comment"],
    )
    request_id = ""
    if release_strategy() == ESS_RELEASE and not is_dummy_order(order_no):
        request_id = release_order_to_arrow(order_no)
    order_store.update_order(
        order_no, status=OrderStatus.APPROVED.value, approved_at=utc_now_iso()
    )
    return {"release_request_id": request_id}


def wait_for_uspo(state: OrderReviewState) -> Dict:
    """Pause until Fusion's USPO for the order shows up in a backlog report mail.

    Args:
        state: The workflow state.

    Returns:
        The USPO.
    """
    answer = interrupt({"order_no": state["order_no"], "waiting_for": "uspo"})
    return {"uspo": answer["uspo"]}


def finish_release(state: OrderReviewState) -> Dict:
    """Mail Arrow the special instructions (if the order has any), then move the order
    from active orders to all orders.

    A failed Arrow mail is logged (and shown in the UI) but doesn't stop the release:
    the order is already with Arrow through B2B.

    Args:
        state: The workflow state.

    Returns:
        No state changes.
    """
    order_no = state["order_no"]
    uspo = state.get("uspo", "")
    order = order_store.get_order(order_no)
    if order is not None and uspo and needs_arrow_mail(order):
        try:
            email_arrow(order, uspo)
        except MailNotSentError as error:
            logger.warning("Arrow mail for %s not sent: %s", order_no, error)
    order_store.mark_released(
        order_no,
        release_request_id=state.get("release_request_id", ""),
        uspo=uspo,
        released_by=state["reviewer"],
    )
    return {}


def send_back_to_om_team(state: OrderReviewState) -> Dict:
    """Email OM the reviewer's correction and the order details; wait for their fix.

    Args:
        state: The workflow state.

    Returns:
        No state changes.
    """
    order_no = state["order_no"]
    email_om_team(
        order_store.get_order(order_no),
        order_store.get_rule_findings(order_no),
        state["reviewer"],
        state["reviewer_comment"],
    )
    order_store.record_reviewer_decision(
        order_no,
        state["reviewer_decision"],
        state["reviewer"],
        state["reviewer_comment"],
    )
    order_store.update_order(order_no, status=OrderStatus.SENT_TO_OM.value)
    return {}


def keep_on_hold(state: OrderReviewState) -> Dict:
    """Put the order on hold; it stays in the queue until the reviewer decides again.

    Args:
        state: The workflow state.

    Returns:
        No state changes.
    """
    order_store.record_reviewer_decision(
        state["order_no"],
        state["reviewer_decision"],
        state["reviewer"],
        state["reviewer_comment"],
    )
    order_store.update_order(state["order_no"], status=OrderStatus.ON_HOLD.value)
    return {}


def learn_from_disagreement(state: OrderReviewState) -> Dict:
    """If the reviewer and the AI disagreed, ask the Rules Assistant to learn why.

    Args:
        state: The workflow state.

    Returns:
        No state changes.
    """
    if (
        order_store.is_agreement(state["ai_verdict"], state["reviewer_decision"])
        is False
    ):
        learning_request_store.create_learning_request(
            state["order_no"],
            state["ai_verdict"],
            state["reviewer_decision"],
            state["reviewer_comment"],
            state["reviewer"],
            order_details=(
                f"{state['order_details']}\n\nAI summary: {state['ai_summary']}\n"
                "AI reasons:\n"
                + "\n".join(f"- {reason}" for reason in state["ai_reasons"])
            ),
        )
    return {}


def choose_step_after_learning(state: OrderReviewState) -> str:
    """After an approval, wait for the USPO (dummy orders are released straight away).

    Args:
        state: The workflow state.

    Returns:
        The name of the next step, or END.
    """
    if state["reviewer_decision"] != ReviewerDecision.APPROVE.value:
        return END
    return "finish_release" if is_dummy_order(state["order_no"]) else "wait_for_uspo"


def build_workflow() -> StateGraph:
    """Wire the steps together.

    Returns:
        The (uncompiled) workflow graph.
    """
    workflow = StateGraph(OrderReviewState)
    steps = [
        decide_order_category,
        run_fixed_checks,
        ask_ai_for_verdict,
        send_to_review_queue,
        wait_for_reviewer,
        approve_and_wait_for_uspo,
        send_back_to_om_team,
        keep_on_hold,
        learn_from_disagreement,
        wait_for_uspo,
        finish_release,
    ]
    for step in steps:
        workflow.add_node(step.__name__, step)
    workflow.add_edge(START, "decide_order_category")
    workflow.add_edge("decide_order_category", "run_fixed_checks")
    workflow.add_edge("run_fixed_checks", "ask_ai_for_verdict")
    workflow.add_edge("ask_ai_for_verdict", "send_to_review_queue")
    workflow.add_edge("send_to_review_queue", "wait_for_reviewer")
    workflow.add_conditional_edges(
        "wait_for_reviewer",
        choose_next_step,
        ["approve_and_wait_for_uspo", "send_back_to_om_team", "keep_on_hold"],
    )
    workflow.add_edge("keep_on_hold", "wait_for_reviewer")
    workflow.add_edge("approve_and_wait_for_uspo", "learn_from_disagreement")
    workflow.add_edge("send_back_to_om_team", "learn_from_disagreement")
    workflow.add_conditional_edges(
        "learn_from_disagreement",
        choose_step_after_learning,
        ["wait_for_uspo", "finish_release", END],
    )
    workflow.add_edge("wait_for_uspo", "finish_release")
    workflow.add_edge("finish_release", END)
    return workflow


_compiled_workflows: Dict[str, object] = {}


def open_checkpoint_store() -> SqliteSaver:
    """Open the SQLite file where paused workflows are saved.

    Returns:
        A LangGraph checkpointer.
    """
    path = Path(load_config().workflow_db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    return SqliteSaver(sqlite3.connect(path, check_same_thread=False, timeout=30))


def get_workflow():
    """Return the compiled workflow, connected to the checkpoint file (built once).

    Returns:
        The compiled LangGraph workflow.
    """
    path = load_config().workflow_db_path
    if path not in _compiled_workflows:
        _compiled_workflows[path] = build_workflow().compile(
            checkpointer=open_checkpoint_store()
        )
    return _compiled_workflows[path]


def thread_config(thread_id: str) -> Dict:
    """Build the LangGraph config that identifies one saved workflow run.

    Args:
        thread_id: The run's id.

    Returns:
        The config dict.
    """
    return {"configurable": {"thread_id": thread_id}}


def start_order_review(order_no: str) -> None:
    """Run the workflow for one order until it waits for the reviewer.

    Every run gets its own saved workflow, so an order can be reviewed again (after OM's
    correction, or when the AI was unavailable the first time).

    Args:
        order_no: Fusion order number.

    Returns:
        None.
    """
    thread_id = f"{order_no}-{uuid.uuid4().hex[:8]}"
    order_store.update_order(order_no, review_thread_id=thread_id)
    get_workflow().invoke({"order_no": order_no}, thread_config(thread_id))


def find_orders_to_review() -> List[str]:
    """List the orders that need the workflow: new or corrected ones, plus any that were
    reviewed without the AI (because it was unreachable) now that the AI is available.

    Returns:
        Order numbers.
    """
    order_nos = [
        order.order_no
        for order in order_store.get_active_orders([OrderStatus.NEW.value])
    ]
    if is_ai_configured():
        order_nos += [
            order.order_no
            for order in order_store.get_active_orders(REVIEWABLE_STATUSES)
            if not order.ai_used and not order.reviewer_decision
        ]
    return order_nos


def review_new_orders() -> int:
    """Start the workflow for every order that needs it.

    Returns:
        How many orders were reviewed.
    """
    order_nos = find_orders_to_review()
    for order_no in order_nos:
        start_order_review(order_no)
    if order_nos:
        logger.info("Reviewed %d order(s)", len(order_nos))
    return len(order_nos)


def check_decision_allowed(
    order, findings: List[RuleFinding], decision: str, reviewer: str, comment: str
) -> None:
    """Make sure a reviewer's decision is allowed, with a clear message if not.

    Args:
        order: The order (None if it isn't active any more).
        findings: Its hard-rule findings.
        decision: A ReviewerDecision value.
        reviewer: Name of the MfgOps user.
        comment: The reviewer's comment.

    Returns:
        None. Raises ValueError explaining what's wrong.
    """
    if not reviewer.strip():
        raise ValueError("Please enter your name in the sidebar first.")
    if order is None or order.status not in REVIEWABLE_STATUSES:
        raise ValueError("This order isn't waiting for a decision any more.")
    if decision == ReviewerDecision.APPROVE.value:
        blocker = find_release_blocker(findings)
        if blocker:
            raise ValueError(f"This order can never be released: {blocker}")
        if order.ai_verdict != Verdict.APPROVE.value and not comment.strip():
            raise ValueError(
                "The AI didn't suggest releasing this order. Please add a comment."
            )
    if decision == ReviewerDecision.SEND_TO_OM.value and not comment.strip():
        raise ValueError("Please write what OM should correct.")


def submit_reviewer_decision(
    order_no: str, decision: str, reviewer: str, comment: Optional[str] = ""
) -> None:
    """Resume a paused workflow with the reviewer's decision.

    Args:
        order_no: Fusion order number.
        decision: A ReviewerDecision value.
        reviewer: Name of the MfgOps user.
        comment: The reviewer's comment (required in some cases).

    Returns:
        None. Raises ValueError if the decision isn't allowed.
    """
    comment = (comment or "").strip()
    order = order_store.get_order(order_no)
    check_decision_allowed(
        order, order_store.get_rule_findings(order_no), decision, reviewer, comment
    )
    config = thread_config(order.review_thread_id)
    if "wait_for_reviewer" not in get_workflow().get_state(config).next:
        raise ValueError("This order isn't waiting for a decision any more.")
    if decision == ReviewerDecision.SEND_TO_OM.value:
        # Send the email before moving the workflow on, so a failed email changes
        # nothing and the reviewer can simply try again. The workflow step then sees
        # it was sent (same idempotency key) and doesn't send it twice.
        try:
            email_om_team(
                order, order_store.get_rule_findings(order_no), reviewer, comment
            )
        except MailNotSentError as error:
            raise ValueError(f"{error} Nothing was changed; please try again.")
    get_workflow().invoke(
        Command(
            resume={
                "decision": decision,
                "reviewer": reviewer.strip(),
                "comment": comment,
            }
        ),
        config,
    )


def confirm_release_from_report(order_no: str, uspo: str) -> bool:
    """Finish an approved order's release now that Fusion's USPO showed up in a report.

    Args:
        order_no: Fusion order number.
        uspo: The USPO from the backlog report.

    Returns:
        True if the order was approved and is now released; False if it wasn't waiting.
    """
    order = order_store.get_order(order_no)
    if order is None or order.status != OrderStatus.APPROVED.value:
        return False
    config = thread_config(order.review_thread_id or "")
    if (
        order.review_thread_id
        and "wait_for_uspo" in get_workflow().get_state(config).next
    ):
        get_workflow().invoke(Command(resume={"uspo": uspo}), config)
    else:
        # The saved workflow is gone (e.g. checkpoints were cleared): close it directly.
        order_store.mark_released(
            order_no, uspo=uspo, released_by=order.decided_by or ""
        )
    logger.info("%s released in Fusion (USPO %s)", order_no, uspo)
    return True
