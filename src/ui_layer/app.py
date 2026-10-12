"""MfgOps Order Review (Streamlit), written for MfgOps users, not IT.

Run from the repo root with `make ui` (python -m streamlit run src/ui_layer/app.py).
Tabs: Dashboard, Orders to Review, Order Tracker, Rules Assistant, EUS Approval.
"""

import uuid
from pathlib import Path
from typing import Optional

import streamlit as st

from src.app import prepare_database, run_pipeline_once
from src.intelligence_layer import rules_assistant
from src.intelligence_layer.order_review_workflow import submit_reviewer_decision
from src.intelligence_layer.rule_checks import find_release_blocker
from src.knowledge_layer import (
    learning_request_store,
    order_store,
    reference_store,
    rule_store,
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
    OrderStatus.RELEASED.value: "Released to Arrow",
}
FINDING_STYLES = {
    RuleOutcome.REJECT.value: (st.error, "⛔"),
    RuleOutcome.HOLD.value: (st.warning, "⏸️"),
    RuleOutcome.WARN.value: (st.info, "⚠️"),
}


def label(labels: dict, value: Optional[str]) -> str:
    """Look up the friendly name of a stored value.

    Args:
        labels: Value-to-name mapping.
        value: The stored value.

    Returns:
        The friendly name, the value itself if unknown, or "—" if empty.
    """
    return labels.get(value, value) if value else "—"


def show_flash_messages() -> None:
    """Show the outcome of the last button press (saved before the page redrew).

    Returns:
        None.
    """
    if "flash_message" in st.session_state:
        st.success(st.session_state.pop("flash_message"))
    if "flash_error" in st.session_state:
        st.error(st.session_state.pop("flash_error"))


def render_sidebar() -> str:
    """Show who is reviewing and the button that checks Fusion for new orders.

    Returns:
        The reviewer's name ("" until entered).
    """
    config = load_config()
    st.sidebar.header("👤 Reviewer")
    reviewer = st.sidebar.text_input(
        "Your name (saved with every decision)", key="reviewer"
    )
    st.sidebar.header("📥 New orders")
    if st.sidebar.button("Check Fusion for new orders", width="stretch"):
        with st.spinner("Fetching orders and reviewing them..."):
            try:
                summary = run_pipeline_once()
                sync = summary["sync"]
                st.sidebar.success(
                    f"{sync['new']} new and {sync['updated']} corrected order(s) found, "
                    f"{summary['reviewed']} reviewed."
                )
            except Exception as error:
                st.sidebar.error(f"Something went wrong: {error}")
    if config.fusion_mode == "file":
        source = f"backlog report “{Path(config.backlog_file_path or '').name}”"
        if config.include_demo_orders:
            source += " + 10 demo problem orders"
    else:
        source = (
            "sample orders (test mode)" if config.fusion_mode == "mock" else "Fusion"
        )
    st.sidebar.caption(f"Orders come from: {source}")
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

    columns = st.columns(4)
    columns[0].metric("Orders received", numbers["orders_seen"])
    columns[1].metric("Released to Arrow", numbers["released"])
    columns[2].metric("Sent back to OM", numbers["sent_to_om"])
    columns[3].metric("Waiting or on hold", numbers["waiting"])
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
            st.info("No orders yet. Use “Check Fusion for new orders” in the sidebar.")
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
    message = {
        ReviewerDecision.APPROVE.value: f"{order_no} was released to Arrow.",
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
        buttons = [
            (
                approve,
                "✅ Approve & release",
                ReviewerDecision.APPROVE.value,
                bool(blocker),
            ),
            (send, "📧 Send to OM", ReviewerDecision.SEND_TO_OM.value, False),
            (hold, "⏸️ Put on hold", ReviewerDecision.HOLD.value, False),
        ]
        for column, text, decision, blocked in buttons:
            column.form_submit_button(
                text,
                disabled=not reviewer or blocked,
                width="stretch",
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
            f"{VERDICT_LABELS.get(order.ai_verdict, '').split()[0]} {order.order_no}",
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
        if order.arrow_po:
            st.caption(
                f"ℹ️ This order already has Arrow PO {order.arrow_po}, so it was sent to Arrow "
                "before. Orders in the backlog report you loaded are treated as test data."
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
    if not orders:
        st.info("Nothing to review. Use “Check Fusion for new orders” in the sidebar.")
        return
    without_ai = [order.order_no for order in orders if not order.ai_used]
    if without_ai:
        st.warning(
            f"⚠️ {len(without_ai)} order(s) were checked by the fixed rules only, because "
            "the AI could not be reached (see each order's summary for the reason). "
            "Once the connection works, click “Check Fusion for new orders” to review "
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
                    "Order": order.order_no,
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
                    "Order": row["order_no"],
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
                }
                for row in rows
            ],
            hide_index=True,
            width="stretch",
        )
    else:
        st.info("No orders match.")


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


def render_eus_placeholder_tab() -> None:
    """Placeholder for the EUS Approval workflow (owned by the EUS workstream).

    Returns:
        None.
    """
    st.info("EUS Approval is coming soon (EUS workstream).")


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
        render_eus_placeholder_tab()


if __name__ == "__main__":
    main()
