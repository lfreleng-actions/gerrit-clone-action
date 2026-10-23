# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""``clone --cleanup`` removes the clones but keeps the run's log file.

The default log file lives in the ``--output-path`` value, which is also
the clone destination unless it is empty or a spelling of ``.``.  Cleanup
must not take the log file with it: the results panel has already named
it, and the closing error summary is still to be written there.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING
from unittest.mock import MagicMock, patch

import pytest
from typer.testing import CliRunner

from source_clone.cli import app
from source_clone.error_codes import DiscoveryError

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

    from source_clone.models import Config

HOST = "gerrit.example.org"
LOG_NAME = f"{HOST}.log"
SUMMARY_FAILURE = "Failed to write error summary"
SUMMARY_HEADING = "ERROR AND WARNING SUMMARY"
WARNING_TEXT = "stubbed clone warning"


@pytest.fixture(autouse=True)
def _close_file_handlers() -> Iterator[None]:
    """Release any log file a command left open on the app logger."""
    yield
    app_logger = logging.getLogger("source_clone")
    for handler in app_logger.handlers[:]:
        if isinstance(handler, logging.FileHandler):
            handler.close()
            app_logger.removeHandler(handler)


def _batch(failed: int) -> MagicMock:
    """Return a batch result with ``failed`` failures and no results."""
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


def _succeed(_config: Config) -> MagicMock:
    """Log one warning, then report an empty successful run."""
    logging.getLogger("source_clone.clone").warning(WARNING_TEXT)
    return _batch(failed=0)


def _fail_clone(_config: Config) -> MagicMock:
    """Log one warning, then report a run with a failed clone."""
    logging.getLogger("source_clone.clone").warning(WARNING_TEXT)
    return _batch(failed=1)


def _fail_discovery(_config: Config) -> MagicMock:
    """Fail the way an early discovery error does, after logging it."""
    logging.getLogger("source_clone.discovery").error("discovery failed")
    raise DiscoveryError("Discovery failed")


def _clone(
    *args: str,
    stub: Callable[[Config], MagicMock] = _succeed,
    cleanup: bool = True,
) -> tuple[int, str]:
    """Run ``clone`` with cloning stubbed; return code and output.

    ``--cleanup`` is passed unless ``cleanup`` is False.
    """
    argv = ["clone", "--host", HOST, *(["--cleanup"] if cleanup else []), *args]
    with patch("source_clone.cli.clone_repositories", side_effect=stub):
        result = CliRunner().invoke(app, argv)
    return result.exit_code, result.output


def _populate(output: Path) -> list[Path]:
    """Create clone-like content under ``output``; return what was made."""
    repo = output / "project-a"
    nested = output / "group" / "project-b"
    (repo / ".git").mkdir(parents=True)
    (repo / ".git" / "config").write_text("[core]\n", encoding="utf-8")
    (repo / "README").write_text("a\n", encoding="utf-8")
    nested.mkdir(parents=True)
    (nested / "file.txt").write_text("b\n", encoding="utf-8")
    manifest = output / "clone-manifest.json"
    manifest.write_text("{}\n", encoding="utf-8")
    return [repo, output / "group", manifest]


def _assert_log_kept(log_file: Path, output_text: str) -> None:
    """The log file survives, holds the summary, and nothing failed."""
    assert SUMMARY_FAILURE not in output_text
    assert "Traceback" not in output_text
    assert log_file.is_file()
    log_text = log_file.read_text(encoding="utf-8")
    assert SUMMARY_HEADING in log_text
    assert WARNING_TEXT in log_text


def _panel_log_path(output_text: str) -> Path:
    """Return the path on the results panel's ``Log File:`` line.

    Callers set ``COLUMNS`` wide enough that Rich does not wrap it.
    """
    match = re.search(r"Log File:\s*(\S+)", output_text)
    assert match is not None, output_text
    return Path(match.group(1))


