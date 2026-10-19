# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2025 Matthew Watkins <mwatkins@linuxfoundation.org>

"""Unified logging system for source-clone.

This module provides both file-based logging and console logging setup.
The init_logging() function is the main entry point that sets up both
file logging (detailed debug info) and console logging (Rich-formatted
output for HTTP debug messages when --verbose is used).

The beautiful Rich UI panels and progress bars are handled separately
in rich_status.py and are not part of the logging system.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from gerrit_clone.error_collection import (
    CollectingHandler,
    ErrorCollector,
    ErrorRecord,
)
from gerrit_clone.logging import setup_logging

if TYPE_CHECKING:
    from typing import TextIO

__all__ = [
    "CollectingHandler",
    "ErrorCollector",
    "ErrorRecord",
    "FileLogger",
    "cli_args_to_dict",
    "get_default_log_path",
    "init_logging",
    "setup_file_logging",
]


def _format_cli_command(cli_args: dict[str, Any], command: str) -> str:
    """Reconstruct the invoked command line from parsed CLI arguments.

    Args:
        cli_args: Mapping of option name to value.
        command: Subcommand the run was invoked with (e.g. ``"mirror"``).

    Returns:
        The reconstructed command line.
    """
    cmd_parts = ["source-clone", command]
    for key, value in cli_args.items():
        if value is None or value is False:
            continue
        flag = f"--{key.replace('_', '-')}"
        if value is True:
            cmd_parts.append(flag)
        else:
            cmd_parts.extend([flag, str(value)])
    return " ".join(cmd_parts)


def _write_log_header(
    stream: TextIO, cli_args: dict[str, Any] | None, command: str
) -> None:
    """Write the execution-log preamble.

    Args:
        stream: Open, writable log file.
        cli_args: Parsed CLI arguments to record, if any.
        command: Subcommand the run was invoked with.
    """
    stream.write("=" * 60 + "\n")
    stream.write("GERRIT CLONE EXECUTION LOG\n")
    stream.write("=" * 60 + "\n")
    stream.write(f"Date: {datetime.now(UTC).strftime('%Y-%m-%d %H:%M:%S UTC')}\n")

    if cli_args:
        stream.write(f"Command: {_format_cli_command(cli_args, command)}\n")
        stream.write("\nCLI Arguments:\n")

        for key, value in sorted(cli_args.items()):
            stream.write(f"  {key}: {value}\n")

    stream.write("\n" + "=" * 60 + "\n")
    stream.write("LOG STREAM\n")
    stream.write("=" * 60 + "\n")


class FileLogger:
    """Manages file-based logging separate from terminal output."""

    def __init__(
        self,
        log_file_path: Path | None = None,
        enabled: bool = True,
        log_level: str = "DEBUG",
    ):
        self.log_file_path = log_file_path or Path("source-clone.log")
        self.enabled = enabled
        self.log_level = getattr(logging, log_level.upper(), logging.DEBUG)
        self.error_collector = ErrorCollector()
        self._file_handler: logging.FileHandler | None = None
        self._collector_handler: CollectingHandler | None = None

    def create_log_file(
        self, cli_args: dict[str, Any] | None = None, command: str = "clone"
    ) -> Path:
        """Create log file with header containing CLI arguments."""
        if not self.enabled:
            return self.log_file_path

        try:
            # Ensure parent directory exists
            self.log_file_path.parent.mkdir(parents=True, exist_ok=True)

            with self.log_file_path.open("w", encoding="utf-8") as f:
                _write_log_header(f, cli_args, command)

            return self.log_file_path

        except Exception:
            logging.getLogger(__name__).warning(
                "Failed to create log file %s", self.log_file_path, exc_info=True
            )
            self.enabled = False
            return self.log_file_path

    def setup_file_handlers(self, logger_name: str = "gerrit_clone") -> logging.Logger:
        """Setup file-based logging handlers."""
        logger = logging.getLogger(logger_name)

        # Remove existing handlers to avoid conflicts
        for handler in logger.handlers[:]:
            logger.removeHandler(handler)

        # Always add error collector (even if file logging disabled)
        self._collector_handler = CollectingHandler(self.error_collector)
        collector_formatter = logging.Formatter(
            fmt="%(asctime)s [%(levelname)s]: %(message)s",
            datefmt="%H:%M:%S",
        )
        self._collector_handler.setFormatter(collector_formatter)
        self._collector_handler.setLevel(
            logging.WARNING
        )  # Only collect warnings and above
        logger.addHandler(self._collector_handler)

        # Add file handler if logging is enabled
        if self.enabled and self.log_file_path:
            try:
                self._file_handler = logging.FileHandler(
                    self.log_file_path,
                    mode="a",
                    encoding="utf-8",
                )

                file_formatter = logging.Formatter(
                    fmt="[%(asctime)s] %(levelname)s: %(message)s",
                    datefmt="%Y-%m-%d %H:%M:%S",
                )
                self._file_handler.setFormatter(file_formatter)
                self._file_handler.setLevel(self.log_level)

                logger.addHandler(self._file_handler)
                logger.setLevel(
                    min(self.log_level, logging.WARNING)
                )  # Ensure we capture warnings for collector

            except Exception:
                logging.getLogger(__name__).warning(
                    "Failed to setup file logging", exc_info=True
                )
                self.enabled = False

        # Allow propagation to root logger so console handlers receive messages
        logger.propagate = True

        return logger

    def get_error_collector(self) -> ErrorCollector:
        """Get the error collector instance."""
        return self.error_collector

    def write_final_summary(self) -> None:
        """Write final summary to log file."""
        if self.enabled and self.log_file_path:
            self.error_collector.write_summary_to_file(self.log_file_path)

    def close(self) -> None:
        """Close file handlers and write final summary."""
        try:
            self.write_final_summary()

            if self._file_handler:
                self._file_handler.close()
                self._file_handler = None

            if self._collector_handler:
                self._collector_handler = None

        except Exception:
            logging.getLogger(__name__).warning(
                "Error closing file logger", exc_info=True
            )


def setup_file_logging(
    log_file_path: Path | None = None,
    enabled: bool = True,
    log_level: str = "DEBUG",
    cli_args: dict[str, Any] | None = None,
    command: str = "clone",
) -> tuple[logging.Logger, ErrorCollector]:
    """
    Setup file-based logging system.

    Args:
        log_file_path: Path to log file (default: source-clone.log)
        enabled: Whether to enable file logging
        log_level: Logging level for file output
        cli_args: CLI arguments to include in log header
        command: Subcommand the run was invoked with, recorded in the
            log header

    Returns:
        Tuple of (logger, error_collector)
    """
    file_logger = FileLogger(
        log_file_path=log_file_path,
        enabled=enabled,
        log_level=log_level,
    )

    actual_log_path = file_logger.create_log_file(cli_args, command)

    logger = file_logger.setup_file_handlers()

    if enabled:
        logger.debug("File logging initialized: %s", actual_log_path)
        logger.debug("Log level: %s", log_level)
        if cli_args:
            logger.debug("CLI arguments logged to file header")

    return logger, file_logger.get_error_collector()


def file_logging_active(logger_name: str = "gerrit_clone") -> bool:
    """Report whether a log file is being written for this run.

    False when file logging was disabled, and when the log file could
    not be created: :class:`FileLogger` then turns itself off and
    attaches no file handler.  Callers use this before naming or
    writing the log file, since the path alone says neither.
    """
    return any(
        isinstance(handler, logging.FileHandler)
        for handler in logging.getLogger(logger_name).handlers
    )


def get_default_log_path(host: str | None = None, path: Path | None = None) -> Path:
    """Get default log file path in path directory (or current working directory).

    Args:
        host: Gerrit server hostname or URL to use in log filename
        path: Base directory for log file (defaults to current working directory)

    Returns:
        Path to log file with dynamic name based on hostname
    """
    # Determine base directory for log file
    base_dir = path if path is not None else Path.cwd()

    if host and host.strip():
        # Sanitize hostname for filename (replace slashes with dots to include org)
        # Examples:
        #   "gerrit.onap.org" -> "gerrit.onap.org"
        #   "github.com/opennetworkinglab" -> "github.com.opennetworkinglab"
        #   "github.example.com/myorg" -> "github.example.com.myorg"

        # 1. Remove port number (everything after first colon)
        clean_host = host.split(":")[0]

        # 2. Replace path separators with dots to preserve org/user structure
        clean_host = clean_host.replace("/", ".").replace("\\", ".")

        # 3. Replace any remaining problematic characters
        clean_host = clean_host.replace(":", "_")

        # 4. Strip whitespace and ensure we have something left
        clean_host = clean_host.strip()

        if clean_host:
            return base_dir / f"{clean_host}.log"

    return base_dir / "source-clone.log"


def init_logging(
    *,
    log_file: Path | None = None,
    disable_file: bool = False,
    log_level: str = "DEBUG",
    console_level: str = "INFO",
    quiet: bool = False,
    verbose: bool = False,
    cli_args: dict[str, Any] | None = None,
    host: str | None = None,
    path: Path | None = None,
    command: str = "clone",
) -> tuple[logging.Logger, ErrorCollector]:
    """Initialize both file and console logging in one place.

    This is the unified logging setup function that replaces separate
    setup_file_logging + setup_logging calls.

    Args:
        log_file: Path to log file (default: source-clone.log)
        disable_file: Whether to disable file logging
        log_level: Logging level for file output
        console_level: Base logging level for console (overridden by quiet/verbose)
        quiet: Suppress console output except errors
        verbose: Enable verbose console output
        cli_args: CLI arguments to include in log header
        host: Gerrit server hostname for dynamic log file naming
        path: Base directory for log file (defaults to current working directory)
        command: Subcommand the run was invoked with. Recorded in the log
            header, which several subcommands share, so it must name the
            command actually running rather than assume "clone".

    Returns:
        Tuple of (file_logger, error_collector)
    """
    # Set up file logging (unchanged behavior)
    log_path = log_file or get_default_log_path(host, path)
    file_logger, collector = setup_file_logging(
        log_file_path=log_path,
        enabled=not disable_file,
        log_level=log_level,
        cli_args=cli_args,
        command=command,
    )

    # Set up console logging (unchanged behavior)
    setup_logging(
        level=console_level,
        quiet=quiet,
        verbose=verbose,
    )

    return file_logger, collector


def cli_args_to_dict(**kwargs: Any) -> dict[str, Any]:
    """Convert CLI arguments to dictionary for logging."""
    # Filter out None values and internal parameters
    filtered_args = {}
    skip_keys = {"console", "logger", "config_file_content"}
    # Keys whose values may carry credentials must never be written
    # verbatim to the log file (e.g. git_filter can embed real
    # tokens when supplied via GERRIT_GIT_FILTER).
    sensitive_keys = {"git_filter", "github_token"}

    for key, value in kwargs.items():
        if key not in skip_keys and value is not None:
            if key in sensitive_keys:
                filtered_args[key] = "<redacted>"
            # Convert Path objects to strings
            elif isinstance(value, Path):
                filtered_args[key] = str(value)
            # Convert lists to comma-separated strings for readability
            elif isinstance(value, list):
                filtered_args[key] = ", ".join(str(item) for item in value)
            else:
                filtered_args[key] = value

    return filtered_args
