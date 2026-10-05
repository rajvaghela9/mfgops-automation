from io import BytesIO
from unittest.mock import MagicMock, patch

from src.app import HealthzHandler, run_pipeline_once, run_poll_loop
from src.knowledge_layer import order_store


def make_handler(path):
    handler = HealthzHandler.__new__(HealthzHandler)
    handler.path = path
    handler.rfile = BytesIO()
    handler.wfile = BytesIO()
    handler.send_response = MagicMock()
    handler.end_headers = MagicMock()
    return handler


def test_healthz_returns_200():
    handler = make_handler("/healthz")
    handler.do_GET()
    handler.send_response.assert_called_once_with(200)
    assert handler.wfile.getvalue() == b"ok\n"


def test_unknown_path_returns_404():
    handler = make_handler("/nope")
    handler.do_GET()
    handler.send_response.assert_called_once_with(404)


def test_poll_loop_survives_a_failing_cycle():
    with patch(
        "src.app.run_pipeline_once", side_effect=[RuntimeError("boom"), {}]
    ) as pipeline:
        with patch("src.app.time.sleep") as sleep:
            run_poll_loop(interval_seconds=5, max_cycles=2)
    assert pipeline.call_count == 2
    sleep.assert_called_once_with(5)


def test_pipeline_runs_end_to_end_on_mock_data(seeded_db):
    summary = run_pipeline_once()
    assert summary["sync"]["inserted"] == 9  # 10 mock orders minus 1 RMA
    assert summary["sync"]["excluded"] == 1
    assert summary["validated"] == 9
    assert not order_store.get_active_orders(["ROUTED"])

    second = run_pipeline_once()
    assert second["sync"]["unchanged"] == 9
    assert second["validated"] == 0
