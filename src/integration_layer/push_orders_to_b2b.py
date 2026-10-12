"""Releases an approved order to Arrow through B2B. Release is the last step of the flow.

Release = submitting the "Infoblox Priority Release Pause Task" ESS job for the order;
Fusion and Boomi then send the PO XML to Arrow. The USPO that Arrow creates shows up in
the backlog report, so it is not read back here. Live ESS submission is pending the job
definition from Fusion IT; mock mode simulates it.
"""

from src.utils.config import load_config
from src.utils.logging_config import get_logger

logger = get_logger(__name__)


def submit_priority_release_job_live(order_no: str) -> str:
    """Submit the release ESS job in Fusion (POST /ess/rest/scheduler/v1/requests).

    Args:
        order_no: Fusion order number.

    Returns:
        The ESS request id.
    """
    raise NotImplementedError(
        "Live release is pending: need the ESS job package/name and parameter names of "
        "'Infoblox Priority Release Pause Task', and an integration user allowed to submit it."
    )


def submit_priority_release_job_mock(order_no: str) -> str:
    """Pretend to submit the release job.

    Args:
        order_no: Fusion order number.

    Returns:
        A fake ESS request id.
    """
    logger.info("[mock] Submitted Priority Release Pause Task for %s", order_no)
    return f"MOCK-ESS-{order_no}"


def release_order_to_arrow(order_no: str) -> str:
    """Release one order to Arrow (live Fusion or mock, per FUSION_MODE).

    Args:
        order_no: Fusion order number.

    Returns:
        The release request id.
    """
    if load_config().fusion_mode == "live":
        return submit_priority_release_job_live(order_no)
    return submit_priority_release_job_mock(order_no)
