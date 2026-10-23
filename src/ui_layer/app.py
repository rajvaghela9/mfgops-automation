"""MfgOps Order Review (Streamlit), written for MfgOps users, not IT.

Run from the repo root with `make ui` (python -m streamlit run src/ui_layer/app.py).
Tabs: Dashboard, Orders to Review, Order Tracker, Rules Assistant, EUS Approval.
"""

import base64
import json
import os
import re
import uuid
from datetime import datetime, timezone
from html import escape, unescape
from pathlib import Path
from typing import Optional

import streamlit as st
from dotenv import load_dotenv

from src.app import prepare_database, run_pipeline_once
from src.graph_attachments import (
    GraphAttachmentRetriever,
    MAIL_METADATA_FILENAME,
    acquire_access_token,
    load_configuration as load_graph_configuration,
)
from src.intelligence_layer import eus_document_review, rules_assistant
from src.intelligence_layer.order_review_workflow import submit_reviewer_decision
from src.intelligence_layer.rule_checks import find_release_blocker
from src.send_attachment import (
    load_send_configuration,
    send_attachment,
    send_attachments,
)
from src.knowledge_layer import (
    attachment_store,
    eus_document_store,
    learning_request_store,
    mail_attachment_store,
    mail_store,
    order_store,
    reference_store,
    rule_store,
    special_instruction_store,
)
from src.utils.config import load_config
from src.utils.models import (
    OUTCOME_LABELS,
    REVIEWABLE_STATUSES,
    ROUTE_LABELS,
    Order,
    OrderStatus,
    ReviewerDecision,
    RuleOutcome,
    RuleType,
    is_dummy_order,
)

VERDICT_LABELS = {
    "approve": "✅ Release to Arrow",
    "reject": "⛔ Send back to OM",
    "hold": "⏸️ Hold — needs a check",
}
DECISION_LABELS = {
    ReviewerDecision.APPROVE.value: "Approved",
    ReviewerDecision.SEND_TO_OM.value: "Sent to OM",
    ReviewerDecision.HOLD.value: "On hold",
}
STATUS_LABELS = {
    OrderStatus.NEW.value: "Being checked",
    OrderStatus.WAITING_FOR_REVIEW.value: "Waiting for your review",
    OrderStatus.ON_HOLD.value: "On hold",
    OrderStatus.SENT_TO_OM.value: "Sent to OM — waiting for correction",
    OrderStatus.APPROVED.value: "Approved — waiting for the USPO from Fusion",
    OrderStatus.RELEASED.value: "Released to Arrow",
    OrderStatus.REMOVED.value: "No longer in the Fusion backlog",
}
# A backlog report is expected about every 15 minutes; warn after three missed ones.
REPORT_LATE_MINUTES = 45
FINDING_STYLES = {
    RuleOutcome.REJECT.value: (st.error, "⛔"),
    RuleOutcome.HOLD.value: (st.warning, "⏸️"),
    RuleOutcome.WARN.value: (st.info, "⚠️"),
}
# Where EUS decisions are forwarded while the EUS workstream is a POC.
EUS_FORWARD_RECIPIENT = "nkp@infoblox.com"
EUS_IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".gif", ".bmp", ".webp"}
EUS_TEXT_SUFFIXES = {".txt", ".csv", ".log", ".json", ".xml"}


def label(labels: dict, value: Optional[str]) -> str:
    """Look up the friendly name of a stored value.

    Args:
        labels: Value-to-name mapping.
        value: The stored value.

    Returns:
        The friendly name, the value itself if unknown, or "—" if empty.
    """
    return labels.get(value, value) if value else "—"


def minutes_since(timestamp: Optional[str]) -> Optional[float]:
    """Work out how long ago an ISO-8601 time was.

    Args:
        timestamp: e.g. "2026-10-19T11:33:17Z" or "2026-10-19T11:33:17+0000".

    Returns:
        Minutes since then, or None if the time is empty or unreadable.
    """
    if not timestamp:
        return None
    text = timestamp.replace("Z", "+00:00")
    if text.endswith("+0000"):
        text = text[:-5] + "+00:00"
    try:
        moment = datetime.fromisoformat(text)
    except ValueError:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - moment).total_seconds() / 60


def show_order_number(order_no: str) -> str:
    """Show an order number, marking dummy (demo) orders.

    Args:
        order_no: Order number.

    Returns:
        The number, with "🧪 " in front of dummy orders.
    """
    return f"🧪 {order_no}" if is_dummy_order(order_no) else order_no


def show_flash_messages() -> None:
    """Show the outcome of the last button press (saved before the page redrew).

    Returns:
        None.
    """
    if "flash_message" in st.session_state:
        st.success(st.session_state.pop("flash_message"))
    if "flash_error" in st.session_state:
        st.error(st.session_state.pop("flash_error"))


def describe_sync(sync: dict, reviewed: int) -> str:
    """Summarize one check for new orders in plain words.

    Args:
        sync: Counts from sync_orders.
        reviewed: How many orders the review workflow checked.

    Returns:
        A short sentence.
    """
    if not sync.get("new_report", 1):
        return "No new backlog report mail since the last check."
    parts = [
        f"{sync['new']} new and {sync['updated']} corrected order(s) found, "
        f"{reviewed} reviewed."
    ]
    if sync.get("release_confirmed"):
        parts.append(
            f"{sync['release_confirmed']} release(s) confirmed by Fusion's USPO."
        )
    if sync.get("released_outside_tool"):
        parts.append(
            f"{sync['released_outside_tool']} order(s) were released in Fusion "
            "without a decision here."
        )
    if sync.get("removed_from_backlog"):
        parts.append(
            f"{sync['removed_from_backlog']} order(s) left the Fusion backlog and "
            "were closed."
        )
    return " ".join(parts)


def render_mail_sync_status() -> None:
    """Show when the last Fusion backlog report mail arrived and whether it was read.

    Returns:
        None.
    """
    status = mail_store.get_mail_sync_status()
    last = status["last_processed"]
    if last is None:
        st.sidebar.caption("No backlog report mail read yet.")
    else:
        age = minutes_since(last["received_at"]) or 0
        st.sidebar.caption(
            f"Last backlog report: {last['received_at'].replace('T', ' ')[:16]} UTC "
            f"({age:.0f} min ago), {last['order_count']} orders"
        )
        if age > REPORT_LATE_MINUTES:
            st.sidebar.warning(
                f"⚠️ No new backlog report for {age:.0f} minutes (Fusion sends one about "
                "every 15). Check the mailbox."
            )
    if status["last_error"]:
        st.sidebar.error(f"The newest report couldn't be read: {status['last_error']}")


