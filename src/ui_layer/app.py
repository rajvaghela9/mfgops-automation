"""MfgOps Order Validation UI (Streamlit).

Run from the repo root with `make ui` (python -m streamlit run src/ui_layer/app.py).
Tabs: Analytics (default), Approval Queue, Order Tracker, Knowledge Addition, EUS Approval.
"""

import json
import re
from pathlib import Path
from typing import Optional

import streamlit as st

from src.app import prepare_database, run_pipeline_once
from src.integration_layer.push_orders_to_b2b import push_order
from src.integration_layer.send_back_to_om import send_back_to_om
from src.intelligence_layer.order_validation import (
    FACT_DESCRIPTIONS,
    SUPPORTED_OPERATORS,
)
from src.knowledge_layer import order_store, reference_store, rule_store
from src.utils.config import load_config
from src.utils.models import AiRecommendation, Order, RouteDecision, Rule, RuleAction
from src.utils.time_helpers import utc_now_iso

ACTION_STYLES = {
    RuleAction.BLOCK.value: st.error,
    RuleAction.REQUIRE_DOC.value: st.error,
    RuleAction.WARN.value: st.warning,
}


def render_sidebar() -> str:
    """Show who is reviewing, the current modes, and a manual refresh button.

    Returns:
        The reviewer's name ("" until entered).
    """
    config = load_config()
    st.sidebar.header("Reviewer")
    reviewer = st.sidebar.text_input(
        "Your name (recorded on every decision)", key="reviewer"
    )
    st.sidebar.header("Fusion")
    st.sidebar.write(
        f"Fusion mode: **{config.fusion_mode}** · Email mode: **{config.email_mode}**"
    )
    st.sidebar.write(
        f"LLM explanations: **{'on' if config.llm_enabled else 'off (template)'}**"
    )
    if st.sidebar.button("Fetch & validate now"):
        try:
            summary = run_pipeline_once()
            st.sidebar.success(
                f"Synced {summary['sync']}, validated {summary['validated']}, "
                f"closed {summary['closed']}."
            )
        except Exception as error:
            st.sidebar.error(f"Pipeline failed: {error}")
    return reviewer.strip()


def render_analytics_tab() -> None:
    """Show the business value delivered: volume, quality/trust and time saved.

    Returns:
        None.
    """
    config = load_config()
    metrics = order_store.get_order_history_metrics(config.minutes_saved_per_order)

    def as_percent(value: Optional[float]) -> str:
        """Format a 0-1 rate as a percentage, or "n/a"."""
        return "n/a" if value is None else f"{value:.0%}"

    columns = st.columns(4)
    columns[0].metric("Orders released & closed", metrics["orders_closed"])
    columns[1].metric("AI / human agreement", as_percent(metrics["agreement_rate"]))
    columns[2].metric(
        "First-pass rate (no rework)", as_percent(metrics["first_pass_rate"])
    )
    columns[3].metric("Hours saved (estimate)", f"{metrics['hours_saved']:.1f}")
    columns = st.columns(4)
    columns[0].metric("Decisions logged", metrics["decisions_total"])
    columns[1].metric("Overrides", metrics["overrides"])
    columns[2].metric("Sent back to OM", metrics["sent_back"])
    average = metrics["avg_minutes_detected_to_released"]
    columns[3].metric(
        "Avg minutes detected → released",
        "n/a" if average is None else f"{average:.0f}",
    )
    st.caption(
        f"Hours saved assumes {config.minutes_saved_per_order:g} manual minutes per order "
        "(MINUTES_SAVED_PER_ORDER); replace with the shadow-mode baseline."
    )
    left, right = st.columns(2)
    with left:
        st.subheader("Closed orders by route")
        if metrics["closed_by_route"]:
            st.bar_chart(metrics["closed_by_route"])
        else:
            st.info("No closed orders yet.")
    with right:
        st.subheader("Active orders by status")
        if metrics["active_by_status"]:
            st.bar_chart(metrics["active_by_status"])
        else:
            st.info("No active orders.")


