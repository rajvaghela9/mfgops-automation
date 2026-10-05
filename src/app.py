"""Service entrypoint: polls Fusion, routes and validates orders, closes released ones.

Run with `python -m src.app`. It never releases or rejects an order by itself; those
actions only happen when a MfgOps user clicks a button in the UI (src/ui_layer/app.py).
"""

import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Dict, Optional

from src.integration_layer.fetch_orders_from_fusion import sync_orders
from src.integration_layer.push_orders_to_b2b import verify_released_orders
from src.intelligence_layer.order_validation import validate_pending_orders
from src.knowledge_layer.db import init_db
from src.knowledge_layer.reference_store import seed_reference_lists
from src.knowledge_layer.rule_store import seed_default_rules
from src.utils.config import load_config
from src.utils.logging_config import get_logger

logger = get_logger(__name__)


class HealthzHandler(BaseHTTPRequestHandler):
    """Answers Kubernetes liveness/readiness probes on /healthz."""

    def do_GET(self):
        """Reply 200 "ok" on /healthz and 404 on anything else.

        Returns:
            None.
        """
        if self.path == "/healthz":
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"ok\n")
        else:
            self.send_response(404)
            self.end_headers()


def prepare_database() -> None:
    """Create the tables and load seed rules and reference lists if they're empty.

    Returns:
        None.
    """
    init_db()
    seed_reference_lists()
    seed_default_rules()


def run_pipeline_once() -> Dict[str, object]:
    """Run one cycle: sync orders from Fusion, validate new ones, close released ones.

    Returns:
        A summary: sync counts, number validated, number closed.
    """
    sync_counts = sync_orders()
    validated = validate_pending_orders()
    closed = verify_released_orders()
    return {"sync": sync_counts, "validated": validated, "closed": closed}


def run_healthz_server(port: int) -> None:
    """Serve /healthz forever (meant to run in a background thread).

    Args:
        port: TCP port to listen on.

    Returns:
        None (never returns).
    """
    HTTPServer(("0.0.0.0", port), HealthzHandler).serve_forever()


def run_poll_loop(interval_seconds: int, max_cycles: Optional[int] = None) -> None:
    """Run the pipeline repeatedly. A failing cycle is logged and the loop carries on.

    Args:
        interval_seconds: Seconds to wait between cycles.
        max_cycles: Stop after this many cycles (None = forever; used by tests).

    Returns:
        None.
    """
    cycle = 0
    while max_cycles is None or cycle < max_cycles:
        cycle += 1
        try:
            logger.info("Pipeline cycle %d: %s", cycle, run_pipeline_once())
        except Exception:
            logger.exception("Pipeline cycle %d failed", cycle)
        if max_cycles is None or cycle < max_cycles:
            time.sleep(interval_seconds)


def main() -> None:
    """Start the health endpoint and the polling loop.

    Returns:
        None (runs until stopped).
    """
    config = load_config()
    prepare_database()
    threading.Thread(
        target=run_healthz_server, args=(config.healthz_port,), daemon=True
    ).start()
    logger.info(
        "Started: FUSION_MODE=%s, LLM %s, polling every %ss, /healthz on :%s",
        config.fusion_mode,
        "on" if config.llm_enabled else "off (template explanations)",
        config.poll_interval_seconds,
        config.healthz_port,
    )
    run_poll_loop(config.poll_interval_seconds)


if __name__ == "__main__":
    main()
