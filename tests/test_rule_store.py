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


def test_an_older_database_gets_the_new_rules_and_list_entries(seeded_db):
    from src.knowledge_layer import reference_store
    from src.knowledge_layer.db import get_connection, set_app_setting

    # Make the database look like it was seeded before version 2.
    new_names = rule_store.ADDED_IN_SEED_VERSION[2]
    with get_connection() as connection:
        for name in new_names:
            connection.execute("DELETE FROM rules WHERE name = ?", (name,))
        connection.execute("UPDATE rules SET version = 1")
        connection.execute(
            "DELETE FROM reference_lists WHERE list_name = ? AND item_key = ?",
            (reference_store.COUNTRY_CORD_REGION, "JP"),
        )
        connection.execute(
            "UPDATE reference_lists SET item_value = 'EU' WHERE list_name = ? "
            "AND item_key = 'IT'",
            (reference_store.COUNTRY_CORD_REGION,),
        )
    set_app_setting(rule_store.SEED_VERSION_SETTING, "1")
    set_app_setting(reference_store.SEED_VERSION_SETTING, "1")
    edited = next(
        r
        for r in rule_store.get_rules()
        if r.name == "Ship-from and ship-to look right"
    )
    rule_store.update_rule(edited.id, {"guidance": "My own guidance."})
    retired = next(r for r in rule_store.get_rules() if r.name == "Banned customer")
    rule_store.retire_rule(retired.id)

    rule_store.upgrade_seed_rules()
    reference_store.upgrade_reference_lists()

    names = {rule.name for rule in rule_store.get_rules()}
    assert set(new_names) <= names
    assert "Banned customer" not in names  # retired stays retired
    assert rule_store.get_rule(edited.id).guidance == "My own guidance."
    cords = next(
        r for r in rule_store.get_rules() if r.name == "Power cords per appliance"
    )
    assert cords.guidance == rule_store.CORD_GUIDANCE
    regions = reference_store.get_reference_list(reference_store.COUNTRY_CORD_REGION)
    assert (regions["JP"], regions["IT"]) == ("JP", "CL")

    count = len(rule_store.get_rules(status=None))
    rule_store.upgrade_seed_rules()  # applied once only
    assert len(rule_store.get_rules(status=None)) == count


def test_a_users_list_edit_is_never_overwritten(seeded_db):
    from src.knowledge_layer import reference_store
    from src.knowledge_layer.db import set_app_setting

    reference_store.upsert_reference_value(
        reference_store.COUNTRY_CORD_REGION, "IT", "UK", "Tester"
    )
    set_app_setting(reference_store.SEED_VERSION_SETTING, "1")
    reference_store.upgrade_reference_lists()
    regions = reference_store.get_reference_list(reference_store.COUNTRY_CORD_REGION)
    assert regions["IT"] == "UK"