def handle_decision(order_no: str, action: str, reviewer: str) -> None:
    """Carry out a reviewer's button press. Runs as a callback, before the page redraws.

    Args:
        order_no: Fusion order number.
        action: "approve", "override" or "send_back".
        reviewer: Name of the MfgOps user.

    Returns:
        None. The outcome is left in session state for the next redraw to show.
    """
    comment = st.session_state.get(f"comment_{order_no}", "")
    try:
        if action == "approve":
            push_order(order_no, reviewer, comment or None)
            message = f"{order_no} released. It closes once Arrow's USPO appears."
        elif action == "override":
            push_order(order_no, reviewer, comment, override=True)
            message = f"{order_no} released with override."
        else:
            send_back_to_om(order_no, reviewer, comment)
            message = f"{order_no} sent back to OM."
        st.session_state["flash_message"] = message
    except ValueError as error:
        st.session_state["flash_error"] = f"{order_no}: {error}"


def render_order_actions(order: Order, reviewer: str) -> None:
    """Show the comment box and Approve / Override / Send back buttons for one order.

    Args:
        order: The order under review.
        reviewer: Name of the MfgOps user ("" disables the buttons).

    Returns:
        None.
    """
    ai_approves = order.ai_recommendation == AiRecommendation.APPROVE.value
    # One form per order so the comment is submitted together with the button press.
    with st.form(key=f"decision_{order.order_no}"):
        st.text_area(
            "Comment (required to override or send back)",
            key=f"comment_{order.order_no}",
        )
        approve_col, override_col, send_back_col = st.columns(3)
        buttons = [
            (
                approve_col,
                "Approve & Push",
                "approve",
                not (reviewer and ai_approves),
                None,
            ),
            (
                override_col,
                "Override & Push",
                "override",
                not reviewer or ai_approves,
                "Push despite the findings, e.g. when the EUS was approved by email.",
            ),
            (send_back_col, "Send back to OM", "send_back", not reviewer, None),
        ]
        for column, label, action, disabled, help_text in buttons:
            column.form_submit_button(
                label,
                key=f"{action}_{order.order_no}",
                disabled=disabled,
                help=help_text,
                on_click=handle_decision,
                args=(order.order_no, action, reviewer),
            )
    if not reviewer:
        st.caption("Enter your name in the sidebar to enable the buttons.")


def render_approval_queue_tab(reviewer: str) -> None:
    """List every order waiting for a decision, with findings and AI explanation.

    Args:
        reviewer: Name of the MfgOps user.

    Returns:
        None.
    """
    if "flash_message" in st.session_state:
        st.success(st.session_state.pop("flash_message"))
    if "flash_error" in st.session_state:
        st.error(st.session_state.pop("flash_error"))
    orders = order_store.get_active_orders(order_store.REVIEWABLE_STATUSES)
    route_filter = st.multiselect(
        "Routes",
        [route.value for route in RouteDecision],
        default=[route.value for route in RouteDecision],
    )
    orders = [order for order in orders if order.route in route_filter]
    if not orders:
        st.info(
            "Nothing waiting for review. Use 'Fetch & validate now' in the sidebar."
        )
        return
    for order in orders:
        verdict = (
            "✅ approve"
            if order.ai_recommendation == AiRecommendation.APPROVE.value
            else "⛔ reject"
        )
        title = " · ".join(
            [
                order.order_no,
                order.order_type,
                order.ship_to_country,
                order.route,
                f"AI: {verdict}",
            ]
        )
        with st.expander(title):
            st.write(
                f"**Status:** {order.status} · **Route reason:** {order.route_reason}"
            )
            st.write(
                f"**Ship to:** {order.ship_to_customer}, {order.ship_to_address}, "
                f"{order.ship_to_country} · **PO:** {order.customer_po or '—'} · "
                f"**Shipping:** {order.shipping_method or '—'} · **FOB:** {order.fob or '—'}"
            )
            st.write(f"**Shipping instructions:** {order.shipping_instructions or '—'}")
            st.dataframe(
                [
                    {
                        "Line": line.line_number,
                        "SKU": line.sku,
                        "Description": line.item_description,
                        "Qty": line.ordered_qty,
                        "Item class": line.item_class,
                    }
                    for line in order.lines
                ],
                hide_index=True,
            )
            findings = order_store.get_validation_results(order.order_no)
            if findings:
                st.write("**Findings**")
                for finding in findings:
                    ACTION_STYLES.get(finding.action, st.info)(
                        f"{finding.rule_name}: {finding.message}"
                    )
                st.write("**Explanation**")
                st.info(order.ai_explanation or "—")
            else:
                st.success("No issues found.")
            render_order_actions(order, reviewer)


