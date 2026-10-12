from src.knowledge_layer import rule_store
from src.utils.models import Rule


def hard(name, scope=None, field="freight_terms"):
    return Rule(
        name,
        "hard",
        "reject",
        "d",
        scope=scope or {},
        conditions=[{"field": field, "op": "missing"}],
    )


def test_seed_rules_load_once(seeded_db):
    count = len(rule_store.get_rules())
    assert count == len(rule_store.SEED_RULES)
    rule_store.seed_default_rules()
    assert len(rule_store.get_rules()) == count


def test_every_column_round_trips(seeded_db):
    rule = Rule(
        "Saudi invoice",
        "soft",
        "hold",
        "Saudi orders need a commercial invoice.",
        applies_to_routes=["special_instructions"],
        scope={"destination_country": ["SA"]},
        guidance="Hold if no invoice.",
        can_be_overridden=True,
        original_wording="saudi needs invoice",
        confidence=0.8,
        created_by="Tester",
        example_orders="DIST-1",
    )
    saved = rule_store.get_rule(rule_store.add_rule(rule))
    assert saved.scope == {"destination_country": ["SA"]}
    assert saved.applies_to_routes == ["special_instructions"]
    assert (saved.guidance, saved.confidence, saved.created_by) == (
        "Hold if no invoice.",
        0.8,
        "Tester",
    )
    assert saved.conditions == [] and saved.message == ""


def test_contradicting_hard_rule_retires_the_old_one(seeded_db):
    first = rule_store.add_rule(hard("Old", {"destination_country": ["DE"]}))
    second = rule_store.add_rule(hard("New", {"destination_country": ["DE"]}))
    assert rule_store.get_rule(first).status == "retired"
    assert rule_store.get_rule(second).version == 2
    other_scope = rule_store.add_rule(hard("Other", {"destination_country": ["FR"]}))
    assert rule_store.get_rule(second).status == "active"
    assert rule_store.get_rule(other_scope).status == "active"


def test_update_and_retire(seeded_db):
    rule_id = rule_store.add_rule(hard("Edit me"))
    updated = rule_store.update_rule(rule_id, {"outcome": "hold", "name": "Edited"})
    assert (updated.outcome, updated.name, updated.version) == ("hold", "Edited", 2)
    rule_store.retire_rule(rule_id)
    assert rule_id not in [rule.id for rule in rule_store.get_rules()]
    assert rule_store.get_rule(rule_id) is not None  # kept for the record
