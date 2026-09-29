from io import BytesIO
from unittest.mock import MagicMock

from src.app import HealthzHandler


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
