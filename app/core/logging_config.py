import logging
import sys


def configure_logging() -> None:
    """Configure a simple structured stdout logger for the application."""
    root_logger = logging.getLogger()
    if root_logger.handlers:
        # Already configured (e.g. reload) - avoid duplicate handlers.
        return

    root_logger.setLevel(logging.INFO)

    handler = logging.StreamHandler(sys.stdout)
    formatter = logging.Formatter(
        fmt="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    handler.setFormatter(formatter)
    root_logger.addHandler(handler)

    # Quiet down noisy third-party loggers a bit.
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)
