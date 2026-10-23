# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""Each command writes its error summary to the log file at most once.

A run function that writes the summary and then raises ``typer.Exit``
passes through the command's own ``except typer.Exit`` handler, which
writes the summary as well.  The log must still end with one summary
block, and a run that collects nothing must still write none.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import TYPE_CHECKING, Any, NoReturn
from unittest.mock import MagicMock, patch

import pytest
from rich.console import Console
from typer.testing import CliRunner

from source_clone.cli import app
from source_clone.cli_session import CliSession
from source_clone.error_codes import DiscoveryError
from source_clone.file_logging import ErrorCollector
from source_clone.mirror_models import MirrorResult, MirrorStatus
from source_clone.model_enums import ProjectState
from source_clone.models import Project
from source_clone.reset_models import ResetResult

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

    from source_clone.models import Config

HOST = "gerrit.example.org"
ORG = "example-org"
SUMMARY_HEADING = "ERROR AND WARNING SUMMARY"
TOKEN_VARIABLES = ("GITHUB_TOKEN", "GERRIT_CLONE_TOKEN")

_stub_logger = logging.getLogger("source_clone.test_summary_written_once")


@pytest.fixture(autouse=True)
def _close_file_handlers() -> Iterator[None]:
    """Release any log file a command left open on the app logger."""
    yield
    app_logger = logging.getLogger("source_clone")
    for handler in app_logger.handlers[:]:
        if isinstance(handler, logging.FileHandler):
            handler.close()
            app_logger.removeHandler(handler)


def _summaries(log_file: Path) -> int:
    """Count the summary blocks in *log_file*."""
    return log_file.read_text(encoding="utf-8").count(SUMMARY_HEADING)


def _batch(failed: int) -> MagicMock:
    """Return a clone batch result with *failed* failures."""
    now = datetime.now()
    return MagicMock(
        started_at=now,
        completed_at=now,
        total_count=failed,
        success_count=0,
        failed_count=failed,
        skipped_count=0,
        refreshed_count=0,
        results=[],
        success_rate=0.0 if failed else 100.0,
    )


def _clone(output: Path, stub: object, *extra: str) -> int:
    """Run ``clone`` against *HOST* with cloning replaced by *stub*."""
    args = ["clone", "--host", HOST, "--output-path", str(output), "--quiet", *extra]
    with patch("source_clone.cli.clone_repositories", side_effect=stub):
        result = CliRunner().invoke(app, args)
    return result.exit_code