def render_sidebar() -> str:
    """Show who is reviewing and the button that checks for new orders.

    Returns:
        The reviewer's name ("" until entered).
    """
    config = load_config()
    st.sidebar.header("👤 Reviewer")
    reviewer = st.sidebar.text_input(
        "Your name (saved with every decision)", key="reviewer"
    )
    st.sidebar.header("📥 New orders")
    if st.sidebar.button("Check for new orders", width="stretch"):
        with st.spinner("Reading the backlog and reviewing new orders..."):
            try:
                summary = run_pipeline_once()
                st.sidebar.success(describe_sync(summary["sync"], summary["reviewed"]))
            except Exception as error:
                st.sidebar.error(f"Something went wrong: {error}")
    dummies = " + 5 dummy orders (🧪)" if config.include_demo_orders else ""
    if config.fusion_mode == "mail":
        source = f"Fusion backlog report mails to {config.graph_api_mailbox}{dummies}"
    elif config.fusion_mode == "file":
        source = (
            f"backlog report “{Path(config.backlog_file_path or '').name}”{dummies}"
        )
    elif config.fusion_mode == "mock":
        source = "5 dummy orders only (test mode)"
    else:
        source = "Fusion"
    st.sidebar.caption(f"Orders come from: {source}")
    if config.fusion_mode == "mail":
        render_mail_sync_status()
    mail_target = config.email_redirect_to or config.om_team_email or "not set"
    st.sidebar.caption(
        "Emails: "
        + (
            f"sent from {config.sender_mailbox} to {mail_target}"
            if config.email_mode == "live"
            else "saved to the outbox folder (test mode)"
        )
    )
    st.sidebar.caption(
        "AI review: " + ("on" if config.llm_enabled else "off — only fixed checks run")
    )
    return reviewer.strip()


def render_dashboard_tab() -> None:
    """Show the business value: orders handled, AI accuracy and time saved.

    Returns:
        None.
    """
    config = load_config()
    numbers = order_store.get_dashboard_numbers(config.minutes_saved_per_order)

    def as_percent(value: Optional[float]) -> str:
        """Format a 0-1 rate as a percentage. Returns: e.g. "85%" or "—"."""
        return "—" if value is None else f"{value:.0%}"

    columns = st.columns(5)
    columns[0].metric("Orders received", numbers["orders_seen"])
    columns[1].metric("Released to Arrow", numbers["released"])
    columns[2].metric("Approved, waiting for USPO", numbers["approved_waiting"])
    columns[3].metric("Sent back to OM", numbers["sent_to_om"])
    columns[4].metric("Waiting or on hold", numbers["waiting"])
    columns = st.columns(3)
    columns[0].metric("AI and reviewer agreed", as_percent(numbers["agreement_rate"]))
    columns[1].metric(
        "Released first time (no rework)", as_percent(numbers["first_time_right_rate"])
    )
    columns[2].metric("Hours saved (estimate)", f"{numbers['hours_saved']:.1f}")
    st.caption(
        f"Hours saved assumes {config.minutes_saved_per_order:g} minutes of manual "
        "checking per order reviewed by the AI."
    )
    left, right = st.columns(2)
    with left:
        st.subheader("Orders by category")
        if numbers["by_route"]:
            st.bar_chart(
                {label(ROUTE_LABELS, k): v for k, v in numbers["by_route"].items()},
                horizontal=True,
            )
        else:
            st.info("No orders yet. Use “Check for new orders” in the sidebar.")
    with right:
        st.subheader("Where orders are now")
        if numbers["by_status"]:
            st.bar_chart(
                {label(STATUS_LABELS, k): v for k, v in numbers["by_status"].items()},
                horizontal=True,
            )


def handle_decision(order_no: str, decision: str, reviewer: str) -> None:
    """Carry out a reviewer's button press (runs before the page redraws).

    Args:
        order_no: Fusion order number.
        decision: A ReviewerDecision value.
        reviewer: Name of the MfgOps user.

    Returns:
        None. The outcome is kept for the next redraw to show.
    """
    comment = st.session_state.get(f"comment_{order_no}", "")
    order = order_store.get_order(order_no)
    try:
        submit_reviewer_decision(order_no, decision, reviewer, comment)
    except ValueError as error:
        st.session_state["flash_error"] = f"{order_no}: {error}"
        return
    approved = (
        f"{order_no} (dummy order) was approved and closed."
        if is_dummy_order(order_no)
        else f"{order_no} is approved. It stays listed as “Approved — waiting for the "
        "USPO” until Fusion's USPO for it shows up in a backlog report mail."
    )
    message = {
        ReviewerDecision.APPROVE.value: approved,
        ReviewerDecision.SEND_TO_OM.value: f"{order_no} was sent back to OM by email.",
        ReviewerDecision.HOLD.value: f"{order_no} is on hold.",
    }[decision]
    if order_store.is_agreement(order.ai_verdict, decision) is False:
        message += " The AI would like to learn from this — see 💬 Rules Assistant."
    st.session_state["flash_message"] = message


def render_order_details(order: Order) -> None:
    """Show the order's shipping details and lines.

    Args:
        order: The order.

    Returns:
        None.
    """
    left, right = st.columns(2)
    left.markdown(
        f"**Customer (bill to):** {order.bill_to_customer}  \n"
        f"**Ship to:** {order.ship_to_customer}, {order.ship_to_address}, "
        f"{order.ship_to_country}  \n"
        f"**Ship from:** {order.ship_from or '—'}  \n"
        f"**Booked:** {order.booked_date}"
    )
    right.markdown(
        f"**Customer PO:** {order.customer_po or '—'}  \n"
        f"**Shipping method:** {order.shipping_method or '—'}  \n"
        f"**Incoterm (FOB):** {order.fob or '—'}  \n"
        f"**Freight terms:** {order.freight_terms or '—'}"
    )
    st.markdown(f"**Shipping instructions:** {order.shipping_instructions or '—'}")
    st.dataframe(
        [
            {
                "Line": line.line_number,
                "Item": line.sku,
                "Description": line.item_description,
                "Qty": line.ordered_qty,
            }
            for line in order.lines
        ],
        hide_index=True,
        width="stretch",
    )
    st.caption(f"Why this category: {order.route_reason}")


