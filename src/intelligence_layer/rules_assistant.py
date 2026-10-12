"""The Rules Assistant: a chat where MfgOps users look up, add, change or remove rules.

Built with LangGraph, one saved conversation per chat:

    AI replies -> (if it asked to use a tool) carry out the tool request -> AI replies ...

The AI can only touch the database through the tools below, which call rule_store and
reference_store; it never writes SQL. Every change pauses with "Save this change?" and is
only made after the user clicks Yes. Removed rules are retired, never deleted.
"""

import json
import operator
from dataclasses import fields, replace
from typing import Annotated, Any, Callable, Dict, List, Optional, Tuple, TypedDict

from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt

from src.intelligence_layer import llm_client
from src.intelligence_layer.order_review_workflow import (
    open_checkpoint_store,
    thread_config,
)
from src.intelligence_layer.rule_checks import (
    FACT_DESCRIPTIONS,
    SUPPORTED_OPERATORS,
    find_rule_problems,
)
from src.knowledge_layer import learning_request_store, reference_store, rule_store
from src.utils.config import load_config
from src.utils.models import (
    OUTCOME_LABELS,
    ROUTE_LABELS,
    OrderRoute,
    ReviewerDecision,
    Rule,
    RuleOutcome,
    RuleType,
    Verdict,
)
from src.utils.time_helpers import utc_now_iso

SYSTEM_PROMPT = f"""You are the Rules Assistant for Infoblox Manufacturing Operations \
(MfgOps). You help MfgOps users look up, add, change and remove the business rules used \
to review sales orders before they are released to Arrow. The users are operations and \
supply-chain experts, not IT people.

How to talk:
- Plain, friendly words. Never show JSON, code, or internal fact names. You may mention \
rule numbers like "rule #12". Say "fixed check" or "AI judgement" (not hard/soft), and \
"send back to OM", "hold" or "warning only" (not reject/warn).
- If a request is unclear or missing something (which orders it covers, what should \
happen, exceptions), ask one short follow-up question at a time before saving anything.
- The app shows the user a summary and asks them to confirm every change, so don't ask \
"shall I save it?" yourself: call the tool once the rule is clear.
- One change at a time.
- Rules must be general (e.g. "orders to Saudi Arabia need a commercial invoice"), never \
about one specific order number.
- Before adding a rule, call find_rules to check there isn't one already; if there is, \
change it instead. To change or remove a rule, call find_rules first to get its number.

Two kinds of rule:
- hard: a fixed check on the facts below that always gives the same answer. Use it for \
clear-cut checks (embargoed countries, banned customers, missing fields, cord type vs \
country). It needs conditions.
- soft: judged by the AI when it reviews an order, using the guidance text. Use it when a \
rule needs judgement or can't be written with the facts below.

Outcome when a rule finds a problem: reject (send back to OM to correct), hold (a person \
must check first) or warn (just flag it).
can_be_overridden: true, unless it's a compliance stop that must never be released \
(like embargo or banned customer).
Order categories (applies_to_routes): {", ".join(route.value for route in OrderRoute)}. \
Leave empty for all orders.
Scope limits a rule to some orders, e.g. {{"destination_country": ["SA"]}}. Countries use \
2-letter ISO codes.

Facts for scope and conditions:
{chr(10).join(f"- {name}: {meaning}" for name, meaning in FACT_DESCRIPTIONS.items())}
Condition operators: {", ".join(SUPPORTED_OPERATORS)}. Example condition: \
{{"field": "customer_po", "op": "missing"}}.

Many checks use lists instead of rules. For example, to embargo a country, add it to the \
embargoed countries list: the existing embargo rule then applies to it. Lists: \
{", ".join(f"{name} ({label})" for name, label in reference_store.LIST_LABELS.items())}."""