class TestClone:
    """``clone`` writes one summary on every exit path."""

    log_name = f"{HOST}.log"

    def test_failed_result(self, tmp_path: Path) -> None:
        """A failed clone exits non-zero after writing one summary."""

        def fail(_config: Config) -> MagicMock:
            _stub_logger.error("clone failed")
            return _batch(failed=1)

        assert _clone(tmp_path, fail) == 7
        assert _summaries(tmp_path / self.log_name) == 1

    def test_configuration_error(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A GitHub host without a token writes one summary."""
        for name in TOKEN_VARIABLES:
            monkeypatch.delenv(name, raising=False)
        args = [
            "clone",
            "--host",
            f"github.com/{ORG}",
            "--output-path",
            str(tmp_path),
            "--quiet",
        ]

        result = CliRunner().invoke(app, args)

        assert result.exit_code == 2
        assert _summaries(tmp_path / f"github.com.{ORG}.log") == 1

    def test_success_with_warning(self, tmp_path: Path) -> None:
        """A successful clone that collected a warning writes one summary."""

        def succeed(_config: Config) -> MagicMock:
            _stub_logger.warning("clone warned")
            return _batch(failed=0)

        assert _clone(tmp_path, succeed) == 0
        assert _summaries(tmp_path / self.log_name) == 1

    def test_discovery_error(self, tmp_path: Path) -> None:
        """A discovery failure, whose run never writes it, writes one."""

        def fail(_config: Config) -> NoReturn:
            _stub_logger.error("discovery failed")
            raise DiscoveryError("Discovery failed")

        assert _clone(tmp_path, fail) == 3
        assert _summaries(tmp_path / self.log_name) == 1

    def test_interrupt(self, tmp_path: Path) -> None:
        """An interrupt writes one summary."""
        assert _clone(tmp_path, KeyboardInterrupt) == 130
        assert _summaries(tmp_path / self.log_name) == 1

    def test_crash(self, tmp_path: Path) -> None:
        """A crash writes one summary."""
        assert _clone(tmp_path, RuntimeError("boom")) == 1
        assert _summaries(tmp_path / self.log_name) == 1

    def test_nothing_collected(self, tmp_path: Path) -> None:
        """A clean run writes no summary block."""
        assert _clone(tmp_path, lambda _config: _batch(failed=0)) == 0
        assert _summaries(tmp_path / self.log_name) == 0


def _mirror(output: Path, status: MirrorStatus) -> int:
    """Run ``mirror`` with one project ending in *status*, warning first."""
    project = Project(name="wanted", state=ProjectState.ACTIVE)

    def mirror_projects(projects: list[Project]) -> list[MirrorResult]:
        _stub_logger.warning("mirror warned")
        return [
            MirrorResult(
                project=p,
                github_name=p.name,
                github_url=f"https://github.com/{ORG}/{p.name}",
                status=status,
                local_path=output / p.name,
            )
            for p in projects
        ]

    manager = MagicMock(resolved_filters=None)
    manager.mirror_projects.side_effect = mirror_projects
    args = [
        "mirror",
        "--server",
        HOST,
        "--org",
        ORG,
        "--output-path",
        str(output),
        "--quiet",
    ]
    with (
        patch("source_clone.cli_mirror_run.authenticate", return_value=MagicMock()),
        patch("source_clone.cli_mirror_run.resolve_org", return_value=ORG),
        patch("source_clone.cli.discover_projects", return_value=([project], {})),
        patch("source_clone.cli_mirror_run.MirrorManager", return_value=manager),
    ):
        result = CliRunner().invoke(app, args)
    return result.exit_code


class TestMirror:
    """``mirror`` writes one summary whichever way it ends."""

    log_name = f"{HOST}.log"

    def test_success(self, tmp_path: Path) -> None:
        """A successful mirror that collected a warning writes one summary."""
        assert _mirror(tmp_path, MirrorStatus.SUCCESS) == 0
        assert _summaries(tmp_path / self.log_name) == 1

    def test_failure(self, tmp_path: Path) -> None:
        """A failed mirror writes one summary."""
        assert _mirror(tmp_path, MirrorStatus.FAILED) == 7
        assert _summaries(tmp_path / self.log_name) == 1


def _reset(path: Path, deleted: int) -> int:
    """Run ``reset`` with *deleted* repositories removed, warning first."""

    async def execute_reset(**_kwargs: object) -> ResetResult:
        _stub_logger.warning("reset warned")
        return ResetResult(
            organization=ORG,
            total_repos=1,
            deleted_repos=deleted,
            failed_deletions=[],
            unsynchronized_repos=[],
            total_prs=0,
            total_issues=0,
        )

    async def check_token_permissions() -> bool:
        return True

    manager = MagicMock()
    manager.check_token_permissions.side_effect = check_token_permissions
    manager.execute_reset.side_effect = execute_reset
    args = [
        "reset",
        "--org",
        ORG,
        "--path",
        str(path),
        "--github-token",
        "not-a-token",
        "--no-confirm",
        "--quiet",
    ]
    with patch("source_clone.cli_reset_run.ResetManager", return_value=manager):
        result = CliRunner().invoke(app, args)
    return result.exit_code


class TestReset:
    """``reset`` writes one summary whichever way it ends."""

    log_name = f"reset-{ORG}.log"

    def test_with_deletions(self, tmp_path: Path) -> None:
        """A reset that deleted repositories writes one summary."""
        assert _reset(tmp_path, deleted=1) == 0
        assert _summaries(tmp_path / self.log_name) == 1

    def test_without_deletions(self, tmp_path: Path) -> None:
        """A reset that deleted nothing writes one summary."""
        assert _reset(tmp_path, deleted=0) == 0
        assert _summaries(tmp_path / self.log_name) == 1


def _collector(*warnings: str) -> ErrorCollector:
    """Return a collector holding *warnings*."""
    collector = ErrorCollector()
    for message in warnings:
        collector.add_warning(message)
    return collector


class TestWriteSummary:
    """``CliSession.write_summary`` writes the summary once per session."""

    def test_second_call_writes_nothing(self, tmp_path: Path) -> None:
        """Calling twice leaves one summary block."""
        log_file = tmp_path / "run.log"
        log_file.touch()
        session = CliSession(
            console=Console(),
            error_collector=_collector("warned"),
            log_file_path=log_file,
        )

        session.write_summary()
        session.write_summary()

        assert _summaries(log_file) == 1

    def test_no_collector_does_not_block_later_write(self, tmp_path: Path) -> None:
        """A call before the collector exists does not count as a write."""
        log_file = tmp_path / "run.log"
        log_file.touch()
        session = CliSession(console=Console(), log_file_path=log_file)

        session.write_summary()
        session.error_collector = _collector("warned")
        session.write_summary()

        assert _summaries(log_file) == 1

    def test_no_log_path_does_not_block_later_write(self, tmp_path: Path) -> None:
        """A call before the log file is known does not count as a write."""
        log_file = tmp_path / "run.log"
        log_file.touch()
        session = CliSession(console=Console(), error_collector=_collector("warned"))

        session.write_summary()
        session.log_file_path = log_file
        session.write_summary()

        assert _summaries(log_file) == 1

    def test_empty_collector_does_not_block_later_write(self, tmp_path: Path) -> None:
        """A call with nothing to report does not count as a write."""
        log_file = tmp_path / "run.log"
        log_file.touch()
        collector = _collector()
        session = CliSession(
            console=Console(), error_collector=collector, log_file_path=log_file
        )

        session.write_summary()
        assert _summaries(log_file) == 0
        collector.add_warning("warned")
        session.write_summary()

        assert _summaries(log_file) == 1


class TestInterruptedWrite:
    """An interrupt during the summary write leaves one summary heading."""

    def test_clone_interrupted_during_write(self, tmp_path: Path) -> None:
        """``clone`` exits 130 with one heading when the write is interrupted."""
        get_summary = ErrorCollector.get_summary
        calls = 0

        def interrupt_once(collector: ErrorCollector) -> dict[str, Any]:
            nonlocal calls
            calls += 1
            if calls == 1:
                raise KeyboardInterrupt
            summary: dict[str, Any] = get_summary(collector)
            return summary

        def fail(_config: Config) -> MagicMock:
            _stub_logger.error("clone failed")
            return _batch(failed=1)

        with patch.object(ErrorCollector, "get_summary", interrupt_once):
            exit_code = _clone(tmp_path, fail)

        assert exit_code == 130
        assert _summaries(tmp_path / f"{HOST}.log") == 1

    def test_interrupt_propagates_and_blocks_rewrite(self, tmp_path: Path) -> None:
        """The interrupt propagates and a later call does not write again."""
        collector = _collector("warned")
        session = CliSession(
            console=Console(), error_collector=collector, log_file_path=tmp_path
        )

        with patch.object(
            collector, "write_summary_to_file", side_effect=[KeyboardInterrupt, None]
        ) as write:
            with pytest.raises(KeyboardInterrupt):
                session.write_summary()
            session.write_summary()

        write.assert_called_once_with(tmp_path)