def render_decision_buttons(
    order: Order, blocker: Optional[str], reviewer: str
) -> None:
    """Show the comment box and the Approve / Send to OM / Hold buttons.

    Args:
        order: The order under review.
        blocker: Why the order can never be released, or None.
        reviewer: Name of the MfgOps user ("" disables the buttons).

    Returns:
        None.
    """
    with st.form(key=f"decision_{order.order_no}"):
        st.text_area(
            "Your comment (needed when sending to OM or disagreeing with the AI)",
            key=f"comment_{order.order_no}",
            placeholder="e.g. EUS already sent to Arrow by email / Please add 2 UK power cords",
        )
        approve, send, hold = st.columns(3)
        approve_help = (
            "Dummy order: approving closes it straight away."
            if is_dummy_order(order.order_no)
            else "Release the order in Fusion first (Priority Release), then click "
            "Approve. It stays listed until Fusion's USPO shows up in a backlog report."
        )
        buttons = [
            (
                approve,
                "✅ Approve (released in Fusion)",
                ReviewerDecision.APPROVE.value,
                bool(blocker),
                approve_help,
            ),
            (
                send,
                "📧 Send to OM",
                ReviewerDecision.SEND_TO_OM.value,
                False,
                "Emails OM your comment and the order details.",
            ),
            (hold, "⏸️ Put on hold", ReviewerDecision.HOLD.value, False, None),
        ]
        for column, text, decision, blocked, help_text in buttons:
            column.form_submit_button(
                text,
                disabled=not reviewer or blocked,
                width="stretch",
                help=help_text,
                on_click=handle_decision,
                args=(order.order_no, decision, reviewer),
            )
    if not reviewer:
        st.caption("Enter your name in the sidebar to enable the buttons.")


def render_order_card(order: Order, reviewer: str) -> None:
    """Show one order with the AI's verdict, what was found, and the decision buttons.

    Args:
        order: The order under review.
        reviewer: Name of the MfgOps user.

    Returns:
        None.
    """
    findings = order_store.get_rule_findings(order.order_no)
    blocker = find_release_blocker(findings)
    title = " · ".join(
        [
            f"{VERDICT_LABELS.get(order.ai_verdict, '').split()[0]} "
            + show_order_number(order.order_no),
            label(ROUTE_LABELS, order.route),
            order.ship_to_country,
            order.order_type,
            label(STATUS_LABELS, order.status),
        ]
    )
    with st.expander(title):
        st.markdown(f"#### AI suggests: {label(VERDICT_LABELS, order.ai_verdict)}")
        if order.ai_summary:
            st.write(order.ai_summary)
        for reason in order.ai_reasons:
            st.markdown(f"- {reason}")
        if is_dummy_order(order.order_no):
            st.caption(
                "🧪 Dummy order, made up to show the AI rejecting or holding an order. "
                "It is not in Fusion."
            )
        if blocker:
            st.error(f"🔒 This order can never be released: {blocker}")
        if findings:
            st.markdown("**Fixed checks that flagged this order**")
            for finding in findings:
                show, icon = FINDING_STYLES.get(finding.outcome, (st.info, "•"))
                show(f"{icon} **{finding.rule_name}** — {finding.message}")
        st.divider()
        render_order_details(order)
        render_decision_buttons(order, blocker, reviewer)


def render_review_tab(reviewer: str) -> None:
    """List every order waiting for a decision, held orders included.

    Args:
        reviewer: Name of the MfgOps user.

    Returns:
        None.
    """
    show_flash_messages()
    orders = order_store.get_active_orders(REVIEWABLE_STATUSES)
    render_waiting_for_uspo()
    if not orders:
        st.info("Nothing to review. Use “Check for new orders” in the sidebar.")
        return
    without_ai = [order.order_no for order in orders if not order.ai_used]
    if without_ai:
        st.warning(
            f"⚠️ {len(without_ai)} order(s) were checked by the fixed rules only, because "
            "the AI could not be reached (see each order's summary for the reason). "
            "Once the connection works, click “Check for new orders” to review "
            "them again with the AI."
        )
    categories = st.multiselect(
        "Show categories",
        list(ROUTE_LABELS),
        default=list(ROUTE_LABELS),
        format_func=lambda route: ROUTE_LABELS[route],
    )
    st.caption(
        "✅ AI suggests release · ⛔ AI suggests sending to OM · ⏸️ AI suggests a hold"
    )
    for order in orders:
        if order.route in categories:
            render_order_card(order, reviewer)


def render_waiting_for_uspo() -> None:
    """List approved orders still waiting for Fusion's USPO (no buttons: Fusion decides).

    Returns:
        None.
    """
    approved = order_store.get_active_orders([OrderStatus.APPROVED.value])
    if not approved:
        return
    warning_minutes = load_config().uspo_wait_warning_minutes
    late = [
        order.order_no
        for order in approved
        if (minutes_since(order.approved_at) or 0) > warning_minutes
    ]
    with st.expander(
        f"⏳ Approved — waiting for the USPO from Fusion ({len(approved)})",
        expanded=bool(late),
    ):
        st.caption(
            "These orders were approved and released in Fusion. Each one leaves this list "
            "once its USPO shows up in a Fusion backlog report mail."
        )
        if late:
            st.warning(
                f"No USPO after {warning_minutes} minutes for: {', '.join(late)}. "
                "Check the release in Fusion."
            )
        st.dataframe(
            [
                {
                    "Order": order.order_no,
                    "Category": label(ROUTE_LABELS, order.route),
                    "Country": order.ship_to_country,
                    "Approved by": order.decided_by or "—",
                    "Approved (UTC)": (order.approved_at or "")[:16].replace("T", " "),
                    "Not in the last report": (
                        "⚠️ check Fusion" if order.missing_snapshot_count else ""
                    ),
                }
                for order in approved
            ],
            hide_index=True,
            width="stretch",
        )


def render_tracker_tab() -> None:
    """Show where every active order is, and the full record of all orders.

    Returns:
        None.
    """
    st.subheader("Active orders")
    active = order_store.get_active_orders()
    if active:
        st.dataframe(
            [
                {
                    "Order": show_order_number(order.order_no),
                    "Category": label(ROUTE_LABELS, order.route),
                    "Country": order.ship_to_country,
                    "Status": label(STATUS_LABELS, order.status),
                    "AI suggested": label(VERDICT_LABELS, order.ai_verdict),
                    "Last decision": label(DECISION_LABELS, order.reviewer_decision),
                    "Corrections by OM": order.rework_count,
                }
                for order in active
            ],
            hide_index=True,
            width="stretch",
        )
    else:
        st.info("No active orders.")
    st.subheader("All orders")
    search = st.text_input("Search by order number or country")
    rows = order_store.get_all_orders(search)
    if rows:
        st.dataframe(
            [
                {
                    "Order": show_order_number(row["order_no"]),
                    "Category": label(ROUTE_LABELS, row["route"]),
                    "Country": row["ship_to_country"],
                    "Status": label(STATUS_LABELS, row["status"]),
                    "AI suggested": label(VERDICT_LABELS, row["ai_verdict"]),
                    "Reviewer decided": label(
                        DECISION_LABELS, row["reviewer_decision"]
                    ),
                    "Reviewed by": row["decided_by"] or "—",
                    "Comment": row["reviewer_comment"] or "",
                    "Released on": (row["released_at"] or "")[:10],
                    "USPO": row["uspo"] or "",
                }
                for row in rows
            ],
            hide_index=True,
            width="stretch",
        )
    else:
        st.info("No orders match.")
    render_sent_emails()


