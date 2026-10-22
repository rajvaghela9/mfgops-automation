import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from streamlit.testing.v1 import AppTest

from src.app import run_pipeline_once
from src.knowledge_layer import order_store

UI_SCRIPT = str(Path(__file__).parents[1] / "src" / "ui_layer" / "app.py")


def open_ui(reviewer=""):
    app = AppTest.from_file(UI_SCRIPT, default_timeout=60).run()
    if reviewer:
        app.sidebar.text_input(key="reviewer").set_value(reviewer).run()
    return app


def test_ui_shows_every_tab_without_errors(seeded_db, sample_backlog):
    run_pipeline_once()
    app = open_ui()
    assert not app.exception
    assert [tab.label for tab in app.tabs][:4] == [
        "📊 Dashboard",
        "📝 Orders to Review",
        "📦 Order Tracker",
        "💬 Rules Assistant",
    ]
    assert len(app.expander) >= 12  # one card per order to review


def card_titles(app):
    return [e.label for e in app.expander if "·" in e.label]


def test_dummy_orders_come_last_and_no_uspo_is_shown_before_release(
    seeded_db, sample_backlog
):
    from src.utils.mock_data import get_dummy_orders

    for order in get_dummy_orders():
        order_store.upsert_order(order)  # dummy orders seen first ...
    run_pipeline_once()  # ... and the sample (real) orders after them
    app = open_ui()
    titles = card_titles(app)
    dummy_positions = [i for i, title in enumerate(titles) if "dummy_" in title]
    assert dummy_positions == list(range(len(titles) - 5, len(titles)))
    assert all("🧪" in titles[i] for i in dummy_positions)
    page = " ".join(str(element.value) for element in app.caption)
    assert "USPO" not in page and "Arrow PO" not in page


def test_reviewer_can_approve_but_never_an_embargoed_order(seeded_db, sample_backlog):
    run_pipeline_once()
    app = open_ui("Tester")
    approve = {button.key: button for button in app.button if "Approve" in button.label}
    embargoed = next(key for key in approve if "DIST-MOCK-0005" in key)
    clean = next(key for key in approve if "REN-1103701" in key)
    assert approve[embargoed].disabled
    approve[clean].click().run()
    assert not app.exception
    assert any(
        "REN-1103701 is approved" in box.value and "USPO" in box.value
        for box in app.success
    )
    assert order_store.get_order("REN-1103701").status == "APPROVED"
    waiting = [e for e in app.expander if "waiting for the USPO" in e.label]
    assert waiting and "(1)" in waiting[0].label

    sample_backlog["REN-1103701"] = "USPO46100"
    run_pipeline_once()
    app = open_ui("Tester")
    assert not [e for e in app.expander if "waiting for the USPO" in e.label]
    assert not any("REN-1103701" in title for title in card_titles(app))
    all_orders = next(df.value for df in app.dataframe if "USPO" in df.value)
    assert (
        all_orders[all_orders["Order"] == "REN-1103701"]["USPO"].item() == "USPO46100"
    )


def test_new_rule_shows_up_marked_right_after_saving(seeded_db):
    new_rule = {
        "name": "Saudi commercial invoice",
        "rule_type": "soft",
        "outcome": "hold",
        "description": "Saudi Arabia orders need a commercial invoice.",
        "guidance": "Hold unless the invoice is attached.",
    }
    function = SimpleNamespace(name="add_rule", arguments=json.dumps(new_rule))
    asks_to_add = SimpleNamespace(
        content=None, tool_calls=[SimpleNamespace(id="c1", function=function)]
    )
    confirms = SimpleNamespace(content="Saved it.", tool_calls=None)
    app = open_ui("Tester")
    marks_before = [df.value[""].tolist() for df in app.dataframe if "Rule" in df.value]
    assert not any(any(marks) for marks in marks_before)

    with patch(
        "src.intelligence_layer.llm_client.ask_ai", side_effect=[asks_to_add, confirms]
    ):
        app.chat_input[0].set_value("Saudi orders need an invoice").run()
        assert any("Add this new rule?" in box.value for box in app.info)
        next(b for b in app.button if "Yes, save it" in b.label).click().run()

    assert not app.exception
    rules_table = next(df.value for df in app.dataframe if "Rule" in df.value)
    assert (
        rules_table[rules_table["Rule"] == "Saudi commercial invoice"][""].item()
        == "🆕 New"
    )
    assert [e for e in app.expander if "Current business rules" in e.label][
        0
    ].proto.expanded
