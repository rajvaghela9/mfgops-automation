"""One place to configure logging so every module logs in the same format."""

import logging

LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"


def get_logger(name: str) -> logging.Logger:
    """Return a logger that writes in the shared format.

    Args:
        name: Usually the calling module's __name__.

    Returns:
        A configured logging.Logger.
    """
    if not logging.getLogger().handlers:
        logging.basicConfig(level=logging.INFO, format=LOG_FORMAT)
    return logging.getLogger(name)