def render_sent_emails() -> None:
    """Show the emails the service sent most recently (to OM and Arrow).

    Returns:
        None.
    """
    sent = mail_store.get_recent_outbound()
    if not sent:
        return
    labels = {
        mail_store.OM_CORRECTION: "To OM",
        mail_store.ARROW_NOTICE: "To Arrow",
    }
    states = {
        mail_store.SENT: "✅ Sent",
        mail_store.MOCK: "📁 Saved to outbox (test mode)",
        mail_store.FAILED: "❌ Failed",
    }
    with st.expander(f"📧 Emails sent ({len(sent)} most recent)"):
        st.dataframe(
            [
                {
                    "When (UTC)": (row["processed_at"] or "")[:16].replace("T", " "),
                    "Order": row["order_no"],
                    "Type": labels.get(row["kind"], row["kind"]),
                    "To": row["recipient"],
                    "Subject": row["subject"],
                    "Result": states.get(row["status"], row["status"]),
                    "Error": row["error"] or "",
                }
                for row in sent
            ],
            hide_index=True,
            width="stretch",
        )


def handle_start_learning_chat(request_id: int, reviewer: str) -> None:
    """Open the chat where the AI asks about a decision (runs before redraw).

    Args:
        request_id: The learning request's id.
        reviewer: Name of the MfgOps user.

    Returns:
        None.
    """
    st.session_state["chat_thread_id"] = rules_assistant.start_learning_chat(
        request_id, reviewer
    )


def handle_new_chat() -> None:
    """Start a fresh Rules Assistant conversation (runs before redraw).

    Returns:
        None.
    """
    st.session_state["chat_thread_id"] = f"rules-chat-{uuid.uuid4().hex[:8]}"


def render_learning_requests(reviewer: str) -> None:
    """Show the decisions the AI would like to learn from.

    Args:
        reviewer: Name of the MfgOps user.

    Returns:
        None.
    """
    requests = learning_request_store.get_open_learning_requests()
    if not requests:
        return
    st.warning(f"🎓 The AI would like to learn from {len(requests)} of your decisions.")
    for request in requests:
        text, talk, dismiss = st.columns([4, 1, 1])
        decision = label(DECISION_LABELS, request["reviewer_decision"])
        text.write(
            f"**{request['order_no']}** — AI suggested "
            f"{label(VERDICT_LABELS, request['ai_verdict'])}, "
            f"{request['decided_by']} chose “{decision}”."
        )
        talk.button(
            "Talk about it",
            key=f"learn_{request['id']}",
            disabled=not reviewer,
            on_click=handle_start_learning_chat,
            args=(request["id"], reviewer),
        )
        dismiss.button(
            "Dismiss",
            key=f"dismiss_{request['id']}",
            on_click=learning_request_store.close_learning_request,
            args=(request["id"],),
        )


def render_chat(reviewer: str) -> None:
    """Show the Rules Assistant conversation, the Save/Don't save buttons, or the input box.

    Args:
        reviewer: Name of the MfgOps user.

    Returns:
        None.
    """
    if "chat_thread_id" not in st.session_state:
        handle_new_chat()
    thread_id = st.session_state["chat_thread_id"]
    messages, change_waiting = rules_assistant.get_conversation(thread_id)
    if not messages:
        st.chat_message("assistant").write(
            "Hi! I can show, add, change or remove the rules used to check orders. "
            "Try: “Orders to Saudi Arabia need a commercial invoice”, “Show me the power "
            "cord rules”, or “Add Russia to the embargoed countries”."
        )
    for message in messages:
        st.chat_message(message["role"]).markdown(message["content"])
    if change_waiting:
        with st.chat_message("assistant"):
            st.info(change_waiting)
            yes, no = st.columns(2)
            if yes.button("✅ Yes, save it", width="stretch"):
                with st.spinner("Saving..."):
                    rules_assistant.answer_change_request(thread_id, True)
                st.rerun()
            if no.button("✖️ No, don't save", width="stretch"):
                with st.spinner("..."):
                    rules_assistant.answer_change_request(thread_id, False)
                st.rerun()
        return
    if not reviewer:
        st.caption("Enter your name in the sidebar to chat.")
        return
    text = st.chat_input("Describe a rule to add, change or remove…")
    if text:
        st.chat_message("user").markdown(text)
        with st.spinner("Thinking..."):
            rules_assistant.send_chat_message(thread_id, text, reviewer)
        st.rerun()


def remember_rules_at_start() -> None:
    """Note the rules and list entries as they were when this browser session began.

    Anything different later is shown as new or changed.

    Returns:
        None.
    """
    if "rules_at_start" in st.session_state:
        return
    st.session_state["rules_at_start"] = {
        rule.id: rule.version for rule in rule_store.get_rules()
    }
    st.session_state["list_entries_at_start"] = {
        name: set(entries)
        for name, entries in reference_store.get_all_reference_lists().items()
    }


def mark_rule_change(rule) -> str:
    """Say whether a rule is new or changed since this session began.

    Args:
        rule: The rule.

    Returns:
        "🆕 New", "✏️ Changed", or "".
    """
    version_at_start = st.session_state["rules_at_start"].get(rule.id)
    if version_at_start is None:
        return "🆕 New"
    return "✏️ Changed" if rule.version > version_at_start else ""


