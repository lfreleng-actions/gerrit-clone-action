# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""Hiding the tool's own files from a checkout's ``git status``."""

from __future__ import annotations

import subprocess
from typing import TYPE_CHECKING

import pytest

from source_clone.worktree_exclude import (
    hide_from_checkout,
    is_checkout_top,
    literal_pattern,
    tracked_names,
)

if TYPE_CHECKING:
    from pathlib import Path


def _git(*args: str, cwd: Path) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True, check=True
    ).stdout.strip()


@pytest.fixture
def checkout(tmp_path: Path) -> Path:
    path = tmp_path / "checkout"
    subprocess.run(["git", "init", "-q", str(path)], check=True)
    return path


class TestLiteralPattern:
    """A name passed to ``--manifest-filename`` is matched as written."""

    @pytest.mark.parametrize(
        ("name", "pattern"),
        [
            ("refresh.log", "/refresh.log"),
            ("[report] #1.json", "/\\[report] #1.json"),
            ("a*b?.json", "/a\\*b\\?.json"),
            ("back\\slash", "/back\\\\slash"),
            ("trailing  ", "/trailing\\ \\ "),
            ("sub/dir.json", "/sub/dir.json"),
        ],
    )
    def test_special_characters_are_escaped(self, name: str, pattern: str) -> None:
        assert literal_pattern(name) == pattern

    def test_a_name_spanning_lines_has_no_pattern(self) -> None:
        assert literal_pattern("two\nlines.json") is None

    def test_git_matches_the_escaped_name_and_nothing_else(
        self, checkout: Path
    ) -> None:
        pattern = literal_pattern("a*b ")
        assert pattern is not None
        hide_from_checkout(checkout, [pattern])
        (checkout / "a*b ").write_text("")
        (checkout / "axb").write_text("")

        assert _git("status", "--porcelain", cwd=checkout) == "?? axb"


class TestHideFromCheckout:
    def test_patterns_are_added_once(self, checkout: Path) -> None:
        hide_from_checkout(checkout, ["/one", "/two"])
        hide_from_checkout(checkout, ["/two", "/three"])

        exclude = (checkout / ".git" / "info" / "exclude").read_text().splitlines()
        assert [line for line in exclude if not line.startswith("#")] == [
            "/one",
            "/two",
            "/three",
        ]

    def test_a_directory_inside_a_checkout_is_left_alone(self, checkout: Path) -> None:
        """Only the checkout at the output path is the run's business."""
        inner = checkout / "inner"
        inner.mkdir()

        hide_from_checkout(inner, ["/refresh.log"])

        exclude = checkout / ".git" / "info" / "exclude"
        assert "/refresh.log" not in exclude.read_text()

    def test_a_directory_outside_any_checkout_is_left_alone(
        self, tmp_path: Path
    ) -> None:
        plain = tmp_path / "plain"
        plain.mkdir()

        hide_from_checkout(plain, ["/refresh.log"])

        assert list(plain.iterdir()) == []


class TestTrackedNames:
    def test_only_tracked_names_are_reported(self, checkout: Path) -> None:
        for name in ("tracked.log", "untracked.log"):
            (checkout / name).write_text("")
        _git("add", "tracked.log", cwd=checkout)

        assert tracked_names(checkout, ["tracked.log", "untracked.log"]) == [
            "tracked.log"
        ]

    def test_names_are_not_patterns(self, checkout: Path) -> None:
        (checkout / "a.json").write_text("")
        _git("add", "a.json", cwd=checkout)

        assert tracked_names(checkout, ["*.json"]) == []

    def test_a_directory_inside_a_checkout_has_none(self, checkout: Path) -> None:
        inner = checkout / "inner"
        inner.mkdir()
        (inner / "refresh.log").write_text("")
        _git("add", "inner/refresh.log", cwd=checkout)

        assert tracked_names(inner, ["refresh.log"]) == []


@pytest.fixture
def broken(tmp_path: Path) -> Path:
    """A checkout whose ``.git`` git cannot read."""
    path = tmp_path / "broken"
    path.mkdir()
    (path / ".git").write_text("not a gitdir line\n")
    return path


class TestIsCheckoutTop:
    def test_a_checkout_is(self, checkout: Path) -> None:
        assert is_checkout_top(checkout)

    def test_a_plain_directory_is_not(self, tmp_path: Path) -> None:
        assert not is_checkout_top(tmp_path)

    def test_one_git_cannot_read_is_an_error(self, broken: Path) -> None:
        """Not "not a checkout": that would skip the tracked-file guard."""
        with pytest.raises(OSError, match="Could not tell"):
            is_checkout_top(broken)
        with pytest.raises(OSError, match="Could not tell"):
            tracked_names(broken, ["refresh.log"])


class TestHideFailures:
    def test_an_exclude_it_cannot_write_is_an_error(self, checkout: Path) -> None:
        exclude = checkout / ".git" / "info" / "exclude"
        exclude.unlink(missing_ok=True)
        exclude.mkdir(parents=True)

        with pytest.raises(OSError, match="Could not write"):
            hide_from_checkout(checkout, ["/refresh.log"])

    def test_one_git_cannot_read_is_an_error(self, broken: Path) -> None:
        with pytest.raises(OSError, match="Could not tell"):
            hide_from_checkout(broken, ["/refresh.log"])
