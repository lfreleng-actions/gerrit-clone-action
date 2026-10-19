# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""Collection and summarisation of errors raised during a run.

Gathers warnings, errors and critical failures as they are logged so a
run can report everything that went wrong at the end, rather than
leaving the operator to reconstruct it from the log stream.

:class:`CollectingHandler` is the bridge from the logging module;
:class:`ErrorCollector` owns the accumulated records and the JSON
summary written alongside the log file.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from pathlib import Path

__all__ = ["CollectingHandler", "ErrorCollector", "ErrorRecord"]


class ErrorRecord:
    """Individual error/warning record for aggregation."""

    def __init__(
        self,
        timestamp: datetime,
        message: str,
        level: int,
        context: str = "",
        exception: BaseException | None = None,
    ):
        self.timestamp = timestamp
        self.message = message
        self.level = level
        self.context = context
        self.exception = exception

    def to_dict(self) -> dict[str, Any]:
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
        self.errors: list[ErrorRecord] = []
        self.warnings: list[ErrorRecord] = []
        self.critical_errors: list[ErrorRecord] = []

    def add_error(
        self, message: str, context: str = "", exception: BaseException | None = None
    ) -> None:
        """Add an error message."""
        record = ErrorRecord(
            timestamp=datetime.now(UTC),
            message=message,
            level=logging.ERROR,
            context=context,
            exception=exception,
        )
        self.errors.append(record)

    def add_warning(
        self, message: str, context: str = "", exception: BaseException | None = None
    ) -> None:
        """Add a warning message."""
        record = ErrorRecord(
            timestamp=datetime.now(UTC),
            message=message,
            level=logging.WARNING,
            context=context,
            exception=exception,
        )
        self.warnings.append(record)

    def add_critical_error(
        self, message: str, context: str = "", exception: BaseException | None = None
    ) -> None:
        """Add a critical error message."""
        record = ErrorRecord(
            timestamp=datetime.now(UTC),
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

    def get_summary(self) -> dict[str, Any]:
        """Get summary of collected issues."""
        return {
            "critical_errors": len(self.critical_errors),
            "errors": len(self.errors),
            "warnings": len(self.warnings),
            "total": self.get_total_count(),
        }

    def to_dict(self) -> dict[str, Any]:
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

                for category, records in [
                    ("CRITICAL ERRORS", self.critical_errors),
                    ("ERRORS", self.errors),
                    ("WARNINGS", self.warnings),
                ]:
                    if records:
                        f.write(f"{category}:\n")
                        f.write("-" * 20 + "\n")
                        for record in records:
                            f.write(
                                f"[{record.timestamp.strftime('%H:%M:%S')}] {record.message}\n"
                            )
                            if record.context:
                                f.write(f"  Context: {record.context}\n")
                            if record.exception:
                                f.write(f"  Exception: {record.exception}\n")
                        f.write("\n")
        except Exception:
            # If we can't write to the log file, fall back to the standard
            # logging subsystem, which records context and routes to stderr.
            logging.getLogger(__name__).warning(
                "Failed to write error summary to log file", exc_info=True
            )


class CollectingHandler(logging.Handler):
    """Handler that collects errors/warnings for summary reporting."""

    def __init__(self, collector: ErrorCollector) -> None:
        super().__init__()
        self.collector = collector

    def emit(self, record: logging.LogRecord) -> None:
        """Collect error/warning messages."""
        try:
            message = self.format(record)
            context = getattr(record, "context", "")
            # record.exc_info is a (type, value, traceback) triple; the
            # collector wants the exception itself, as direct callers pass.
            exc_info = record.exc_info
            exception = exc_info[1] if exc_info else None

            if record.levelno >= logging.CRITICAL:
                self.collector.add_critical_error(message, context, exception)
            elif record.levelno >= logging.ERROR:
                self.collector.add_error(message, context, exception)
            elif record.levelno >= logging.WARNING:
                self.collector.add_warning(message, context, exception)
        except Exception:
            # Route handler failures through the standard logging machinery
            # instead of silently dropping them.
            self.handleError(record)