def render_order_tracker_tab() -> None:
    """Show live status of every active order, and searchable history of closed ones.

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
                    "Type": order.order_type,
                    "Route": order.route,
                    "Status": order.status,
                    "AI": order.ai_recommendation,
                    "Decision": order.human_decision,
                    "Rework": order.rework_count,
                    "First seen": order.first_seen_at,
                    "Updated": order.last_updated_at,
                }
                for order in active
            ],
            hide_index=True,
        )
    else:
        st.info("No active orders.")
    st.subheader("Order history (closed)")
    search = st.text_input("Search by order number or USPO")
    history = order_store.get_order_history(search)
    if history:
        st.dataframe(history, hide_index=True)
    else:
        st.info("No closed orders match.")


def save_attachment(uploaded_file) -> str:
    """Save an uploaded evidence file under ATTACHMENTS_DIR with a safe, unique name.

    Args:
        uploaded_file: The Streamlit UploadedFile.

    Returns:
        The saved file's path.
    """
    folder = Path(load_config().attachments_dir)
    folder.mkdir(parents=True, exist_ok=True)
    safe_name = re.sub(r"[^A-Za-z0-9._-]", "_", Path(uploaded_file.name).name)
    stamp = utc_now_iso().replace(":", "").replace("+0000", "Z")
    path = folder / f"{stamp}_{safe_name}"
    path.write_bytes(uploaded_file.getvalue())
    return str(path)


def parse_rule_form(scope_text: str, conditions_text: str) -> tuple:
    """Parse and check the scope and conditions JSON typed into the rule form.

    Args:
        scope_text: JSON object, e.g. {"destination_country": ["AE"]}.
        conditions_text: JSON list, e.g. [{"field": "fob", "op": "missing"}].

    Returns:
        A (scope, conditions) tuple. Raises ValueError with a readable message.
    """
    try:
        scope = json.loads(scope_text or "{}")
        conditions = json.loads(conditions_text or "[]")
    except json.JSONDecodeError as error:
        raise ValueError(f"Not valid JSON: {error}") from error
    if not isinstance(scope, dict) or not all(
        isinstance(v, list) for v in scope.values()
    ):
        raise ValueError('Scope must look like {"fact": ["allowed", "values"]}.')
    if not isinstance(conditions, list):
        raise ValueError("Conditions must be a JSON list.")
    for item in list(scope) + [condition.get("field") for condition in conditions]:
        if item not in FACT_DESCRIPTIONS:
            raise ValueError(
                f"Unknown fact {item!r}. See the list of available facts below."
            )
    for condition in conditions:
        if condition.get("op") not in SUPPORTED_OPERATORS:
            raise ValueError(
                f"Unknown operator {condition.get('op')!r}; use {SUPPORTED_OPERATORS}."
            )
    return scope, conditions


def render_rule_form() -> None:
    """Show the form for adding a business rule with optional evidence.

    Returns:
        None.
    """
    with st.form("add_rule", clear_on_submit=True):
        name = st.text_input("Rule name")
        domain = st.selectbox("Domain", rule_store.RULE_DOMAINS)
        action = st.selectbox("Action", [action.value for action in RuleAction])
        message = st.text_input("Message shown when it fires (use {fact} placeholders)")
        scope_text = st.text_area("Scope (JSON, optional)", value="{}")
        conditions_text = st.text_area(
            "Conditions (JSON list, all must be true)",
            value='[{"field": "", "op": "eq", "value": ""}]',
        )
        source = st.text_input("Source (who/what this rule comes from)")
        owner = st.text_input("Owner")
        evidence = st.file_uploader(
            "Supporting screenshot or PDF", type=["png", "jpg", "jpeg", "pdf"]
        )
        if not st.form_submit_button("Add rule"):
            return
    try:
        if not name.strip() or not message.strip():
            raise ValueError("Rule name and message are required.")
        scope, conditions = parse_rule_form(scope_text, conditions_text)
    except ValueError as error:
        st.error(str(error))
        return
    rule = Rule(
        name=name.strip(),
        domain=domain,
        action=action,
        message=message.strip(),
        scope=scope,
        conditions=conditions,
        source=source.strip(),
        owner=owner.strip(),
        attachment_path=save_attachment(evidence) if evidence else None,
    )
    replaced = rule_store.find_contradicting_rules(rule)
    rule_id = rule_store.add_rule(rule)
    st.success(f"Rule #{rule_id} added. It applies from the next validation run.")
    for old in replaced:
        st.warning(
            f"Replaced contradicting rule #{old.id} '{old.name}' (v{old.version})."
        )


def render_knowledge_addition_tab() -> None:
    """Show rule management and reference-list editing.

    Returns:
        None.
    """
    st.subheader("Add a business rule")
    render_rule_form()
    with st.expander("Available facts for scope and conditions"):
        st.dataframe(
            [
                {"Fact": fact, "Meaning": meaning}
                for fact, meaning in FACT_DESCRIPTIONS.items()
            ],
            hide_index=True,
        )
        st.write(f"Operators: {', '.join(SUPPORTED_OPERATORS)}")

    st.subheader("Active rules")
    rules = rule_store.get_active_rules()
    st.dataframe(
        [
            {
                "ID": rule.id,
                "Name": rule.name,
                "Domain": rule.domain,
                "Action": rule.action,
                "Scope": json.dumps(rule.scope),
                "Conditions": json.dumps(rule.conditions),
                "Version": rule.version,
                "Source": rule.source,
                "Evidence": rule.attachment_path or "",
            }
            for rule in rules
        ],
        hide_index=True,
    )
    rule_names = {rule.id: rule.name for rule in rules}
    with st.form("retire_rule"):
        st.selectbox(
            "Retire a rule",
            [None] + [rule.id for rule in rules],
            format_func=lambda rule_id: (
                "" if rule_id is None else f"#{rule_id} {rule_names[rule_id]}"
            ),
            key="rule_to_retire",
        )
        st.form_submit_button("Retire selected rule", on_click=handle_retire_rule)

    st.subheader("Reference lists")
    if "reference_message" in st.session_state:
        st.info(st.session_state.pop("reference_message"))
    st.dataframe(reference_store.get_reference_rows(), hide_index=True)
    with st.form("reference_entry"):
        st.selectbox("List", list(reference_store.SEED_REFERENCE_LISTS), key="ref_list")
        st.text_input("Key (country code, SKU prefix or customer name)", key="ref_key")
        st.text_input("Value (description, cord region or ratio)", key="ref_value")
        st.text_input("Source", key="ref_source")
        add_col, delete_col = st.columns(2)
        add_col.form_submit_button(
            "Add / update entry", on_click=handle_reference_change, args=("add",)
        )
        delete_col.form_submit_button(
            "Delete entry", on_click=handle_reference_change, args=("delete",)
        )


def handle_retire_rule() -> None:
    """Retire the rule picked in the Retire form (callback, runs before redraw).

    Returns:
        None.
    """
    rule_id = st.session_state.get("rule_to_retire")
    if rule_id is not None:
        rule_store.retire_rule(rule_id)


def handle_reference_change(action: str) -> None:
    """Add/update or delete a reference-list entry (callback, runs before redraw).

    Args:
        action: "add" or "delete".

    Returns:
        None. The outcome is left in session state for the redraw to show.
    """
    list_name = st.session_state["ref_list"]
    key = st.session_state.get("ref_key", "").strip()
    if not key:
        st.session_state["reference_message"] = "Key is required."
        return
    if action == "add":
        reference_store.upsert_reference_value(
            list_name,
            key,
            st.session_state.get("ref_value", ""),
            st.session_state.get("ref_source", ""),
        )
        st.session_state["reference_message"] = f"Saved {key.upper()} in {list_name}."
    else:
        reference_store.delete_reference_value(list_name, key)
        st.session_state["reference_message"] = (
            f"Deleted {key.upper()} from {list_name}."
        )


def render_eus_placeholder_tab() -> None:
    """Placeholder for the EUS Approval workflow (owned by the EUS workstream).

    Returns:
        None.
    """
    st.info(
        "EUS Approval is a work in progress (EUS workstream). No data in this phase."
    )


def main() -> None:
    """Build the page and its five tabs.

    Returns:
        None.
    """
    st.set_page_config(page_title="MfgOps Order Validation", layout="wide")
    st.title("MfgOps · B2B Push to Arrow · Order Validation")
    prepare_database()
    reviewer = render_sidebar()
    analytics, queue, tracker, knowledge, eus = st.tabs(
        [
            "Analytics",
            "Approval Queue",
            "Order Tracker",
            "Knowledge Addition",
            "EUS Approval",
        ]
    )
    with analytics:
        render_analytics_tab()
    with queue:
        render_approval_queue_tab(reviewer)
    with tracker:
        render_order_tracker_tab()
    with knowledge:
        render_knowledge_addition_tab()
    with eus:
        render_eus_placeholder_tab()


if __name__ == "__main__":
    main()