class TestReproductionTable:
    """Each invocation from the issue keeps its log file."""

    def test_explicit_output_path(self, tmp_path: Path) -> None:
        """The default log file inside the output path survives cleanup."""
        output = tmp_path / "out"

        exit_code, output_text = _clone("--output-path", str(output))

        assert exit_code == 0
        assert "Log File:" in output_text
        _assert_log_kept(output / LOG_NAME, output_text)
        assert "kept log file" in output_text

    def test_output_path_dot(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """With ``.`` the log file sits beside the clones and is kept."""
        monkeypatch.chdir(tmp_path)
        (tmp_path / HOST / "project-a").mkdir(parents=True)

        exit_code, output_text = _clone("--output-path", ".")

        assert exit_code == 0
        _assert_log_kept(tmp_path / LOG_NAME, output_text)
        assert not (tmp_path / HOST).exists()
        assert "kept log file" not in output_text

    def test_no_output_path(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """With no output path the log file is in the working directory."""
        monkeypatch.chdir(tmp_path)
        monkeypatch.delenv("OUTPUT_PATH", raising=False)
        (tmp_path / HOST / "project-a").mkdir(parents=True)

        exit_code, output_text = _clone()

        assert exit_code == 0
        _assert_log_kept(tmp_path / LOG_NAME, output_text)
        assert not (tmp_path / HOST).exists()

    def test_log_file_outside_output_path(self, tmp_path: Path) -> None:
        """A log file outside the output path is kept; the path goes."""
        output = tmp_path / "out"
        _populate(output)
        log_file = tmp_path / "run.log"

        exit_code, output_text = _clone(
            "--output-path", str(output), "--log-file", str(log_file)
        )

        assert exit_code == 0
        _assert_log_kept(log_file, output_text)
        assert not output.exists()
        assert "kept log file" not in output_text


class TestCleanupWithinOutputPath:
    """Cleanup removes everything under the output path but the log."""

    def test_removes_clones_but_not_log(self, tmp_path: Path) -> None:
        """Every clone directory and file goes; the log file stays."""
        output = tmp_path / "out"
        made = _populate(output)

        exit_code, output_text = _clone("--output-path", str(output))

        assert exit_code == 0
        _assert_log_kept(output / LOG_NAME, output_text)
        for path in made:
            assert not path.exists(), path
        assert sorted(p.name for p in output.iterdir()) == [LOG_NAME]

    def test_log_file_in_subdirectory(self, tmp_path: Path) -> None:
        """A nested log file and its parent directories survive."""
        output = tmp_path / "out"
        made = _populate(output)
        logs = output / "logs" / "deep"
        logs.mkdir(parents=True)
        (output / "logs" / "other.txt").write_text("x\n", encoding="utf-8")
        (logs / "sibling").mkdir()
        (logs / "sibling" / "f").write_text("x\n", encoding="utf-8")
        log_file = logs / "run.log"

        exit_code, output_text = _clone(
            "--output-path", str(output), "--log-file", str(log_file)
        )

        assert exit_code == 0
        _assert_log_kept(log_file, output_text)
        for path in made:
            assert not path.exists(), path
        assert sorted(p.name for p in output.iterdir()) == ["logs"]
        assert sorted(p.name for p in (output / "logs").iterdir()) == ["deep"]
        assert sorted(p.name for p in logs.iterdir()) == ["run.log"]

    def test_disabled_log_file(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """With no log file, the output path is removed entirely."""
        monkeypatch.chdir(tmp_path)
        output = tmp_path / "out"
        _populate(output)

        exit_code, output_text = _clone(
            "--output-path", str(output), "--disable-log-file"
        )

        assert exit_code == 0
        assert SUMMARY_FAILURE not in output_text
        assert not output.exists()
        assert not (tmp_path / LOG_NAME).exists()
        assert list(tmp_path.rglob("*.log")) == []

    def test_symlink_is_removed_not_followed(self, tmp_path: Path) -> None:
        """Links inside the output path go; their targets stay intact."""
        output = tmp_path / "out"
        output.mkdir()
        outside = tmp_path / "outside"
        outside.mkdir()
        target_file = outside / "keep.txt"
        target_file.write_text("keep\n", encoding="utf-8")
        (output / "dir-link").symlink_to(outside, target_is_directory=True)
        (output / "file-link").symlink_to(target_file)

        exit_code, _output_text = _clone("--output-path", str(output))

        assert exit_code == 0
        assert not (output / "dir-link").is_symlink()
        assert not (output / "file-link").is_symlink()
        assert target_file.read_text(encoding="utf-8") == "keep\n"
        assert sorted(p.name for p in outside.iterdir()) == ["keep.txt"]


class TestFailedRuns:
    """A failed run keeps its log file too."""

    def test_failed_clone(self, tmp_path: Path) -> None:
        """A run with failed clones still cleans up and keeps the log."""
        output = tmp_path / "out"
        made = _populate(output)

        exit_code, output_text = _clone("--output-path", str(output), stub=_fail_clone)

        assert exit_code != 0
        _assert_log_kept(output / LOG_NAME, output_text)
        for path in made:
            assert not path.exists(), path

    def test_discovery_error(self, tmp_path: Path) -> None:
        """An early discovery failure leaves the log file in place."""
        output = tmp_path / "out"
        output.mkdir()

        exit_code, output_text = _clone(
            "--output-path", str(output), stub=_fail_discovery
        )

        assert exit_code == 3
        assert SUMMARY_FAILURE not in output_text
        log_text = (output / LOG_NAME).read_text(encoding="utf-8")
        assert SUMMARY_HEADING in log_text
        assert "discovery failed" in log_text


class TestNonCanonicalLogPath:
    """The session records the log file's canonical path.

    Cleanup, the results panel and the closing summary then all name the
    same file, however ``--output-path`` or ``--log-file`` is spelled.
    """

    @pytest.fixture(autouse=True)
    def _wide_console(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Keep the ``Log File:`` line on one line of the panel."""
        monkeypatch.setenv("COLUMNS", "1000")

    def test_cleanup_output_path_through_removed_directory(
        self, tmp_path: Path
    ) -> None:
        """Cleanup removing ``child/`` leaves the printed path valid."""
        output = tmp_path / "out"
        (output / "child").mkdir(parents=True)

        exit_code, output_text = _clone("--output-path", str(output / "child" / ".."))

        assert exit_code == 0
        _assert_log_kept(output / LOG_NAME, output_text)
        assert not (output / "child").exists()
        assert _panel_log_path(output_text).is_file()

    def test_cleanup_log_file_through_symlink(self, tmp_path: Path) -> None:
        """A ``--log-file`` reached through a link is kept and printed."""
        output = tmp_path / "out"
        actual = output / "actual"
        actual.mkdir(parents=True)
        (output / "logs").symlink_to(actual, target_is_directory=True)

        exit_code, output_text = _clone(
            "--output-path",
            str(output),
            "--log-file",
            str(output / "logs" / "run.log"),
        )

        assert exit_code == 0
        _assert_log_kept(actual / "run.log", output_text)
        assert not (output / "logs").is_symlink()
        assert _panel_log_path(output_text).is_file()

    def test_no_cleanup_shows_canonical_path(self, tmp_path: Path) -> None:
        """Without cleanup the file lands as before; the panel names it."""
        output = tmp_path / "out"
        (output / "child").mkdir(parents=True)
        log_file = output / LOG_NAME

        exit_code, output_text = _clone(
            "--output-path", str(output / "child" / ".."), cleanup=False
        )

        assert exit_code == 0
        _assert_log_kept(log_file, output_text)
        assert (output / "child").is_dir()
        assert _panel_log_path(output_text) == log_file.resolve()

    def test_disabled_log_file_prints_no_path(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """With file logging off there is no log file and no path line."""
        monkeypatch.chdir(tmp_path)
        output = tmp_path / "out"
        (output / "child").mkdir(parents=True)

        exit_code, output_text = _clone(
            "--output-path", str(output / "child" / ".."), "--disable-log-file"
        )

        assert exit_code == 0
        assert SUMMARY_FAILURE not in output_text
        assert "Log File:" not in output_text
        assert list(tmp_path.rglob("*.log")) == []
