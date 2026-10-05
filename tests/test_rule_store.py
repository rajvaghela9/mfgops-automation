from src.knowledge_layer import rule_store
from src.knowledge_layer.db import init_db
from src.utils.models import Rule


def make_rule(name, scope=None, field="fob", action="block"):
    return Rule(
        name=name,
        domain=rule_store.ORDER_VALIDATION,
        action=action,
        message="msg",
        scope=scope or {},
        conditions=[{"field": field, "op": "missing"}],
    )


def test_add_and_read_rule():
    init_db()
    rule_id = rule_store.add_rule(make_rule("FOB required"))
    rules = rule_store.get_active_rules()
    assert [rule.id for rule in rules] == [rule_id]
    assert rules[0].conditions == [{"field": "fob", "op": "missing"}]


def test_contradicting_rule_replaces_older_one():
    init_db()
    old_id = rule_store.add_rule(make_rule("FOB required", action="block"))
    new_id = rule_store.add_rule(make_rule("FOB only a warning", action="warn"))
    rules = rule_store.get_active_rules()
    assert [rule.id for rule in rules] == [new_id]
    assert rules[0].version == 2
    assert old_id != new_id


def test_different_scope_or_field_does_not_contradict():
    init_db()
    rule_store.add_rule(make_rule("FOB required"))
    rule_store.add_rule(
        make_rule("FOB required in AE", scope={"destination_country": ["AE"]})
    )
    rule_store.add_rule(make_rule("PO required", field="customer_po"))
    assert len(rule_store.get_active_rules()) == 3


def test_retired_rule_is_not_active():
    init_db()
    rule_id = rule_store.add_rule(make_rule("FOB required"))
    rule_store.retire_rule(rule_id)
    assert rule_store.get_active_rules() == []


def test_seeding_runs_only_once():
    init_db()
    rule_store.seed_default_rules()
    rule_store.seed_default_rules()
    assert len(rule_store.get_active_rules()) == len(rule_store.SEED_RULES)
