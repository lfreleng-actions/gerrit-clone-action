# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""Commands touch a log file only when file logging is active.

File logging is off when ``--disable-log-file`` is given, and when the
log file cannot be created.  Either way, a command must not write its
error summary to the file, report it as the log file, or fail trying.
"""

from __future__ import annotations

import logging
from datetime import datetime
from types import SimpleNamespace
from typing import TYPE_CHECKING, cast
from unittest.mock import MagicMock, patch

import pytest
from rich.console import Console
from typer.testing import CliRunner

from source_clone.cli import app
from source_clone.cli_mirror_setup import start_logging as start_mirror_logging
from source_clone.cli_reset_run import _start_logging as start_reset_logging
from source_clone.cli_session import CliSession
from source_clone.error_codes import DiscoveryError
from source_clone.file_logging import file_logging_active, init_logging

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

    from source_clone.cli_mirror_models import MirrorRequest
    from source_clone.cli_reset_run import ResetRequest
    from source_clone.models import Config

HOST = "gerrit.example.org"
LOG_NAME = f"{HOST}.log"
SUMMARY_FAILURE = "Failed to write error summary"
SUMMARY_HEADING = "ERROR AND WARNING SUMMARY"


@pytest.fixture(autouse=True)
def _close_file_handlers() -> Iterator[None]:
    """Release any log file a command left open on the app logger."""
    yield
    app_logger = logging.getLogger("source_clone")
    for handler in app_logger.handlers[:]:
        if isinstance(handler, logging.FileHandler):
            handler.close()
            app_logger.removeHandler(handler)


def _fail_discovery(_config: Config) -> MagicMock:
    """Fail the way an early discovery error does, after logging it."""
    logging.getLogger("source_clone.discovery").error("discovery failed")
    raise DiscoveryError("Discovery failed")


def _succeed(_config: Config) -> MagicMock:
    """Return an empty successful batch result."""
    now = datetime.now()
    return MagicMock(
        started_at=now,
        completed_at=now,
        total_count=0,
        success_count=0,
        failed_count=0,
        skipped_count=0,
        refreshed_count=0,
        results=[],
        success_rate=100.0,
    )


def _clone(output_path: Path, *extra: str, fail: bool) -> tuple[int, str]:
    """Run ``clone`` with cloning stubbed; return exit code and output."""
    args = ["clone", "--host", HOST, "--output-path", str(output_path), *extra]
    stub = _fail_discovery if fail else _succeed
    with patch("source_clone.cli.clone_repositories", side_effect=stub):
        result = CliRunner().invoke(app, args)
    return result.exit_code, result.output


class TestCloneWithLogFileDisabled:
    """``clone --disable-log-file`` never touches a log file."""

    def test_early_failure_into_new_directory(self, tmp_path: Path) -> None:
        """An early failure reports only the real error."""
        output = tmp_path / "new"

        exit_code, output_text = _clone(output, "--disable-log-file", fail=True)

        assert exit_code == 3
        assert SUMMARY_FAILURE not in output_text
        assert "Traceback" not in output_text
        assert not output.exists()

    def test_early_failure_into_existing_directory(self, tmp_path: Path) -> None:
        """An early failure creates no log file."""
        exit_code, _output = _clone(tmp_path, "--disable-log-file", fail=True)

        assert exit_code == 3
        assert not (tmp_path / LOG_NAME).exists()

    def test_success_reports_no_log_file(self, tmp_path: Path) -> None:
        """The results panel names no log file."""
        exit_code, output_text = _clone(tmp_path, "--disable-log-file", fail=False)

        assert exit_code == 0
        assert "Log File:" not in output_text
        assert not (tmp_path / LOG_NAME).exists()


class TestCloneWithLogFileEnabled:
    """With file logging active, the summary still reaches the log file."""

    def test_early_failure_writes_summary(self, tmp_path: Path) -> None:
        """An early failure appends the error summary to the log file."""
        exit_code, output_text = _clone(tmp_path, fail=True)

        assert exit_code == 3
        assert SUMMARY_FAILURE not in output_text
        log_text = (tmp_path / LOG_NAME).read_text(encoding="utf-8")
        assert SUMMARY_HEADING in log_text
        assert "discovery failed" in log_text

    def test_success_reports_log_file(self, tmp_path: Path) -> None:
        """The results panel names the log file it wrote."""
        exit_code, output_text = _clone(tmp_path, fail=False)

        assert exit_code == 0
        assert "Log File:" in output_text
        assert (tmp_path / LOG_NAME).is_file()

    def test_uncreatable_log_file_is_not_retried(self, tmp_path: Path) -> None:
        """A log file that cannot be created is not written later either.

        Its parent is a regular file, so no directory can be made there.
        """
        blocker = tmp_path / "not-a-directory"
        blocker.write_text("", encoding="utf-8")
        log_file = blocker / "clone.log"

        exit_code, output_text = _clone(
            tmp_path, "--log-file", str(log_file), fail=True
        )

        assert exit_code == 3
        assert SUMMARY_FAILURE not in output_text
        assert "Log File:" not in output_text


def _uncreatable_dir(tmp_path: Path) -> Path:
    """Return a directory path that cannot be created (parent is a file)."""
    blocker = tmp_path / "not-a-directory"
    blocker.write_text("", encoding="utf-8")
    return blocker / "out"


class TestFileLoggingActive:
    """``file_logging_active`` reports whether a log file is written."""

    def test_true_once_log_file_created(self, tmp_path: Path) -> None:
        """An enabled log file that was created counts as active."""
        init_logging(log_file=tmp_path / "run.log", quiet=True)

        assert file_logging_active()

    def test_false_when_disabled(self, tmp_path: Path) -> None:
        """Disabled file logging is inactive."""
        init_logging(log_file=tmp_path / "run.log", disable_file=True, quiet=True)

        assert not file_logging_active()
        assert not (tmp_path / "run.log").exists()

    def test_false_when_log_file_cannot_be_created(self, tmp_path: Path) -> None:
        """A log file that cannot be created leaves logging inactive."""
        init_logging(log_file=_uncreatable_dir(tmp_path) / "run.log", quiet=True)

        assert not file_logging_active()


class TestOtherCommandsRecordOnlyAnActiveLogFile:
    """``mirror`` and ``reset`` record a log file only once it exists."""

    def test_mirror(self, tmp_path: Path) -> None:
        """Mirror records no log file it could not create."""
        request = SimpleNamespace(
            server=HOST,
            org="example-org",
            include_projects=None,
            exclude_projects=None,
            output_path=_uncreatable_dir(tmp_path),
            recreate=False,
            overwrite=False,
            set_default_branch=True,
            fix_default_branch=True,
            verbose=False,
            quiet=True,
        )
        session = CliSession(console=Console())

        start_mirror_logging(cast("MirrorRequest", request), session)

        assert session.log_file_path is None

    def test_reset(self, tmp_path: Path) -> None:
        """Reset records no log file it could not create."""
        request = SimpleNamespace(
            org="example-org",
            path=_uncreatable_dir(tmp_path),
            compare=False,
            no_confirm=True,
            include_automation_prs=False,
            verbose=False,
            quiet=True,
        )
        session = CliSession(console=Console())

        start_reset_logging(cast("ResetRequest", request), session)

        assert session.log_file_path is None

    def test_mirror_records_created_log_file(self, tmp_path: Path) -> None:
        """Mirror records the log file it created."""
        request = SimpleNamespace(
            server=HOST,
            org="example-org",
            include_projects=None,
            exclude_projects=None,
            output_path=tmp_path,
            recreate=False,
            overwrite=False,
            set_default_branch=True,
            fix_default_branch=True,
            verbose=False,
            quiet=True,
        )
        session = CliSession(console=Console())

        start_mirror_logging(cast("MirrorRequest", request), session)

        assert session.log_file_path == tmp_path / LOG_NAME
        assert (tmp_path / LOG_NAME).is_file()
