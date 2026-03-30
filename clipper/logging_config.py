import logging
import os

from rich.console import Console
from rich.logging import RichHandler
from rich.progress import Progress
from rich.theme import Theme

_THEME = Theme(
    {
        "logging.level.debug": "cyan",
        "logging.level.info": "green",
        "logging.level.warning": "yellow",
        "logging.level.error": "bold red",
        "logging.level.critical": "bold white on red",
    }
)
_CONSOLE = Console(theme=_THEME)


def get_console() -> Console:
    """Return the shared Rich console used by logs and progress displays."""
    return _CONSOLE


def create_progress(*columns, transient: bool = False) -> Progress:
    """Create a Rich progress instance bound to the shared console."""
    return Progress(*columns, console=_CONSOLE, transient=transient, expand=True)


def setup_logging():
    """Configure project logging with Rich output and tracebacks."""
    logger = logging.getLogger("clipper")
    if logger.handlers:
        return

    handler = RichHandler(
        console=_CONSOLE,
        show_path=False,
        omit_repeated_times=False,
        markup=False,
        rich_tracebacks=True,
        tracebacks_show_locals=False,
        log_time_format="%Y-%m-%d %H:%M:%S",
    )
    handler.setFormatter(logging.Formatter("%(message)s"))

    log_level = os.environ.get("CLIPPER_LOG_LEVEL", "DEBUG")
    logger.setLevel(getattr(logging, log_level.upper(), logging.DEBUG))
    logger.addHandler(handler)
    logger.propagate = False
