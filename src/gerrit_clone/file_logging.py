# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2025 Matthew Watkins <mwatkins@linuxfoundation.org>

"""Unified logging system for gerrit-clone.

This module provides both file-based logging and console logging setup.
The init_logging() function is the main entry point that sets up both
file logging (detailed debug info) and console logging (Rich-formatted
output for HTTP debug messages when --verbose is used).

The beautiful Rich UI panels and progress bars are handled separately
in rich_status.py and are not part of the logging system.
"""

from __future__ import annotations

import json
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional


class ErrorRecord:
    """Individual error/warning record for aggregation."""

    def __init__(
        self,
        timestamp: datetime,
        message: str,
        level: int,
        context: str = "",
        exception: Optional[Exception] = None,
    ):
        self.timestamp = timestamp
        self.message = message
        self.level = level
        self.context = context
        self.exception = exception

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for JSON serialization."""
        return {
            "timestamp": self.timestamp.isoformat(),
            "message": self.message,
            "level": logging.getLevelName(self.level),
            "context": self.context,
            "exception": str(self.exception) if self.exception else None,
        }


class ErrorCollector:
    """Collects errors and warnings for end-of-run summary."""

    def __init__(self) -> None:
        self.errors: List[ErrorRecord] = []
        self.warnings: List[ErrorRecord] = []
        self.critical_errors: List[ErrorRecord] = []

    def add_error(self, message: str, context: str = "", exception: Optional[Exception] = None) -> None:
        """Add an error message."""
        record = ErrorRecord(
            timestamp=datetime.now(timezone.utc),
            message=message,
            level=logging.ERROR,
            context=context,
            exception=exception,
        )
        self.errors.append(record)

    def add_warning(self, message: str, context: str = "", exception: Optional[Exception] = None) -> None:
        """Add a warning message."""
        record = ErrorRecord(
            timestamp=datetime.now(timezone.utc),
            message=message,
            level=logging.WARNING,
            context=context,
            exception=exception,
        )
        self.warnings.append(record)

    def add_critical_error(self, message: str, context: str = "", exception: Optional[Exception] = None) -> None:
        """Add a critical error message."""
        record = ErrorRecord(
            timestamp=datetime.now(timezone.utc),
            message=message,
            level=logging.CRITICAL,
            context=context,
            exception=exception,
        )
        self.critical_errors.append(record)

    def has_errors(self) -> bool:
        """Check if any errors have been collected."""
        return bool(self.errors or self.critical_errors)

    def has_warnings(self) -> bool:
        """Check if any warnings have been collected."""
        return bool(self.warnings)

    def get_total_count(self) -> int:
        """Get total number of issues collected."""
        return len(self.errors) + len(self.warnings) + len(self.critical_errors)

    def get_summary(self) -> Dict[str, Any]:
        """Get summary of collected issues."""
        return {
            "critical_errors": len(self.critical_errors),
            "errors": len(self.errors),
            "warnings": len(self.warnings),
            "total": self.get_total_count(),
        }

    def to_dict(self) -> Dict[str, Any]:
        """Convert all collected issues to dictionary."""
        return {
            "summary": self.get_summary(),
            "critical_errors": [record.to_dict() for record in self.critical_errors],
            "errors": [record.to_dict() for record in self.errors],
            "warnings": [record.to_dict() for record in self.warnings],
        }

    def write_summary_to_file(self, log_file_path: Path) -> None:
        """Append summary of issues to log file."""
        if not self.get_total_count():
            return

        try:
            with log_file_path.open("a", encoding="utf-8") as f:
                f.write("\n" + "=" * 50 + "\n")
                f.write("ERROR AND WARNING SUMMARY\n")
                f.write("=" * 50 + "\n")

                summary = self.get_summary()
                f.write(f"Total Issues: {summary['total']}\n")
                f.write(f"Critical Errors: {summary['critical_errors']}\n")
                f.write(f"Errors: {summary['errors']}\n")
                f.write(f"Warnings: {summary['warnings']}\n\n")

                # Write detailed records
                for category, records in [
                    ("CRITICAL ERRORS", self.critical_errors),
                    ("ERRORS", self.errors),
                    ("WARNINGS", self.warnings),
                ]:
                    if records:
                        f.write(f"{category}:\n")
                        f.write("-" * 20 + "\n")
                        for record in records:
                            f.write(f"[{record.timestamp.strftime('%H:%M:%S')}] {record.message}\n")
                            if record.context:
                                f.write(f"  Context: {record.context}\n")
                            if record.exception:
                                f.write(f"  Exception: {record.exception}\n")
                        f.write("\n")
        except Exception as e:
            # If we can't write to log file, write to stderr as last resort
            print(f"Failed to write error summary to log file: {e}", file=sys.stderr)


class CollectingHandler(logging.Handler):
    """Handler that collects errors/warnings for summary reporting."""

    def __init__(self, collector: ErrorCollector) -> None:
        super().__init__()
        self.collector = collector

    def emit(self, record: logging.LogRecord) -> None:
        """Collect error/warning messages."""
        try:
            message = self.format(record)
            context = getattr(record, 'context', '')
            exception = getattr(record, 'exc_info', None)

            if record.levelno >= logging.CRITICAL:
                self.collector.add_critical_error(message, context, exception)
            elif record.levelno >= logging.ERROR:
                self.collector.add_error(message, context, exception)
            elif record.levelno >= logging.WARNING:
                self.collector.add_warning(message, context, exception)
        except Exception:
            # Don't let logging errors break the collector
            pass


class FileLogger:
    """Manages file-based logging separate from terminal output."""

    def __init__(
        self,
        log_file_path: Optional[Path] = None,
        enabled: bool = True,
        log_level: str = "DEBUG",
    ):
        self.log_file_path = log_file_path or Path("gerrit-clone.log")
        self.enabled = enabled
        self.log_level = getattr(logging, log_level.upper(), logging.DEBUG)
        self.error_collector = ErrorCollector()
        self._file_handler: Optional[logging.FileHandler] = None
        self._collector_handler: Optional[CollectingHandler] = None

    def create_log_file(self, cli_args: Optional[Dict[str, Any]] = None) -> Path:
        """Create log file with header containing CLI arguments."""
        if not self.enabled:
            return self.log_file_path

        try:
            # Ensure parent directory exists
            self.log_file_path.parent.mkdir(parents=True, exist_ok=True)

            # Create/overwrite log file with header
            with self.log_file_path.open("w", encoding="utf-8") as f:
                f.write("=" * 60 + "\n")
                f.write("GERRIT CLONE EXECUTION LOG\n")
                f.write("=" * 60 + "\n")
                f.write(f"Date: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')}\n")

                if cli_args:
                    # Reconstruct command line from args
                    cmd_parts = ["gerrit-clone", "clone"]
                    for key, value in cli_args.items():
                        if value is not None and value is not False:
                            flag = f"--{key.replace('_', '-')}"
                            if value is True:
                                cmd_parts.append(flag)
                            else:
                                cmd_parts.extend([flag, str(value)])

                    f.write(f"Command: {' '.join(cmd_parts)}\n")
                    f.write("\nCLI Arguments:\n")

                    # Write formatted CLI arguments
                    for key, value in sorted(cli_args.items()):
                        f.write(f"  {key}: {value}\n")

                f.write("\n" + "=" * 60 + "\n")
                f.write("LOG STREAM\n")
                f.write("=" * 60 + "\n")

            return self.log_file_path

        except Exception as e:
            print(f"Warning: Failed to create log file {self.log_file_path}: {e}", file=sys.stderr)
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
        self._collector_handler.setLevel(logging.WARNING)  # Only collect warnings and above
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
                logger.setLevel(min(self.log_level, logging.WARNING))  # Ensure we capture warnings for collector

            except Exception as e:
                print(f"Warning: Failed to setup file logging: {e}", file=sys.stderr)
                self.enabled = False

        # Prevent propagation to root logger (which may have console handlers)
        logger.propagate = False

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

        except Exception as e:
            print(f"Warning: Error closing file logger: {e}", file=sys.stderr)


def setup_file_logging(
    log_file_path: Optional[Path] = None,
    enabled: bool = True,
    log_level: str = "DEBUG",
    cli_args: Optional[Dict[str, Any]] = None,
) -> tuple[logging.Logger, ErrorCollector]:
    """
    Setup file-based logging system.

    Args:
        log_file_path: Path to log file (default: gerrit-clone.log)
        enabled: Whether to enable file logging
        log_level: Logging level for file output
        cli_args: CLI arguments to include in log header

    Returns:
        Tuple of (logger, error_collector)
    """
    # Create file logger
    file_logger = FileLogger(
        log_file_path=log_file_path,
        enabled=enabled,
        log_level=log_level,
    )

    # Create log file with header
    actual_log_path = file_logger.create_log_file(cli_args)

    # Setup handlers and get logger
    logger = file_logger.setup_file_handlers()

    # Log startup information
    if enabled:
        logger.debug("File logging initialized: %s", actual_log_path)
        logger.debug("Log level: %s", log_level)
        if cli_args:
            logger.debug("CLI arguments logged to file header")

    return logger, file_logger.get_error_collector()


def get_default_log_path() -> Path:
    """Get default log file path in current working directory."""
    return Path.cwd() / "gerrit-clone.log"


def init_logging(
    *,
    log_file: Optional[Path] = None,
    disable_file: bool = False,
    log_level: str = "DEBUG",
    console_level: str = "INFO",
    quiet: bool = False,
    verbose: bool = False,
    cli_args: Optional[Dict[str, Any]] = None,
) -> tuple[logging.Logger, ErrorCollector]:
    """Initialize both file and console logging in one place.

    This is the unified logging setup function that replaces separate
    setup_file_logging + setup_logging calls.

    Args:
        log_file: Path to log file (default: gerrit-clone.log)
        disable_file: Whether to disable file logging
        log_level: Logging level for file output
        console_level: Base logging level for console (overridden by quiet/verbose)
        quiet: Suppress console output except errors
        verbose: Enable verbose console output
        cli_args: CLI arguments to include in log header

    Returns:
        Tuple of (file_logger, error_collector)
    """
    # Set up file logging (unchanged behavior)
    log_path = log_file or get_default_log_path()
    file_logger, collector = setup_file_logging(
        log_file_path=log_path,
        enabled=not disable_file,
        log_level=log_level,
        cli_args=cli_args,
    )

    # Set up console logging (unchanged behavior)
    from gerrit_clone.logging import setup_logging
    setup_logging(
        level=console_level,
        quiet=quiet,
        verbose=verbose,
    )

    return file_logger, collector


def cli_args_to_dict(**kwargs: Any) -> Dict[str, Any]:
    """Convert CLI arguments to dictionary for logging."""
    # Filter out None values and internal parameters
    filtered_args = {}
    skip_keys = {'console', 'logger', 'config_file_content'}

    for key, value in kwargs.items():
        if key not in skip_keys and value is not None:
            # Convert Path objects to strings
            if isinstance(value, Path):
                filtered_args[key] = str(value)
            # Convert lists to comma-separated strings for readability
            elif isinstance(value, list):
                filtered_args[key] = ', '.join(str(item) for item in value)
            else:
                filtered_args[key] = value

    return filtered_args