RULE_PROPERTIES = {
    "name": {"type": "string", "description": "Short rule name."},
    "rule_type": {"type": "string", "enum": [kind.value for kind in RuleType]},
    "outcome": {"type": "string", "enum": [outcome.value for outcome in RuleOutcome]},
    "description": {
        "type": "string",
        "description": "One plain-English sentence saying what the rule checks.",
    },
    "category": {
        "type": "string",
        "description": "Topic, e.g. power_cords, documents.",
    },
    "applies_to_routes": {
        "type": "array",
        "items": {"type": "string", "enum": [route.value for route in OrderRoute]},
        "description": "Order categories it applies to; empty for all.",
    },
    "scope": {
        "type": "object",
        "description": "Fact name -> list of allowed values; empty for all orders.",
        "additionalProperties": {"type": "array"},
    },
    "conditions": {
        "type": "array",
        "description": "Hard rules only: all must be true for the rule to find a problem.",
        "items": {
            "type": "object",
            "properties": {
                "field": {"type": "string"},
                "op": {"type": "string", "enum": SUPPORTED_OPERATORS},
                "value": {
                    "description": "Value to compare with (text, number, true/false)."
                },
            },
            "required": ["field", "op"],
        },
    },
    "guidance": {
        "type": "string",
        "description": "Soft rules only: how the AI should judge an order.",
    },
    "message": {
        "type": "string",
        "description": (
            "Hard rules only: message shown when it fires. It may include a fact value "
            "in braces, e.g. 'Shipping method is {shipping_method}'."
        ),
    },
    "can_be_overridden": {"type": "boolean"},
    "original_wording": {"type": "string", "description": "The user's own words."},
    "source": {"type": "string", "description": "Who or what the rule comes from."},
}


def tool(name: str, description: str, properties: Dict, required: List[str]) -> Dict:
    """Describe one tool in the OpenAI format.

    Args:
        name: Tool name.
        description: What it does.
        properties: JSON schema of its arguments.
        required: Required argument names.

    Returns:
        The tool definition.
    """
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": {
                "type": "object",
                "properties": properties,
                "required": required,
            },
        },
    }


LIST_NAME = {"type": "string", "enum": list(reference_store.LIST_LABELS)}
TOOLS = [
    tool(
        "find_rules",
        "Look up active rules (all of them if search is empty).",
        {"search": {"type": "string", "description": "Words to look for."}},
        [],
    ),
    tool(
        "show_list",
        "Show the entries of a list.",
        {"list_name": LIST_NAME},
        ["list_name"],
    ),
    tool(
        "add_rule",
        "Add a new rule (the user is asked to confirm).",
        RULE_PROPERTIES,
        ["name", "rule_type", "outcome", "description"],
    ),
    tool(
        "change_rule",
        "Change an existing rule (the user is asked to confirm).",
        {
            "rule_id": {"type": "integer"},
            "changes": {
                "type": "object",
                "properties": {
                    name: schema
                    for name, schema in RULE_PROPERTIES.items()
                    if name in rule_store.EDITABLE_FIELDS
                },
            },
        },
        ["rule_id", "changes"],
    ),
    tool(
        "remove_rule",
        "Stop applying a rule; it stays in the history (the user is asked to confirm).",
        {"rule_id": {"type": "integer"}, "reason": {"type": "string"}},
        ["rule_id"],
    ),
    tool(
        "change_list",
        "Add or remove one entry of a list (the user is asked to confirm).",
        {
            "list_name": LIST_NAME,
            "action": {"type": "string", "enum": ["add", "remove"]},
            "key": {
                "type": "string",
                "description": "Country code, SKU prefix or name.",
            },
            "value": {
                "type": "string",
                "description": "Description or value (for add).",
            },
        },
        ["list_name", "action", "key"],
    ),
]
CHANGE_TOOLS = {"add_rule", "change_rule", "remove_rule", "change_list"}

FIELD_LABELS = {
    "name": "Name",
    "rule_type": "How it's checked",
    "outcome": "If a problem is found",
    "description": "What it checks",
    "category": "Topic",
    "applies_to_routes": "Order categories",
    "scope": "Only for orders where",
    "conditions": "Fixed check",
    "guidance": "Guidance for the AI",
    "message": "Message shown",
    "can_be_overridden": "Reviewer can override",
    "severity": "Severity",
    "source": "Source",
    "owner": "Owner",
}


class RulesChatState(TypedDict, total=False):
    """One Rules Assistant conversation."""

    messages: Annotated[List[Dict[str, Any]], operator.add]
    reviewer: str
    learning_request_id: Optional[int]
    learning_context: str


def describe_where_rule_applies(rule: Rule) -> str:
    """Say in plain words which orders a rule is about.

    Args:
        rule: The rule.

    Returns:
        E.g. "EUS orders; destination country: CN, HK".
    """
    parts = []
    if rule.applies_to_routes:
        names = [ROUTE_LABELS.get(route, route) for route in rule.applies_to_routes]
        parts.append(", ".join(names) + " orders")
    for name, values in rule.scope.items():
        parts.append(f"{name.replace('_', ' ')}: {', '.join(map(str, values))}")
    return "; ".join(parts) or "All orders"


