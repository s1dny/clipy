import logging
import os

import colorlog


def setup_logging():
    """Configure logging with colored output and timestamps."""
    logger = logging.getLogger("clipper")
    if logger.handlers:
        return

    handler = colorlog.StreamHandler()
    handler.setFormatter(
        colorlog.ColoredFormatter(
            "%(log_color)s%(asctime)s [%(levelname)s] %(reset)s%(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
            log_colors={
                "DEBUG": "cyan",
                "INFO": "green",
                "WARNING": "yellow",
                "ERROR": "red",
                "CRITICAL": "red,bg_white",
            },
        )
    )

    # Enable debug by default, or control via env var
    log_level = os.environ.get("CLIPPER_LOG_LEVEL", "DEBUG")
    logger.setLevel(getattr(logging, log_level.upper(), logging.DEBUG))
    logger.addHandler(handler)
    logger.propagate = False