def render_rules_and_lists() -> None:
    """Show the current rules and lists in plain words (changed only through the chat).

    They are read fresh on every page refresh, so a saved change appears straight away;
    new and changed items are marked, and the section opens when something changed.

    Returns:
        None.
    """
    rules = rule_store.get_rules()
    marks = {rule.id: mark_rule_change(rule) for rule in rules}
    lists = reference_store.get_all_reference_lists()
    entries_at_start = st.session_state["list_entries_at_start"]
    new_entries = {
        name: set(entries) - entries_at_start.get(name, set())
        for name, entries in lists.items()
    }
    with st.expander("📚 Current business rules", expanded=any(marks.values())):
        st.dataframe(
            [
                {
                    "": marks[rule.id],
                    "No.": rule.id,
                    "Rule": rule.name,
                    "What it checks": rule.description,
                    "Checked by": (
                        "Fixed check"
                        if rule.rule_type == RuleType.HARD.value
                        else "AI judgement"
                    ),
                    "Applies to": rules_assistant.describe_where_rule_applies(rule),
                    "If a problem is found": label(OUTCOME_LABELS, rule.outcome),
                    "Can override": "Yes" if rule.can_be_overridden else "No",
                    "Source": rule.source,
                }
                for rule in rules
            ],
            hide_index=True,
            width="stretch",
        )
    with st.expander("📋 Lists used by the rules", expanded=any(new_entries.values())):
        for list_name, list_label in reference_store.LIST_LABELS.items():
            entries = lists.get(list_name, {})
            st.markdown(f"**{list_label}**")
            st.write(
                ", ".join(
                    ("🆕 " if key in new_entries.get(list_name, set()) else "")
                    + f"{key} ({value})"
                    for key, value in entries.items()
                )
                or "—"
            )


def render_rules_assistant_tab(reviewer: str) -> None:
    """Show learning requests, the chat, and the current rules and lists.

    Args:
        reviewer: Name of the MfgOps user.

    Returns:
        None.
    """
    render_learning_requests(reviewer)
    top_left, top_right = st.columns([4, 1])
    top_left.caption("Chat with the AI to look up, add, change or remove rules.")
    top_right.button("Start new chat", on_click=handle_new_chat, width="stretch")
    render_chat(reviewer)
    st.divider()
    render_rules_and_lists()


def eus_attachment_dir() -> Path:
    """Find the folder the mailbox downloader saves EUS attachments in.

    Returns:
        The configured attachment folder (defaults to "attachments").
    """
    load_dotenv(dotenv_path=Path.cwd() / ".env")
    return (
        Path(os.environ.get("ATTACHMENT_OUTPUT_DIR", "attachments"))
        .expanduser()
        .resolve()
    )


def list_eus_attachments() -> list:
    """List the EUS attachments downloaded from the shared mailbox.

    Returns:
        The attachment files, newest first.
    """
    folder = eus_attachment_dir()
    attachment_store.migrate_files(folder)
    attachment_store.materialize_attachments(folder)
    eus_document_store.sync_documents()
    if not folder.is_dir():
        return []
    files = [
        path
        for path in folder.rglob("*")
        if path.is_file()
        and not any(part.startswith(".") for part in path.relative_to(folder).parts)
    ]
    return sorted(files, key=lambda path: path.stat().st_mtime, reverse=True)


def handle_eus_fetch() -> None:
    """Download the newest EUS attachments from the shared mailbox.

    Returns:
        None. The outcome is kept for the next redraw to show.
    """
    output_dir = None
    known = {
        path.resolve().relative_to(eus_attachment_dir()).as_posix()
        for path in list_eus_attachments()
    }
    try:
        # This tab can run before anything else has read .env.
        load_dotenv()
        config = load_graph_configuration()
        access_token = acquire_access_token(
            config["tenant_id"], config["client_id"], config["client_secret"]
        )
        output_dir = Path(config["output_dir"]).expanduser().resolve()
        retriever = GraphAttachmentRetriever(access_token)
        downloaded = []
        for sender_email in config["sender_emails"]:
            processed_message_ids = (
                mail_attachment_store.get_processed_message_ids(
                    config["mailbox"], sender_email
                )
            )
            downloaded += retriever.download_sender_attachments(
                config["mailbox"],
                sender_email,
                output_dir,
                processed_message_ids=processed_message_ids,
                on_message_processed=lambda message, sender=sender_email: (
                    mail_attachment_store.record_processed_message(
                        message["id"],
                        config["mailbox"],
                        sender,
                        message["receivedDateTime"],
                    )
                ),
            )
    except Exception as error:
        st.session_state["flash_error"] = f"Could not check the mailbox: {error}"
        return
    fresh = [
        path
        for path in downloaded
        if path.resolve().relative_to(output_dir).as_posix() not in known
    ]
    reviewed = 0
    review_failures = 0
    for folder_path in sorted({path.resolve().parent for path in fresh}):
        if folder_path == output_dir:
            continue
        folder_name = folder_path.relative_to(output_dir).as_posix()
        folder_files = [
            path for path in folder_path.iterdir() if path.is_file()
        ]
        perform_eus_review(folder_name, folder_files)
        reviewed += 1
        if f"eus_automated_review_{folder_name}_error" in st.session_state:
            review_failures += 1
    if fresh:
        st.session_state["flash_message"] = (
            f"{len(fresh)} new EUS attachment(s) downloaded from the mailbox to "
            f"{output_dir}. Automated verification ran for {reviewed} email(s)"
            + (
                f" and reported {review_failures} error(s)."
                if review_failures
                else "."
            )
        )
    else:
        st.session_state["flash_message"] = (
            f"No new EUS attachments. {len(downloaded)} already downloaded "
            f"in {output_dir}."
        )


def handle_eus_decision(
    relative_path: str,
    decision: str,
    reviewer: str,
    comment_key: Optional[str] = None,
) -> None:
    """Forward one EUS attachment with the reviewer's decision and comments.

    Args:
        relative_path: Attachment path relative to the download folder.
        decision: Either "approve" or "reject".
        reviewer: Name of the MfgOps user.

    Returns:
        None. The outcome is kept for the next redraw to show.
    """
    file_name = Path(relative_path).name
    comment = st.session_state.get(
        comment_key or f"eus_comment_{relative_path}", ""
    ).strip()
    if decision == "reject" and not comment:
        st.session_state["flash_error"] = (
            f"{file_name}: add a comment before rejecting."
        )
        return
    attachment_root = eus_attachment_dir()
    attachment_path = (attachment_root / relative_path).resolve()
    if attachment_root not in attachment_path.parents:
        st.session_state["flash_error"] = f"{file_name}: invalid attachment path."
        return
    if not attachment_path.is_file():
        st.session_state["flash_error"] = f"{file_name} is no longer available."
        return
    verdict = "Approved" if decision == "approve" else "Rejected"
    body = [f"EUS review outcome: {verdict}."]
    if reviewer:
        body.append(f"Reviewed by: {reviewer}.")
    if comment:
        body += ["", "Comments:", comment]
    try:
        config = load_send_configuration()
        access_token = acquire_access_token(
            config["tenant_id"], config["client_id"], config["client_secret"]
        )
        send_attachment(
            access_token,
            config["mailbox"],
            EUS_FORWARD_RECIPIENT,
            f"EUS {verdict}: {file_name}",
            "\n".join(body),
            attachment_path,
        )
    except Exception as error:
        st.session_state["flash_error"] = f"{file_name}: could not forward — {error}"
        return
    attachment_store.record_decision(relative_path, decision, reviewer, comment)
    st.session_state["flash_message"] = (
        f"{file_name} was {verdict.lower()} and forwarded to {EUS_FORWARD_RECIPIENT}."
    )