def describe_rule(rule: Rule) -> str:
    """Write a rule out for a MfgOps user (used in the "Save this change?" box).

    Args:
        rule: The rule.

    Returns:
        A short markdown summary.
    """
    is_hard = rule.rule_type == RuleType.HARD.value
    lines = [
        f"**{rule.name}**",
        f"- What it checks: {rule.description}",
        "- How it's checked: "
        + ("fixed check, always the same answer" if is_hard else "judged by the AI"),
        f"- Applies to: {describe_where_rule_applies(rule)}",
        f"- If a problem is found: {OUTCOME_LABELS.get(rule.outcome, rule.outcome)}",
        "- Reviewer can override: "
        + ("yes" if rule.can_be_overridden else "no, the order can never be released"),
    ]
    if not is_hard and rule.guidance:
        lines.append(f"- Guidance for the AI: {rule.guidance}")
    if is_hard and rule.message:
        lines.append(f"- Message shown: {rule.message}")
    return "\n".join(lines)


def describe_value(name: str, value: Any) -> str:
    """Write one rule field's value in plain words.

    Args:
        name: Field name.
        value: Field value.

    Returns:
        Readable text.
    """
    if name == "outcome":
        return OUTCOME_LABELS.get(value, value)
    if name == "applies_to_routes":
        return ", ".join(ROUTE_LABELS.get(route, route) for route in value) or "all"
    if name == "can_be_overridden":
        return "yes" if value else "no"
    if name == "conditions":
        return "updated"
    if name == "scope":
        return describe_where_rule_applies(Rule("", "", "", "", scope=value))
    return str(value) if value not in (None, "") else "(empty)"


def find_rules(search: str = "") -> str:
    """Tool: list active rules with everything the AI needs to edit them.

    Args:
        search: Optional words to look for.

    Returns:
        One line per rule.
    """
    rules = rule_store.get_rules()
    words = [word.lower() for word in search.split() if len(word) > 2]
    matching = [
        rule
        for rule in rules
        if not words
        or any(
            word
            in f"{rule.name} {rule.description} {rule.category} {rule.guidance}".lower()
            for word in words
        )
    ]
    note = "" if matching or not words else "No rule matched; here are all rules.\n"
    return note + "\n".join(
        f"#{rule.id} {rule.name} | "
        + ("fixed check" if rule.rule_type == RuleType.HARD.value else "AI judgement")
        + f" | if a problem is found: {OUTCOME_LABELS.get(rule.outcome, rule.outcome)} | "
        f"reviewer can override: {'yes' if rule.can_be_overridden else 'no'} | "
        f"{rule.description} | applies to: {describe_where_rule_applies(rule)} | "
        f"conditions: {json.dumps(rule.conditions)} | guidance: {rule.guidance or '-'} | "
        f"message: {rule.message or '-'}"
        for rule in (matching or rules)
    )


def show_list(list_name: str) -> str:
    """Tool: show the entries of one reference list.

    Args:
        list_name: One of reference_store.LIST_LABELS.

    Returns:
        One line per entry.
    """
    entries = reference_store.get_reference_list(list_name)
    label = reference_store.LIST_LABELS.get(list_name, list_name)
    return f"{label}:\n" + (
        "\n".join(f"- {key}: {value}" for key, value in entries.items()) or "(empty)"
    )


def prepare_change(
    tool_name: str, arguments: Dict[str, Any], reviewer: str
) -> Tuple[str, Callable[[], str]]:
    """Check a change the AI asked for and describe it for the user.

    Args:
        tool_name: One of CHANGE_TOOLS.
        arguments: The AI's tool arguments.
        reviewer: Name of the MfgOps user.

    Returns:
        (summary shown to the user, function that makes the change and says what it did).
        Raises ValueError if the change can't be made.
    """
    if tool_name == "add_rule":
        return prepare_new_rule(arguments, reviewer)
    if tool_name in ("change_rule", "remove_rule"):
        rule = rule_store.get_rule(int(arguments.get("rule_id") or 0))
        if rule is None:
            raise ValueError(f"There is no rule #{arguments.get('rule_id')}.")
        if tool_name == "remove_rule":
            return prepare_rule_removal(rule)
        return prepare_rule_change(rule, arguments.get("changes") or {})
    return prepare_list_change(arguments, reviewer)


