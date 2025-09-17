# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2025 Matthew Watkins <mwatkins@linuxfoundation.org>

"""Centralized logging setup with Rich console integration."""

from __future__ import annotations

import inspect
import logging
from typing import Any

from rich.console import Console
from rich.logging import RichHandler
from rich.theme import Theme

# Custom theme for consistent styling
GERRIT_THEME = Theme(
    {
        "info": "dim cyan",
        "warning": "magenta",
        "error": "bold red",
        "critical": "bold white on red",
        "success": "bold green",
        "progress": "blue",
        "project": "yellow",
        "path": "dim blue",
        "count": "bold cyan",
    }
)


class GerritRichHandler(RichHandler):
    """Custom Rich handler with Gerrit-specific styling."""

    def __init__(self, console: Console | None = None, **kwargs: Any) -> None:
        """Initialize with custom console if not provided."""
        if console is None:
            console = Console(theme=GERRIT_THEME, stderr=True)
        super().__init__(console=console, **kwargs)


# Global reference to current progress tracker for progress-aware logging
_current_progress_tracker: Any = None


class ProgressAwareHandler(logging.Handler):
    """Logging handler that integrates with progress display."""

    def __init__(self, fallback_handler: logging.Handler) -> None:
        """Initialize with fallback handler for non-progress scenarios.

        Args:
            fallback_handler: Handler to use when no progress tracker is active
        """
        super().__init__()
        self.fallback_handler = fallback_handler
        self.setLevel(fallback_handler.level)
        self.setFormatter(fallback_handler.formatter)

    def emit(self, record: logging.LogRecord) -> None:
        """Emit a log record, routing to progress display or fallback."""
        try:
            # Check if we have an active progress tracker
            if (_current_progress_tracker is not None and
                hasattr(_current_progress_tracker, 'update_log_message') and
                record.levelno == logging.INFO):

                # Format the message for progress display
                message = self.format(record)
                # Strip ANSI codes for progress display
                import re
                clean_message = re.sub(r'\x1b\[[0-9;]*m', '', message)
                # Update progress tracker log line
                _current_progress_tracker.update_log_message(clean_message)
            else:
                # Use fallback handler for non-INFO messages or when no progress tracker
                self.fallback_handler.emit(record)
        except Exception:
            # If anything goes wrong, fall back to regular logging
            self.fallback_handler.emit(record)


def set_progress_tracker(tracker: Any) -> None:
    """Set the current progress tracker for progress-aware logging.

    Args:
        tracker: Progress tracker instance with update_log_message method
    """
    global _current_progress_tracker
    _current_progress_tracker = tracker


def clear_progress_tracker() -> None:
    """Clear the current progress tracker reference."""
    global _current_progress_tracker
    _current_progress_tracker = None


def setup_logging(
    level: str = "INFO",
    quiet: bool = False,
    verbose: bool = False,
    console: Console | None = None,
    enable_progress_aware: bool = False,
) -> logging.Logger:
    """Set up centralized logging with Rich formatting.

    Args:
        level: Log level (DEBUG, INFO, WARNING, ERROR, CRITICAL)
        quiet: Suppress all output except errors
        verbose: Enable verbose/debug output
        console: Optional Rich console instance to use
        enable_progress_aware: Enable progress-aware logging integration

    Returns:
        Configured logger instance
    """
    # Determine effective log level
    if quiet:
        log_level = logging.ERROR
    elif verbose:
        log_level = logging.DEBUG
    else:
        log_level = getattr(logging, level.upper(), logging.INFO)

    # Create console if not provided
    if console is None:
        console = Console(theme=GERRIT_THEME, stderr=True)

    # Configure root logger
    root_logger = logging.getLogger()
    root_logger.setLevel(log_level)

    # Remove existing handlers
    for handler in root_logger.handlers[:]:
        root_logger.removeHandler(handler)

    # Add Rich handler
    rich_handler = GerritRichHandler(
        console=console,
        show_time=True,
        show_level=True,
        show_path=False,
        markup=True,
        rich_tracebacks=True,
        tracebacks_show_locals=verbose,
    )
    rich_handler.setLevel(log_level)

    # Use progress-aware handler if requested
    if enable_progress_aware:
        handler = ProgressAwareHandler(rich_handler)
    else:
        handler = rich_handler

    # Custom format for cleaner output
    formatter = logging.Formatter(
        fmt="%(message)s",
        datefmt="%H:%M:%S",
    )
    rich_handler.setFormatter(formatter)

    root_logger.addHandler(handler)

    # Suppress noisy third-party loggers unless in debug mode
    if not verbose:
        logging.getLogger("httpx").setLevel(logging.WARNING)
        logging.getLogger("httpcore").setLevel(logging.WARNING)
        logging.getLogger("urllib3").setLevel(logging.WARNING)
        logging.getLogger("requests").setLevel(logging.WARNING)

    # Return logger for gerrit_clone package
    return logging.getLogger("gerrit_clone")


def get_logger(name: str | None = None) -> logging.Logger:
    """Get a logger instance.

    Args:
        name: Logger name (defaults to caller's module)

    Returns:
        Logger instance
    """
    if name is None:
        # Get caller's module name
        frame = inspect.currentframe()
        if frame and frame.f_back:
            caller_name = frame.f_back.f_globals.get("__name__", "gerrit_clone")
        else:
            caller_name = "gerrit_clone"
        name = caller_name

    return logging.getLogger(name)


def log_success(message: str, logger: logging.Logger | None = None) -> None:
    """Log a success message with styling."""
    if logger is None:
        logger = get_logger()
    logger.info(f"[success]✓[/success] {message}")


def log_error(message: str, logger: logging.Logger | None = None) -> None:
    """Log an error message with styling."""
    if logger is None:
        logger = get_logger()
    logger.error(f"[error]✗[/error] {message}")


def log_warning(message: str, logger: logging.Logger | None = None) -> None:
    """Log a warning message with styling."""
    if logger is None:
        logger = get_logger()
    logger.warning(f"[warning]⚠[/warning] {message}")


def log_info(message: str, logger: logging.Logger | None = None) -> None:
    """Log an info message with styling."""
    if logger is None:
        logger = get_logger()
    logger.info(f"[info]i[/info] {message}")


def log_project_status(
    project: str,
    status: str,
    details: str = "",
    logger: logging.Logger | None = None,
) -> None:
    """Log project-specific status with consistent formatting."""
    if logger is None:
        logger = get_logger()

    status_styles = {
        "success": "[success]✓[/success]",
        "failed": "[error]✗[/error]",
        "retry": "[warning]⟲[/warning]",
        "skipped": "[dim]⊘[/dim]",
        "cloning": "[progress]⬇[/progress]",
    }

    style = status_styles.get(status.lower(), "")
    project_styled = f"[project]{project}[/project]"

    if details:
        message = f"{style} {project_styled} - {details}"
    else:
        message = f"{style} {project_styled}"

    logger.info(message)


def create_console(
    quiet: bool = False,
    no_color: bool = False,
    force_terminal: bool | None = None,
) -> Console:
    """Create a Rich console with appropriate settings.

    Args:
        quiet: Minimize output
        no_color: Disable color output
        force_terminal: Force terminal detection override

    Returns:
        Configured Rich Console instance
    """
    return Console(
        theme=GERRIT_THEME,
        stderr=True,
        quiet=quiet,
        force_terminal=force_terminal,
        no_color=no_color,
        width=None,  # Auto-detect
        legacy_windows=False,
    )
