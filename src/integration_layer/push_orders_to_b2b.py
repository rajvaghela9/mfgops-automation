"""Releases an approved order to Arrow through B2B.

Release = the "Infoblox Priority Release Pause Task" in Fusion; Fusion and Boomi then send
the PO XML to Arrow and Fusion creates the USPO. Without Fusion API access, a MfgOps user
runs the release in Fusion by hand ("manual" strategy): clicking Approve means "I have
released it in Fusion", and the order is confirmed as released when its USPO shows up in
a backlog report mail. FUSION_MODE=live will submit the ESS job instead ("ess" strategy)
once Fusion IT provides the job definition.
"""

from src.utils.config import load_config
from src.utils.logging_config import get_logger

logger = get_logger(__name__)


MANUAL_RELEASE = "manual"
ESS_RELEASE = "ess"


def release_strategy() -> str:
    """Say how approved orders are released.

    Returns:
        ESS_RELEASE when FUSION_MODE=live, otherwise MANUAL_RELEASE (done in Fusion by
        a MfgOps user).
    """
    return ESS_RELEASE if load_config().fusion_mode == "live" else MANUAL_RELEASE


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