def render_eus_preview(attachment_path: Path) -> None:
    """Show the attachment in the page so the reviewer can read it here.

    Args:
        attachment_path: The downloaded attachment.

    Returns:
        None.
    """
    suffix = attachment_path.suffix.lower()
    content = attachment_path.read_bytes()
    if suffix == ".pdf":
        encoded = base64.b64encode(content).decode("ascii")
        st.markdown(
            f'<iframe src="data:application/pdf;base64,{encoded}" width="100%" '
            'height="600" title="EUS attachment"></iframe>',
            unsafe_allow_html=True,
        )
    elif suffix in EUS_IMAGE_SUFFIXES:
        st.image(content)
    elif suffix in EUS_TEXT_SUFFIXES:
        st.text(content.decode("utf-8", errors="replace")[:5000])
    else:
        st.caption("No preview for this file type — use Download to open it.")


def mail_received_label(folder_name: str) -> Optional[str]:
    """Read the UTC mail arrival timestamp from a downloaded mail folder name.

    Args:
        folder_name: Name of the per-email folder, e.g. "20261015_121500_AAMk".

    Returns:
        A readable UTC timestamp, or None when the name carries no timestamp.
    """
    match = re.match(r"^(\d{8}_\d{6})(?:_|$)", folder_name)
    if not match:
        return None
    try:
        received_at = datetime.strptime(match.group(1), "%Y%m%d_%H%M%S").replace(
            tzinfo=timezone.utc
        )
    except ValueError:
        return None
    return received_at.strftime("%d %b %Y, %H:%M:%S UTC")


def group_eus_attachments_by_mail(attachments: list) -> list:
    """Group downloaded attachments by the per-email folder they were saved in.

    Args:
        attachments: Attachment files, newest first.

    Returns:
        A list of (folder_name, received_label, files) tuples, newest mail first.
        Files saved before per-email folders existed are grouped last under a
        None folder name.
    """
    root = eus_attachment_dir()
    groups: dict = {}
    for path in attachments:
        parts = path.relative_to(root).parts
        folder_name = parts[0] if len(parts) > 1 else None
        groups.setdefault(folder_name, []).append(path)
    mail_groups = [
        (folder_name, mail_received_label(folder_name or ""), files)
        for folder_name, files in groups.items()
        if folder_name is not None
    ]
    mail_groups.sort(key=lambda group: group[0], reverse=True)
    if None in groups:
        mail_groups.append((None, None, groups[None]))
    return mail_groups


def load_mail_metadata(folder_name: str) -> Optional[dict]:
    """Read the saved subject and body for one downloaded email."""
    root = eus_attachment_dir()
    folder = (root / folder_name).resolve()
    if root not in folder.parents:
        return None
    metadata_path = folder / MAIL_METADATA_FILENAME
    if not metadata_path.is_file():
        return None
    try:
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return metadata if isinstance(metadata, dict) else None


def mail_body_preview(metadata: dict) -> str:
    """Convert an HTML or text email body into readable plain text for the UI."""
    body = str(metadata.get("body") or "")
    if str(metadata.get("body_content_type") or "").lower() == "html":
        body = re.sub(r"<[^>]+>", " ", body)
        body = unescape(body)
    return re.sub(r"\s+", " ", body).strip()


def handle_special_instruction_share(folder_name: str, reviewer: str) -> None:
    """Share an email body with its PO, Quote, and approved EUS attachments."""
    root = eus_attachment_dir()
    folder = (root / folder_name).resolve()
    if root not in folder.parents or not folder.is_dir():
        st.session_state["flash_error"] = "The email folder is no longer available."
        return
    metadata = load_mail_metadata(folder_name)
    if not metadata or not str(metadata.get("body") or "").strip():
        st.session_state["flash_error"] = (
            "The original email body is unavailable, so its special instructions "
            "cannot be shared."
        )
        return
    review = st.session_state.get(f"eus_automated_review_{folder_name}")
    if review is None:
        st.session_state["flash_error"] = (
            "Run the automated document classification before sharing this email."
        )
        return
    files = sorted(
        (
            path
            for path in folder.iterdir()
            if path.is_file() and not path.name.startswith(".")
        ),
        key=lambda path: path.name.lower(),
    )
    eus_files = [
        path
        for path in files
        if review.document_types.get(path.name) == "eus"
    ]
    unapproved = [
        path.name
        for path in eus_files
        if (
            attachment_store.get_decision(path.relative_to(root).as_posix()) or {}
        ).get("decision")
        != "approve"
    ]
    if unapproved:
        st.session_state["flash_error"] = (
            "Approve the EUS before sharing the special instructions: "
            + ", ".join(unapproved)
        )
        return
    # The EUS is forwarded on its own when approved; instructions go with PO/Quote.
    files = [
        path
        for path in files
        if review.document_types.get(path.name) in {"po", "quote"}
    ]
    if not files:
        st.session_state["flash_error"] = (
            "No PO or Quote was identified to share with the special instructions."
        )
        return
    body_type = str(metadata.get("body_content_type") or "text")
    original_body = str(metadata.get("body") or "")
    if body_type.lower() == "html":
        body = (
            f"<p><strong>Shared by {escape(reviewer or 'MfgOps reviewer')}.</strong></p>"
            f"<hr>{original_body}"
        )
    else:
        body = (
            f"Shared by {reviewer or 'MfgOps reviewer'}.\n\n"
            f"{original_body}"
        )
    subject = str(metadata.get("subject") or "Special instructions")
    try:
        config = load_send_configuration()
        access_token = acquire_access_token(
            config["tenant_id"], config["client_id"], config["client_secret"]
        )
        send_attachments(
            access_token,
            config["mailbox"],
            EUS_FORWARD_RECIPIENT,
            f"Special instructions: {subject}",
            body,
            files,
            body_content_type=body_type,
        )
    except Exception as error:
        st.session_state["flash_error"] = (
            f"Could not share the special instructions: {error}"
        )
        return
    special_instruction_store.record_share(
        folder_name, EUS_FORWARD_RECIPIENT, reviewer
    )
    st.session_state["flash_message"] = (
        f"Special instructions and {len(files)} attachment(s) were shared with "
        f"{EUS_FORWARD_RECIPIENT}."
    )


