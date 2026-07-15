# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2025 Matthew Watkins <mwatkins@linuxfoundation.org>

"""Logging helpers with dual-channel architecture.

The root logger is held at WARNING so third-party libraries stay quiet
by default.  Application output is routed through the ``gerrit_clone``
logger hierarchy, whose level is unlocked independently by --verbose
or --quiet.  File-based logging continues to live in file_logging.py.
"""

from __future__ import annotations

import inspect
import logging
from contextlib import contextmanager
from typing import TYPE_CHECKING, Any

from rich.console import Console
from rich.logging import RichHandler
from rich.theme import Theme

if TYPE_CHECKING:
    from collections.abc import Generator

# Minimal theme (can be expanded later if reused elsewhere)
GERRIT_THEME = Theme(
    {
        "info": "cyan",
        "warning": "magenta",
        "error": "bold red",
        "critical": "bold white on red",
        "success": "green",
    }
)


class GerritRichHandler(RichHandler):
    """Thin wrapper around RichHandler retained for backward compatibility with tests."""

    def __init__(self, console: Console | None = None, **kwargs: Any) -> None:
        if console is None:
            console = Console(theme=GERRIT_THEME, stderr=True)
        # Pass through kwargs so tests using show_time/show_level etc. keep working
        super().__init__(console=console, **kwargs)


def setup_logging(
    level: str = "INFO",
    quiet: bool = False,
    verbose: bool = False,
    console: Console | None = None,
) -> logging.Logger:
    """Set up console logging with dual-channel architecture.

    The root logger is kept at WARNING so third-party libraries stay
    quiet by default.  ``--verbose`` only enables DEBUG for the
    ``gerrit_clone`` logger hierarchy, avoiding the need for an
    ever-growing suppression list.

    In normal mode, application messages should use ``console.print``
    (or ``log_and_print``) for user-facing output.  The logging
    subsystem captures everything for file logging and structured
    diagnostics.

    Args:
        level: Base log level — in normal mode, the console handler
            is set to ``max(WARNING, level)`` so only WARNING and
            above reach the terminal.  User-facing output should
            use ``console.print`` or ``log_and_print`` instead.
            Defaults to ``"INFO"``.
        quiet: If True, only errors and above
        verbose: If True, debug logging enabled for gerrit_clone
        console: Optional Rich Console to direct output to
    """
    if console is None:
        console = Console(theme=GERRIT_THEME, stderr=True)

    root = logging.getLogger()

    # Clear existing handlers to avoid duplicates in tests
    for h in root.handlers[:]:
        root.removeHandler(h)

    handler = GerritRichHandler(
        console=console,
        show_time=verbose,  # Show timestamps in verbose mode
        show_level=True,
        show_path=verbose,  # Show module path in verbose mode
        markup=True,
        rich_tracebacks=verbose,
        tracebacks_show_locals=verbose,
    )
    handler.setFormatter(logging.Formatter("%(message)s"))

    # Resolve the caller-supplied level string to a numeric constant
    # so it can serve as a floor for the handler in normal mode.
    base_level = getattr(logging, level.upper(), logging.INFO)

    if quiet:
        # Quiet: only errors reach the console
        root.setLevel(logging.WARNING)
        handler.setLevel(logging.ERROR)
    elif verbose:
        # Verbose: root stays at WARNING, app logger goes to DEBUG
        root.setLevel(logging.WARNING)
        handler.setLevel(logging.DEBUG)
    else:
        # Normal: root at WARNING, handler at the higher of WARNING
        # and the caller-supplied *level*.  User-facing output goes
        # through console.print / log_and_print.
        root.setLevel(logging.WARNING)
        handler.setLevel(max(logging.WARNING, base_level))

    root.addHandler(handler)

    # Unlock the gerrit_clone logger hierarchy based on mode
    app_logger = logging.getLogger("gerrit_clone")
    if verbose:
        desired_app_level = logging.DEBUG
    elif quiet:
        desired_app_level = logging.ERROR
    else:
        # In normal mode, honour the caller-supplied base level and
        # avoid overriding a more-verbose configuration that may have
        # been set up by file_logging.
        desired_app_level = base_level

    current_level = app_logger.level
    # Only *lower* (make more verbose) the logger level — never
    # raise it.  This preserves a DEBUG/INFO level that file_logging
    # may have already configured, so file output keeps capturing
    # everything.  --quiet suppresses the *console* via the handler
    # level (ERROR) set above; it intentionally does NOT touch the
    # logger level when file logging is active, because the logger
    # gate must remain open for records to reach the file handler.
    if current_level == logging.NOTSET or current_level > desired_app_level:
        app_logger.setLevel(desired_app_level)

    # Suppress noisy third-party HTTP transport modules
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    logging.getLogger("requests").setLevel(logging.WARNING)

    return app_logger


def get_logger(name: str | None = None) -> logging.Logger:
    """Return a logger for the caller (or provided name).

    Falls back to 'gerrit_clone' when the caller cannot be determined.
    """
    if name is None:
        frame = inspect.currentframe()
        if frame and frame.f_back:
            caller_name = frame.f_back.f_globals.get("__name__", "gerrit_clone")
        else:
            caller_name = "gerrit_clone"
        name = caller_name

    return logging.getLogger(name)


class _SuppressConsoleFilter(logging.Filter):
    """Thread-safe filter to suppress console output during Rich display.

    This filter blocks all log records when suppression is active.
    It's designed to be added/removed from handlers in a thread-safe manner.
    """

    def filter(self, record: logging.LogRecord) -> bool:  # noqa: ARG002
        """Block all records when this filter is active."""
        return False


@contextmanager
def suppress_console_logging(verbose: bool = False) -> Generator[None, None, None]:
    """Context manager to temporarily suppress console logging handlers.

    This is used during Rich Live display to prevent log messages from
    interfering with the progress display. File logging continues normally.

    Uses a logging.Filter approach which is thread-safe and doesn't mutate
    handler state directly.

    Args:
        verbose: If True, don't suppress logging (useful for debugging)

    Yields:
        None
    """
    # In verbose mode, don't suppress logging - users want to see everything
    if verbose:
        yield
        return

    root = logging.getLogger()

    # Find all RichHandler instances
    rich_handlers = [h for h in root.handlers if isinstance(h, RichHandler)]

    suppress_filter = _SuppressConsoleFilter()

    # Add filter to all RichHandler instances
    for handler in rich_handlers:
        handler.addFilter(suppress_filter)

    try:
        yield
    finally:
        for handler in rich_handlers:
            handler.removeFilter(suppress_filter)