def prepare_new_rule(
    arguments: Dict[str, Any], reviewer: str
) -> Tuple[str, Callable[[], str]]:
    """Check and describe a new rule.

    Args:
        arguments: The AI's add_rule arguments.
        reviewer: Name of the MfgOps user (recorded as author and approver).

    Returns:
        (summary, function that saves the rule).
    """
    known = {item.name for item in fields(Rule)}
    rule = Rule(
        **{name: value for name, value in arguments.items() if name in known},
        created_by=reviewer,
        approved_by=reviewer,
        approved_at=utc_now_iso(),
        confirmed=True,
        generated_by_model=load_config().litellm_model or "",
    )
    problems = find_rule_problems(rule)
    if problems:
        raise ValueError(" ".join(problems))
    summary = "**Add this new rule?**\n\n" + describe_rule(rule)
    replaced = rule_store.find_contradicting_rules(rule)
    if replaced:
        summary += "\n\nIt replaces: " + ", ".join(
            f"#{old.id} {old.name}" for old in replaced
        )

    def save_rule() -> str:
        """Save the rule. Returns: what happened."""
        return f"Saved as rule #{rule_store.add_rule(rule)}."

    return summary, save_rule


def prepare_rule_removal(rule: Rule) -> Tuple[str, Callable[[], str]]:
    """Describe retiring a rule.

    Args:
        rule: The rule to retire.

    Returns:
        (summary, function that retires the rule).
    """
    summary = (
        f"**Remove rule #{rule.id} “{rule.name}”?**\n\nIt will no longer be applied to "
        "orders (it stays in the history)."
    )

    def remove() -> str:
        """Retire the rule. Returns: what happened."""
        rule_store.retire_rule(rule.id)
        return f"Removed rule #{rule.id}."

    return summary, remove


def prepare_rule_change(
    rule: Rule, requested: Dict[str, Any]
) -> Tuple[str, Callable[[], str]]:
    """Check and describe changes to an existing rule.

    Args:
        rule: The rule as it is now.
        requested: Field name to new value, from the AI.

    Returns:
        (summary listing old and new values, function that saves the changes).
    """
    changes = {
        name: value
        for name, value in requested.items()
        if name in rule_store.EDITABLE_FIELDS and getattr(rule, name) != value
    }
    if not changes:
        raise ValueError("Nothing would change.")
    problems = find_rule_problems(replace(rule, **changes))
    if problems:
        raise ValueError(" ".join(problems))
    summary = f"**Change rule #{rule.id} “{rule.name}”?**\n\n" + "\n".join(
        f"- {FIELD_LABELS.get(name, name)}: {describe_value(name, getattr(rule, name))} "
        f"→ **{describe_value(name, value)}**"
        for name, value in changes.items()
    )

    def save_changes() -> str:
        """Save the changes. Returns: what happened."""
        rule_store.update_rule(rule.id, changes)
        return f"Updated rule #{rule.id}."

    return summary, save_changes


def prepare_list_change(
    arguments: Dict[str, Any], reviewer: str
) -> Tuple[str, Callable[[], str]]:
    """Check and describe adding or removing a list entry.

    Args:
        arguments: The AI's change_list arguments.
        reviewer: Name of the MfgOps user (recorded as the entry's source).

    Returns:
        (summary, function that changes the list).
    """
    list_name = arguments.get("list_name", "")
    if list_name not in reference_store.LIST_LABELS:
        raise ValueError(f"Unknown list {list_name!r}.")
    key = str(arguments.get("key", "")).strip().upper()
    if not key:
        raise ValueError("The list entry needs a key.")
    label = reference_store.LIST_LABELS[list_name]
    value = str(arguments.get("value", "")).strip()
    removing = arguments.get("action") == "remove"

    def change_list() -> str:
        """Change the list. Returns: what happened."""
        if removing:
            reference_store.delete_reference_value(list_name, key)
            return f"Removed {key} from {label}."
        reference_store.upsert_reference_value(
            list_name, key, value, f"Rules Assistant ({reviewer})"
        )
        return f"Added {key} to {label}."

    if removing:
        return f"**Remove “{key}” from the list “{label}”?**", change_list
    return (
        f"**Add “{key}” ({value or 'no description'}) to the list “{label}”?**",
        change_list,
    )


