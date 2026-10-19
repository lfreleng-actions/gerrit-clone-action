# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""Tests for failure reporting in the worktree removal fallback.

A content filter whose job is removing secrets must not report success
while leaving the files in place.  Both listing helpers used to return
an empty result for a failed git command, which the caller could not
distinguish from a branch that genuinely had nothing to remove.
"""

from __future__ import annotations

import contextlib
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING
from unittest.mock import MagicMock, patch

import pytest

from source_clone.content_filter import apply_content_filters
from source_clone.content_removal import _list_tree_files
from source_clone.content_worktree import _list_branch_heads, _remove_files_worktree

if TYPE_CHECKING:
    from collections.abc import Generator


@contextlib.contextmanager
def _unrecorded(_repo_path: Path, _policy: object) -> Generator[list[str], None, None]:
    """Stand-in for policy recording, which these filter tests are not about.

    Recording fails closed on a path that is no repository, and these
    run the filters against stand-ins or mocks.  Recording has its own
    tests in ``tests/test_content_policy.py``.
    """
    yield []


@pytest.fixture
def _no_policy_record() -> Generator[None, None, None]:
    with patch("source_clone.content_filter.content_filtering", _unrecorded):
        yield


REPO = Path("/tmp/example.git")
PATTERNS = [".github/workflows"]


def _matcher(file_path: str, pattern: str) -> bool:
    return file_path.startswith(pattern)


def _failed(
    stderr: str = "fatal: not a git repository",
) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(["git"], 128, stdout="", stderr=stderr)


def _succeeded(stdout: str) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(["git"], 0, stdout=stdout, stderr="")


def _dispatch(
    branches: subprocess.CompletedProcess[str],
    tree: subprocess.CompletedProcess[str],
):
    """Route ``for-each-ref`` and ``ls-tree`` to separate results.

    ``content_removal`` and ``content_worktree`` both launch through
    :mod:`source_clone.content_git`, so patching its launch patches
    both; the two commands have to be told apart by their arguments.
    """

    def run(cmd, *args, **kwargs):
        if "ls-tree" in cmd:
            return tree
        return branches

    return run


class TestBranchEnumeration:
    """A failed enumeration is not a repository without branches."""

    @patch("source_clone.content_git.run_tracked")
    def test_command_failure_raises(self, mock_run: MagicMock) -> None:
        mock_run.return_value = _failed()

        with pytest.raises(RuntimeError, match="Failed to list branches"):
            _list_branch_heads(REPO, 30)

    @patch("source_clone.content_git.run_tracked")
    def test_timeout_raises(self, mock_run: MagicMock) -> None:
        mock_run.side_effect = subprocess.TimeoutExpired(cmd=["git"], timeout=30)

        with pytest.raises(RuntimeError, match="timed out"):
            _list_branch_heads(REPO, 30)

    @patch("source_clone.content_git.run_tracked")
    def test_missing_git_raises(self, mock_run: MagicMock) -> None:
        mock_run.side_effect = FileNotFoundError("git")

        with pytest.raises(RuntimeError, match="Failed to list branches"):
            _list_branch_heads(REPO, 30)

    @patch("source_clone.content_git.run_tracked")
    def test_a_repository_without_branches_is_still_empty(
        self, mock_run: MagicMock
    ) -> None:
        mock_run.return_value = _succeeded("")

        assert _list_branch_heads(REPO, 30) == []


class TestTreeListing:
    """A failed listing is not a branch with no matching content."""

    @patch("source_clone.content_git.run_tracked")
    def test_command_failure_raises(self, mock_run: MagicMock) -> None:
        mock_run.return_value = _failed("fatal: not a tree object")

        with pytest.raises(RuntimeError, match="git ls-tree failed"):
            _list_tree_files(REPO, "main")

    @patch("source_clone.content_git.run_tracked")
    def test_timeout_raises(self, mock_run: MagicMock) -> None:
        mock_run.side_effect = subprocess.TimeoutExpired(cmd=["git"], timeout=30)

        with pytest.raises(RuntimeError, match="timed out"):
            _list_tree_files(REPO, "main", timeout=30)

    @patch("source_clone.content_git.run_tracked")
    def test_missing_git_raises(self, mock_run: MagicMock) -> None:
        mock_run.side_effect = OSError("git")

        with pytest.raises(RuntimeError, match="git ls-tree failed"):
            _list_tree_files(REPO, "main")

    @patch("source_clone.content_git.run_tracked")
    def test_an_empty_tree_is_still_empty(self, mock_run: MagicMock) -> None:
        mock_run.return_value = _succeeded("")

        assert _list_tree_files(REPO, "main") == []


@pytest.mark.usefixtures("_no_policy_record")
class TestReportedToTheCaller:
    """apply_content_filters must surface the failure, not swallow it."""

    @patch("source_clone.content_filter._check_git_filter_repo", return_value=False)
    @patch("source_clone.content_git.run_tracked")
    def test_branch_enumeration_failure_fails_the_filter(
        self, mock_run: MagicMock, _mock_check: MagicMock, tmp_path: Path
    ) -> None:
        """The regression: this reported success with the files present."""
        mock_run.return_value = _failed()

        success, error = apply_content_filters(
            tmp_path, "example/repo", remove_patterns=PATTERNS
        )

        assert success is False
        assert error is not None
        assert "Failed to list branches" in error

    @patch("source_clone.content_filter._check_git_filter_repo", return_value=False)
    @patch("source_clone.content_git.run_tracked")
    def test_tree_listing_failure_fails_the_filter(
        self,
        mock_run: MagicMock,
        _mock_check: MagicMock,
        tmp_path: Path,
    ) -> None:
        mock_run.side_effect = _dispatch(
            _succeeded("main\n"), _failed("fatal: not a tree object")
        )

        success, error = apply_content_filters(
            tmp_path, "example/repo", remove_patterns=PATTERNS
        )

        assert success is False
        assert error is not None
        assert "git ls-tree failed" in error


class TestNothingToDoStillSucceeds:
    """An empty repository is not a failure."""

    @patch("source_clone.content_git.run_tracked")
    def test_no_branches_removes_nothing(self, mock_run: MagicMock) -> None:
        mock_run.return_value = _succeeded("")

        assert _remove_files_worktree(REPO, PATTERNS, _matcher) == []

    @patch("source_clone.content_git.run_tracked")
    def test_a_branch_with_no_matching_files_removes_nothing(
        self, mock_run: MagicMock
    ) -> None:
        mock_run.side_effect = _dispatch(
            _succeeded("main\n"), _succeeded("README.md\nsrc/main.py\n")
        )

        assert _remove_files_worktree(REPO, PATTERNS, _matcher) == []
