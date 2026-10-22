import json
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from src.intelligence_layer import rules_assistant
from src.knowledge_layer import reference_store, rule_store

NEW_RULE = {
    "name": "Saudi commercial invoice",
    "rule_type": "soft",
    "outcome": "hold",
    "description": "Saudi Arabia orders need a commercial invoice.",
    "scope": {"destination_country": ["SA"]},
    "guidance": "Hold unless the invoice is attached.",
}


def tool_call(name, arguments, call_id="call-1"):
    function = SimpleNamespace(name=name, arguments=json.dumps(arguments))
    return SimpleNamespace(
        content=None, tool_calls=[SimpleNamespace(id=call_id, function=function)]
    )


def text(content):
    return SimpleNamespace(content=content, tool_calls=None)


def fake_ai(*replies):
    return patch("src.intelligence_layer.llm_client.ask_ai", side_effect=list(replies))


def test_adding_a_rule_waits_for_yes(seeded_db):
    before = len(rule_store.get_rules())
    with fake_ai(tool_call("add_rule", NEW_RULE), text("Saved it.")):
        rules_assistant.send_chat_message(
            "chat-1", "Saudi orders need an invoice", "Tester"
        )
        messages, waiting = rules_assistant.get_conversation("chat-1")
        assert "Add this new rule?" in waiting and "destination country: SA" in waiting
        assert len(rule_store.get_rules()) == before  # nothing saved yet

        rules_assistant.answer_change_request("chat-1", True)
    rules = rule_store.get_rules()
    assert len(rules) == before + 1
    assert (rules[-1].created_by, rules[-1].rule_type) == ("Tester", "soft")
    messages, waiting = rules_assistant.get_conversation("chat-1")
    assert waiting is None and messages[-1]["content"] == "Saved it."


def test_saying_no_changes_nothing(seeded_db):
    with fake_ai(
        tool_call(
            "change_list",
            {
                "list_name": "embargoed_countries",
                "action": "add",
                "key": "mm",
                "value": "Myanmar",
            },
        ),
        text("OK."),
    ):
        rules_assistant.send_chat_message("chat-2", "Embargo Myanmar", "Tester")
        rules_assistant.answer_change_request("chat-2", False)
    assert "MM" not in reference_store.get_reference_list("embargoed_countries")


def test_change_and_remove_are_described_in_plain_words(seeded_db):
    rule = next(
        rule for rule in rule_store.get_rules() if rule.name.startswith("Blue-ink")
    )
    summary, save = rules_assistant.prepare_change(
        "change_rule",
        {
            "rule_id": rule.id,
            "changes": {"scope": {"destination_country": ["AE", "QA", "SA"]}},
        },
        "Tester",
    )
    assert "AE, QA → **destination country: AE, QA, SA**" in summary
    save()
    assert rule_store.get_rule(rule.id).scope["destination_country"][-1] == "SA"

    summary, remove = rules_assistant.prepare_change(
        "remove_rule", {"rule_id": rule.id}, "Tester"
    )
    assert "stays in the history" in summary
    remove()
    assert rule_store.get_rule(rule.id).status == "retired"


def test_bad_changes_are_refused_before_asking_the_user(seeded_db):
    with pytest.raises(ValueError, match="needs conditions"):
        rules_assistant.prepare_change(
            "add_rule", {**NEW_RULE, "rule_type": "hard"}, "Tester"
        )
    with pytest.raises(ValueError, match="no rule"):
        rules_assistant.prepare_change("remove_rule", {"rule_id": 9999}, "Tester")


def test_lookups_run_without_asking(seeded_db):
    with fake_ai(
        tool_call("find_rules", {"search": "power cord"}), text("Here they are.")
    ):
        rules_assistant.send_chat_message("chat-3", "Show power cord rules", "Tester")
    messages, waiting = rules_assistant.get_conversation("chat-3")
    assert waiting is None and messages[-1]["content"] == "Here they are."
    assert "Power cords per appliance" in rules_assistant.find_rules("power cord")


def test_a_reply_written_twice_is_shown_once():
    reply = "I suggested sending it back to OM.\n\nWhat did I miss about this order?"
    assert rules_assistant.collapse_repeated_reply(reply + "\n" + reply) == reply
    assert rules_assistant.collapse_repeated_reply(reply) == reply
    two_paragraphs = reply + "\n\nA different second paragraph that is not a repeat."
    assert rules_assistant.collapse_repeated_reply(two_paragraphs) == two_paragraphs