def handle_change_request(
    tool_name: str, arguments: Dict, state: RulesChatState
) -> str:
    """Show the user the change, pause for Yes/No, and make it only on Yes.

    Args:
        tool_name: One of CHANGE_TOOLS.
        arguments: The AI's tool arguments.
        state: The conversation state.

    Returns:
        What happened, for the AI to read.
    """
    try:
        summary, make_change = prepare_change(
            tool_name, arguments, state.get("reviewer", "")
        )
    except ValueError as error:
        return f"Not done, please fix and try again: {error}"
    if not interrupt({"summary": summary}):
        return "The user chose not to save this change. Ask what they'd like instead."
    result = make_change()
    if state.get("learning_request_id"):
        learning_request_store.close_learning_request(state["learning_request_id"])
    return f"Done. {result}"


def build_system_prompt(state: RulesChatState) -> str:
    """Add the learning context (if this chat is about a disagreement) to the instructions.

    Args:
        state: The conversation state.

    Returns:
        The system prompt.
    """
    context = state.get("learning_context")
    return f"{SYSTEM_PROMPT}\n\n{context}" if context else SYSTEM_PROMPT


def collapse_repeated_reply(text: str) -> str:
    """Undo the occasional glitch where a smaller model writes its whole reply twice.

    Args:
        text: The AI's reply.

    Returns:
        The reply once if it is two identical copies, else unchanged.
    """
    stripped = text.strip()
    for position, character in enumerate(stripped):
        first, second = stripped[:position].strip(), stripped[position:].strip()
        if character == "\n" and len(first) > 40 and first == second:
            return first
    return text


def ai_replies(state: RulesChatState) -> Dict:
    """Ask the AI for its next message (text, or a request to use tools).

    Args:
        state: The conversation state.

    Returns:
        The AI's message, added to the conversation.
    """
    history = state.get("messages", [])
    if not history or history[0]["role"] != "user":
        history = [
            {"role": "user", "content": "(The reviewer opened this chat.)"}
        ] + history
    try:
        reply = llm_client.ask_ai(
            [{"role": "system", "content": build_system_prompt(state)}] + history,
            tools=TOOLS,
        )
    except llm_client.AiUnavailableError as error:
        text = f"Sorry, I can't reach the AI right now ({error}). Please try again shortly."
        return {"messages": [{"role": "assistant", "content": text}]}
    message: Dict[str, Any] = {
        "role": "assistant",
        "content": collapse_repeated_reply(reply.content or ""),
    }
    if reply.tool_calls:
        message["content"] = reply.content or None
        message["tool_calls"] = [
            {
                "id": call.id,
                "type": "function",
                "function": {
                    "name": call.function.name,
                    "arguments": call.function.arguments,
                },
            }
            for call in reply.tool_calls
        ]
    return {"messages": [message]}


def carry_out_tool_requests(state: RulesChatState) -> Dict:
    """Run the tools the AI asked for. Lookups run straight away; changes need a Yes.

    Only one change per turn, so a resumed step can never make a change twice.

    Args:
        state: The conversation state.

    Returns:
        One tool result per tool request, added to the conversation.
    """
    lookups = {"find_rules": find_rules, "show_list": show_list}
    results, change_requested = [], False
    for call in state["messages"][-1]["tool_calls"]:
        name = call["function"]["name"]
        try:
            arguments = json.loads(call["function"]["arguments"] or "{}")
        except json.JSONDecodeError:
            arguments = None
        if arguments is None:
            result = "Not done: the tool arguments were not valid JSON."
        elif name in lookups:
            result = lookups[name](**arguments)
        elif name in CHANGE_TOOLS and change_requested:
            result = "Not done: one change at a time. Ask for this one after the first."
        elif name in CHANGE_TOOLS:
            change_requested = True
            result = handle_change_request(name, arguments, state)
        else:
            result = f"Unknown tool {name!r}."
        results.append({"role": "tool", "tool_call_id": call["id"], "content": result})
    return {"messages": results}


def ai_wants_tools(state: RulesChatState) -> str:
    """Decide whether to run tools or wait for the user's next message.

    Args:
        state: The conversation state.

    Returns:
        "carry_out_tool_requests" or END.
    """
    return "carry_out_tool_requests" if state["messages"][-1].get("tool_calls") else END


