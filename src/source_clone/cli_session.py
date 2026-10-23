# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""Logging state shared between a command body and its error handlers.

The file logger, error collector and log file path only exist once logging has
been initialised, but the command's ``except`` blocks must cope with failures
that happen before that.  Collecting them in one object keeps that
partially-initialised state explicit.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import logging
    from pathlib import Path

    from rich.console import Console

    from source_clone.file_logging import ErrorCollector


@dataclass
class CliSession:
    """Console and logging handles for a single command invocation."""

    console: Console
    file_logger: logging.Logger | None = None
    error_collector: ErrorCollector | None = None
    log_file_path: Path | None = None
    _summary_written: bool = field(default=False, init=False, repr=False)

    def write_summary(self) -> None:
        """Write the collected error summary to the log file, once.

        Some exit paths write the summary and then raise ``typer.Exit``
        through a command handler that writes it again; only the first
        write reaches the file, so issues collected after it are not
        summarised.  A call that writes nothing, because there is no
        collector, no log file or nothing collected yet, does not count,
        so a later call still writes the summary.  The session is marked
        before the write starts, so a write that an interrupt cuts short
        still counts and no later call appends a second summary.
        """
        if self._summary_written:
            return
        collector = self.error_collector
        if collector and self.log_file_path and collector.get_total_count():
            self._summary_written = True
            collector.write_summary_to_file(self.log_file_path)
