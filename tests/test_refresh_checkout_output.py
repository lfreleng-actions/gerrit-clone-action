# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""A refresh pointed at a working copy's own top level.

The run writes its log and manifest into the output path, which is then
inside the checkout.  Git must not report them as changes there: refresh
skips a working copy with uncommitted changes, so it would never update
the one checkout it was pointed at.
"""

from __future__ import annotations

import subprocess
import sys
from typing import TYPE_CHECKING, Any

import pytest
from typer.testing import CliRunner

from source_clone.cli import app

if TYPE_CHECKING:
    from pathlib import Path


def _git(*args: str, cwd: Path | None = None) -> str:
    return subprocess.run(
        [
            "git",
            "-c",
            "user.email=t@example.com",
            "-c",
            "user.name=T",
            "-c",
            "commit.gpgsign=false",
            *args,
        ],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()


def _commit(upstream: Path, content: str) -> None:
    (upstream / "file.txt").write_text(content)
    _git("add", "file.txt", cwd=upstream)
    _git("commit", "-q", "-m", content.strip(), cwd=upstream)


@pytest.fixture
def upstream(tmp_path: Path) -> Path:
    path = tmp_path / "up"
    path.mkdir()
    _git("init", "-q", "-b", "main", str(path))
    _commit(path, "one\n")
    return path


@pytest.fixture
def checkout(tmp_path: Path, upstream: Path) -> Path:
    path = tmp_path / "checkout"
    _git("clone", "-q", upstream.as_uri(), str(path))
    return path


def _refresh(checkout: Path, *options: str) -> Any:
    return CliRunner().invoke(
        app,
        ["refresh", "--output-path", str(checkout), "--all-repos", *options],
    )


class TestRefreshAtACheckout:
    def test_the_checkout_is_refreshed(self, upstream: Path, checkout: Path) -> None:
        _commit(upstream, "two\n")

        result = _refresh(checkout)

        assert result.exit_code == 0, result.output
        assert (checkout / "file.txt").read_text() == "two\n"

    def test_every_later_run_refreshes_it_too(
        self, upstream: Path, checkout: Path
    ) -> None:
        """The manifest is written after the refresh, so only the next
        run would trip over it."""
        for content in ("two\n", "three\n", "four\n"):
            _commit(upstream, content)
            result = _refresh(checkout)
            assert result.exit_code == 0, result.output
            assert (checkout / "file.txt").read_text() == content

        assert _git("status", "--porcelain", cwd=checkout) == ""
        assert (checkout / "refresh.log").is_file()
        assert list(checkout.glob("refresh-manifest-*.json"))

    def test_a_named_manifest_is_hidden_too(
        self, upstream: Path, checkout: Path
    ) -> None:
        _refresh(checkout, "--manifest-filename", "[report] #1.json")
        _commit(upstream, "two\n")

        result = _refresh(checkout, "--manifest-filename", "[report] #1.json")

        assert result.exit_code == 0, result.output
        assert (checkout / "file.txt").read_text() == "two\n"
        assert _git("status", "--porcelain", cwd=checkout) == ""

    def test_files_git_already_tracks_still_count(
        self, upstream: Path, checkout: Path
    ) -> None:
        """Hiding the run's own files must not hide a real local change."""
        (checkout / "file.txt").write_text("local edit\n")
        _commit(upstream, "two\n")

        _refresh(checkout)

        assert (checkout / "file.txt").read_text() == "local edit\n"


class TestTrackedNames:
    """A checkout that tracks a file the run would write keeps it."""

    @pytest.mark.parametrize(
        ("name", "options"),
        [
            ("refresh.log", ()),
            ("notes.json", ("--manifest-filename", "notes.json")),
        ],
    )
    def test_the_run_stops_before_writing_it(
        self, upstream: Path, checkout: Path, name: str, options: tuple[str, ...]
    ) -> None:
        (checkout / name).write_text("committed\n")
        _git("add", name, cwd=checkout)
        _git("commit", "-q", "-m", f"track {name}", cwd=checkout)
        _commit(upstream, "two\n")

        result = _refresh(checkout, *options)

        assert result.exit_code != 0
        assert name in result.output
        assert (checkout / name).read_text() == "committed\n"
        assert _git("status", "--porcelain", cwd=checkout) == ""


class TestUnclassifiableCheckouts:
    def test_a_checkout_git_refuses_is_left_alone(self, tmp_path: Path) -> None:
        """Git cannot read it, so it cannot say what the checkout tracks."""
        broken = tmp_path / "broken"
        broken.mkdir()
        (broken / ".git").write_text("not a gitdir line\n")

        result = _refresh(broken)

        assert result.exit_code != 0
        assert not (broken / "refresh.log").exists()

    def test_a_manifest_name_git_cannot_exclude_is_refused(
        self, checkout: Path
    ) -> None:
        result = _refresh(checkout, "--manifest-filename", "two\nlines.json")

        assert result.exit_code != 0
        assert "line break" in result.output
        assert _git("status", "--porcelain", cwd=checkout) == ""


class TestExcludeFailures:
    def test_an_exclude_it_cannot_write_stops_the_run(self, checkout: Path) -> None:
        """Unhidden, the log would make the worker skip the checkout."""
        exclude = checkout / ".git" / "info" / "exclude"
        exclude.unlink(missing_ok=True)
        exclude.mkdir(parents=True)

        result = _refresh(checkout)

        assert result.exit_code != 0
        assert "would then skip the checkout" in result.output
        assert not (checkout / "refresh.log").exists()


@pytest.mark.skipif(sys.platform == "win32", reason="symbolic links need privileges")
class TestLinkedNames:
    @pytest.mark.parametrize(
        ("name", "options"),
        [
            ("refresh.log", ()),
            ("notes.json", ("--manifest-filename", "notes.json")),
        ],
    )
    def test_the_run_stops_before_writing_through_it(
        self, checkout: Path, name: str, options: tuple[str, ...]
    ) -> None:
        """An untracked link could aim the write at a tracked file."""
        (checkout / name).symlink_to("file.txt")

        result = _refresh(checkout, *options)

        assert result.exit_code != 0
        assert "symbolic" in result.output
        assert (checkout / "file.txt").read_text() == "one\n"