def build_chat() -> StateGraph:
    """Wire the two chat steps together.

    Returns:
        The (uncompiled) chat graph.
    """
    chat = StateGraph(RulesChatState)
    chat.add_node("ai_replies", ai_replies)
    chat.add_node("carry_out_tool_requests", carry_out_tool_requests)
    chat.add_edge(START, "ai_replies")
    chat.add_conditional_edges(
        "ai_replies", ai_wants_tools, ["carry_out_tool_requests", END]
    )
    chat.add_edge("carry_out_tool_requests", "ai_replies")
    return chat


_compiled_chats: Dict[str, object] = {}


def get_chat():
    """Return the compiled chat, connected to the checkpoint file (built once).

    Returns:
        The compiled LangGraph chat.
    """
    path = load_config().workflow_db_path
    if path not in _compiled_chats:
        _compiled_chats[path] = build_chat().compile(
            checkpointer=open_checkpoint_store()
        )
    return _compiled_chats[path]


def send_chat_message(thread_id: str, text: str, reviewer: str) -> None:
    """Send the user's message and let the AI answer (it may pause for a confirmation).

    Args:
        thread_id: The conversation's id.
        text: What the user typed.
        reviewer: Name of the MfgOps user.

    Returns:
        None.
    """
    get_chat().invoke(
        {"messages": [{"role": "user", "content": text}], "reviewer": reviewer},
        thread_config(thread_id),
    )


def answer_change_request(thread_id: str, save_it: bool) -> None:
    """Resume the conversation with the user's Yes/No to a proposed change.

    Args:
        thread_id: The conversation's id.
        save_it: True if the user clicked Yes.

    Returns:
        None.
    """
    get_chat().invoke(Command(resume=save_it), thread_config(thread_id))


def get_conversation(thread_id: str) -> Tuple[List[Dict[str, str]], Optional[str]]:
    """Read a conversation for display.

    Args:
        thread_id: The conversation's id.

    Returns:
        (user and AI text messages, summary of a change waiting for Yes/No or None).
    """
    snapshot = get_chat().get_state(thread_config(thread_id))
    messages = [
        message
        for message in snapshot.values.get("messages", [])
        if message["role"] in ("user", "assistant") and message.get("content")
    ]
    waiting = snapshot.interrupts[0].value["summary"] if snapshot.interrupts else None
    return messages, waiting


def start_learning_chat(request_id: int, reviewer: str) -> str:
    """Open (or reopen) the chat where the AI learns from a reviewer's decision.

    Args:
        request_id: The learning request's id.
        reviewer: Name of the MfgOps user.

    Returns:
        The conversation's id.
    """
    request = learning_request_store.get_learning_request(request_id)
    thread_id = request["chat_thread_id"] or f"learning-{request_id}"
    if get_chat().get_state(thread_config(thread_id)).values.get("messages"):
        return thread_id
    learning_request_store.link_chat(request_id, thread_id)
    verdict_labels = {
        Verdict.APPROVE.value: "release it",
        Verdict.REJECT.value: "send it back to OM",
        Verdict.HOLD.value: "hold it",
    }
    decision_labels = {
        ReviewerDecision.APPROVE.value: "approved and released it",
        ReviewerDecision.SEND_TO_OM.value: "sent it back to OM",
        ReviewerDecision.HOLD.value: "put it on hold",
    }
    context = (
        "THIS CONVERSATION: learn from a review decision you disagreed with.\n"
        f"You suggested to {verdict_labels.get(request['ai_verdict'])} order "
        f"{request['order_no']}, but {request['decided_by']} "
        f"{decision_labels.get(request['reviewer_decision'])}, commenting: "
        f"\"{request['reviewer_comment'] or 'no comment'}\".\n\n"
        f"{request['order_details']}\n\n"
        "Start by saying in one or two sentences what you suggested and what the reviewer "
        "decided, then ask what you missed. Ask short follow-up questions until you "
        "understand the general business rule behind the decision. Then add or change ONE "
        "general rule (never tied to this order number) or list entry. If the reviewer "
        "says no rule is needed, accept that and say they can dismiss this request."
    )
    get_chat().invoke(
        {
            "messages": [],
            "reviewer": reviewer,
            "learning_request_id": request_id,
            "learning_context": context,
        },
        thread_config(thread_id),
    )
    return thread_id