def render_special_instruction_share(
    folder_name: str,
    review: Optional[eus_document_review.EusReview],
    reviewer: str,
) -> None:
    """Show the original instructions and their mail-level sharing action."""
    metadata = load_mail_metadata(folder_name)
    if not metadata or not str(metadata.get("body") or "").strip():
        return
    st.markdown("#### Special instructions")
    subject = str(metadata.get("subject") or "(no subject)")
    sender = str(metadata.get("sender_email") or "unknown sender")
    st.caption(f"Original email: {subject} · From {sender}")
    preview = mail_body_preview(metadata)
    with st.expander("View original email instructions"):
        st.write(preview or "The email body is empty.")

    shared = special_instruction_store.get_share(folder_name)
    if shared:
        st.success(
            f"Shared with {shared['recipient']} by "
            f"{shared['shared_by'] or 'unknown'} at {shared['shared_at']}."
        )
        return

    disabled_reason = ""
    if review is None:
        disabled_reason = "Run automated document classification first."
    else:
        eus_names = [
            file_name
            for file_name, document_type in review.document_types.items()
            if document_type == "eus"
        ]
        unapproved = [
            file_name
            for file_name in eus_names
            if (
                attachment_store.get_decision(
                    (Path(folder_name) / file_name).as_posix()
                )
                or {}
            ).get("decision")
            != "approve"
        ]
        if unapproved:
            disabled_reason = "Approve the EUS before sharing this package."
        elif eus_names:
            st.info(
                "The EUS is approved. Special instructions are shared with the "
                "PO and Quote only; the EUS was forwarded separately."
            )
        else:
            st.info(
                "No EUS was identified. The special instructions are shared with "
                "the PO and Quote."
            )
    st.button(
        "📤 Share special instructions",
        key=f"share_special_instructions_{folder_name}",
        disabled=not reviewer or bool(disabled_reason),
        on_click=handle_special_instruction_share,
        args=(folder_name, reviewer),
        width="stretch",
    )
    if disabled_reason:
        st.caption(disabled_reason)


def render_eus_mail_group(
    folder_name: Optional[str],
    received_label: Optional[str],
    files: list,
    reviewer: str,
    key_prefix: str = "",
) -> None:
    """Show one email as a heading with every attachment that came with it.

    Args:
        folder_name: Name of the per-email folder, or None for older downloads.
        received_label: Readable UTC arrival time, or None when unknown.
        files: Attachments saved in that folder.
        reviewer: Name of the MfgOps user ("" disables the buttons).

    Returns:
        None.
    """
    if folder_name is None:
        st.subheader("📁 Earlier downloads (not grouped by email)")
    elif received_label:
        st.subheader(f"📧 Email received {received_label}")
        st.caption(f"{len(files)} attachment(s) in {folder_name}/")
    else:
        st.subheader(f"📧 {folder_name}")
        st.caption(f"{len(files)} attachment(s)")
    review = (
        render_eus_automated_review(folder_name, files, key_prefix)
        if folder_name is not None
        else None
    )
    for attachment_path in files:
        document_type = (
            review.document_types.get(attachment_path.name) if review else None
        )
        render_eus_attachment_card(
            attachment_path, reviewer, document_type, key_prefix
        )
    if folder_name is not None and not key_prefix:
        render_special_instruction_share(folder_name, review, reviewer)
    st.divider()


def render_eus_automated_review(
    folder_name: str, files: list, key_prefix: str = ""
) -> Optional[eus_document_review.EusReview]:
    """Show automated checks for one email's document set."""
    state_key = f"eus_automated_review_{folder_name}"
    error_key = f"{state_key}_error"
    if st.button(
        "🔄 Re-run automated EUS checks",
        key=f"{key_prefix}run_{state_key}",
        width="stretch",
    ):
        perform_eus_review(folder_name, files)

    error = st.session_state.get(error_key)
    if error:
        st.error(f"Automated EUS review could not run: {error}")
        st.caption("A reviewer may still inspect the files and make a manual decision.")
        return None

    review = st.session_state.get(state_key)
    if review is None:
        st.info(
            "Automated verification has not completed for this email. Use the "
            "re-run button to try again."
        )
        return None

    failed_checks = [check for check in review.checks if not check.passed]
    status = (
        "✅ passed" if review.passed else f"❌ {len(failed_checks)} check(s) failed"
    )
    with st.expander(f"🧾 Automated EUS checks — {status}", expanded=False):
        render_eus_review_details(review, failed_checks)
    return review


def render_eus_review_details(
    review: eus_document_review.EusReview, failed_checks: list
) -> None:
    """Render the result and individual checks of one automated review."""
    confidence = f"{review.confidence:.0%}"
    if review.repeat_customer:
        st.info(
            f"🔁 Repeat customer: {review.end_user_name} already has an approved "
            f"EUS. Confidence {confidence} (needs "
            f"{eus_document_review.REPEAT_CUSTOMER_CONFIDENCE:.0%})."
        )
    if review.passed and not failed_checks:
        st.success("All automated EUS checks passed.")
    elif review.passed:
        st.success(
            f"Repeat customer confidence {confidence} meets the threshold."
        )
    else:
        st.warning(
            "One or more automated checks failed. Review the details before overriding."
        )
    if failed_checks:
        st.error(
            "**Failed checks:**\n\n"
            + "\n".join(
                f"- **{check.label}** — {check.details}" for check in failed_checks
            )
        )
    if review.document_types:
        identified = ", ".join(
            f"{file_name}: {document_type.upper()}"
            for file_name, document_type in review.document_types.items()
        )
        st.caption(f"Identified documents — {identified}")
    for check in review.checks:
        marker = "✅" if check.passed else "❌"
        st.markdown(f"{marker} **{check.label}** — {check.details}")


def perform_eus_review(folder_name: str, files: list) -> None:
    """Run and store automated checks for one downloaded email folder."""
    state_key = f"eus_automated_review_{folder_name}"
    error_key = f"{state_key}_error"
    st.session_state.pop(error_key, None)
    try:
        with st.spinner("Reading and comparing the EUS, quote, and PO..."):
            review = eus_document_review.review_mail_documents(
                files,
                is_repeat_customer=lambda name: (
                    eus_document_store.is_repeat_customer(
                        name, exclude_folder=folder_name
                    )
                ),
            )
            st.session_state[state_key] = review
            eus_document_store.set_document_types(
                folder_name, review.document_types
            )
            if review.end_user_name:
                for file_name, document_type in review.document_types.items():
                    if document_type == "eus":
                        eus_document_store.set_end_user(
                            folder_name, file_name, review.end_user_name
                        )
    except eus_document_review.EusReviewError as error:
        st.session_state.pop(state_key, None)
        st.session_state[error_key] = str(error)


def render_eus_attachment_card(
    attachment_path: Path,
    reviewer: str,
    document_type: Optional[str] = None,
    key_prefix: str = "",
) -> None:
    """Show one EUS attachment with its preview and the decision buttons.

    Args:
        attachment_path: The downloaded attachment.
        reviewer: Name of the MfgOps user ("" disables the buttons).

    Returns:
        None.
    """
    file_name = attachment_path.name
    relative_path = attachment_path.relative_to(eus_attachment_dir()).as_posix()
    saved_decision = attachment_store.get_decision(relative_path)
    decided = None
    if saved_decision:
        decided = {
            "verdict": (
                "Approved" if saved_decision["decision"] == "approve" else "Rejected"
            ),
            "comment": saved_decision["comment"],
            "reviewer": saved_decision["decided_by"],
        }
    icon = "📄" if not decided else ("✅" if decided["verdict"] == "Approved" else "⛔")
    title = f"{icon} {file_name}"
    if decided:
        title += f" · {decided['verdict']}"
    expander = st.expander(
        title, key=f"{key_prefix}eus_expander_{relative_path}", on_change="rerun"
    )
    with expander:
        st.caption(f"{attachment_path.stat().st_size / 1024:,.0f} KB")
        st.download_button(
            "⬇️ Download",
            data=attachment_path.read_bytes(),
            file_name=file_name,
            key=f"{key_prefix}eus_download_{relative_path}",
            width="stretch",
        )
        # Embedded PDF viewers float their toolbars outside collapsed expanders.
        if expander.open:
            render_eus_preview(attachment_path)
        st.divider()
        if decided:
            st.success(
                f"{decided['verdict']} by {decided['reviewer'] or 'unknown'} and "
                f"forwarded to {EUS_FORWARD_RECIPIENT}."
            )
            if decided["comment"]:
                st.write(decided["comment"])
            return
        if document_type in {"quote", "po", "other"}:
            st.info(
                f"This {document_type.upper()} is supporting evidence for the "
                "email-level EUS checks and does not need a separate decision."
            )
            return
        comment_key = f"{key_prefix}eus_comment_{relative_path}"
        with st.form(key=f"{key_prefix}eus_decision_{relative_path}"):
            st.text_area(
                "Your comment (needed when rejecting)",
                key=comment_key,
                placeholder="e.g. the EUS is missing the end customer's signature",
            )
            approve, reject = st.columns(2)
            approve.form_submit_button(
                "✅ Approve & forward",
                disabled=not reviewer,
                width="stretch",
                on_click=handle_eus_decision,
                args=(relative_path, "approve", reviewer, comment_key),
            )
            reject.form_submit_button(
                "⛔ Reject & forward",
                disabled=not reviewer,
                width="stretch",
                on_click=handle_eus_decision,
                args=(relative_path, "reject", reviewer, comment_key),
            )
        if not reviewer:
            st.caption("Enter your name in the sidebar to enable the buttons.")


def render_eus_approval_tab(reviewer: str) -> None:
    """Show the EUS attachments from the mailbox and approve or reject them.

    Args:
        reviewer: Name of the MfgOps user.

    Returns:
        None.
    """
    show_flash_messages()
    st.caption(
        "Attachments are grouped by the email they arrived in; each email has its "
        "own timestamped folder. Approving or rejecting forwards the attachment to "
        f"{EUS_FORWARD_RECIPIENT}."
    )
    st.button(
        "📥 Check mailbox for new EUS attachments",
        on_click=handle_eus_fetch,
        width="stretch",
    )
    attachments = list_eus_attachments()
    if not attachments:
        st.info(
            "No EUS attachments yet. Use the button above to check the mailbox."
        )
        return
    groups = group_eus_attachments_by_mail(attachments)
    active_tab, all_tab = st.tabs(["Active EUS documents", "All EUS documents"])
    with active_tab:
        active_folders = eus_document_store.get_active_folders()
        active_groups = [
            group for group in groups if group[0] in active_folders
        ]
        if not active_groups:
            st.success("No active EUS documents. Every email has been reviewed.")
        else:
            st.caption(f"{len(active_groups)} email(s) waiting for review.")
            for folder_name, received_label, files in active_groups:
                render_eus_mail_group(
                    folder_name,
                    received_label,
                    files,
                    reviewer,
                    key_prefix="active_",
                )
    with all_tab:
        st.caption(
            f"{len(attachments)} attachment(s) from {len(groups)} email(s) "
            f"in {eus_attachment_dir()}/"
        )
        for folder_name, received_label, files in groups:
            render_eus_mail_group(folder_name, received_label, files, reviewer)


def main() -> None:
    """Build the page and its tabs.

    Returns:
        None.
    """
    st.set_page_config(page_title="MfgOps Order Review", page_icon="📦", layout="wide")
    st.title("📦 MfgOps · Order Review before release to Arrow")
    prepare_database()
    remember_rules_at_start()
    reviewer = render_sidebar()
    to_review = len(order_store.get_active_orders(REVIEWABLE_STATUSES))
    to_learn = len(learning_request_store.get_open_learning_requests())
    if to_review:
        st.sidebar.info(f"📝 {to_review} order(s) waiting in Orders to Review")
    if to_learn:
        st.sidebar.warning(
            f"🔔 The AI has {to_learn} question(s) for you in Rules Assistant"
        )
    eus_waiting = eus_document_store.count_waiting_folders()
    st.sidebar.info(
        f"🛂 {eus_waiting} EUS document(s) waiting for approval."
    )
    # Fixed tab names, so the open tab stays open after every button click.
    tabs = st.tabs(
        [
            "📊 Dashboard",
            "📝 Orders to Review",
            "📦 Order Tracker",
            "💬 Rules Assistant",
            "🛂 EUS Approval",
        ],
        key="main_tabs",
        on_change="rerun",
    )
    with tabs[0]:
        render_dashboard_tab()
    with tabs[1]:
        render_review_tab(reviewer)
    with tabs[2]:
        render_tracker_tab()
    with tabs[3]:
        render_rules_assistant_tab(reviewer)
    with tabs[4]:
        render_eus_approval_tab(reviewer)


if __name__ == "__main__":
    main()
