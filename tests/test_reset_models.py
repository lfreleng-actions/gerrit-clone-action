# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2025 Matthew Watkins <mwatkins@linuxfoundation.org>

"""Unit tests for reset models."""

from pathlib import Path

from source_clone.reset_models import (
    GitHubRepoStatus,
    LocalRepoStatus,
    ResetResult,
    SyncComparison,
)


def test_github_repo_status_creation():
    """Test GitHubRepoStatus creation."""
    repo = GitHubRepoStatus(
        name="test-repo",
        full_name="org/test-repo",
        url="https://github.com/org/test-repo",
        open_prs=5,
        open_issues=10,
        last_commit_sha="abc123",
        last_commit_date="2025-01-18",
        default_branch="main",
    )

    assert repo.name == "test-repo"
    assert repo.open_prs == 5
    assert repo.open_issues == 10


def test_local_repo_status_creation():
    """Test LocalRepoStatus creation."""
    repo = LocalRepoStatus(
        name="test-repo",
        path=Path("/tmp/test-repo"),
        last_commit_sha="abc123",
        commit_count=42,
        current_branch="main",
        is_valid_git_repo=True,
    )

    assert repo.name == "test-repo"
    assert repo.commit_count == 42
    assert repo.is_valid_git_repo


def test_sync_comparison_synchronized():
    """Test SyncComparison when repos are synchronized."""
    local = LocalRepoStatus(
        name="test-repo",
        path=Path("/tmp/test-repo"),
        last_commit_sha="abc123",
        commit_count=42,
        current_branch="main",
        is_valid_git_repo=True,
    )

    remote = GitHubRepoStatus(
        name="test-repo",
        full_name="org/test-repo",
        url="https://github.com/org/test-repo",
        open_prs=0,
        open_issues=0,
        last_commit_sha="abc123",
        last_commit_date="2025-01-18",
        default_branch="main",
    )

    comparison = SyncComparison(
        repo_name="test-repo",
        local_status=local,
        remote_status=remote,
        is_synchronized=True,
        difference_description="Synchronized",
    )

    assert comparison.is_synchronized
    assert not comparison.commits_differ


def test_sync_comparison_unsynchronized():
    """Test SyncComparison when repos are not synchronized."""
    local = LocalRepoStatus(
        name="test-repo",
        path=Path("/tmp/test-repo"),
        last_commit_sha="abc123",
        commit_count=42,
        current_branch="main",
        is_valid_git_repo=True,
    )

    remote = GitHubRepoStatus(
        name="test-repo",
        full_name="org/test-repo",
        url="https://github.com/org/test-repo",
        open_prs=0,
        open_issues=0,
        last_commit_sha="def456",
        last_commit_date="2025-01-18",
        default_branch="main",
    )

    comparison = SyncComparison(
        repo_name="test-repo",
        local_status=local,
        remote_status=remote,
        is_synchronized=False,
        difference_description="Different commits",
    )

    assert not comparison.is_synchronized
    assert comparison.commits_differ


def test_sync_comparison_no_local():
    """Test SyncComparison when there's no local repo."""
    remote = GitHubRepoStatus(
        name="test-repo",
        full_name="org/test-repo",
        url="https://github.com/org/test-repo",
        open_prs=0,
        open_issues=0,
        last_commit_sha="abc123",
        last_commit_date="2025-01-18",
        default_branch="main",
    )

    comparison = SyncComparison(
        repo_name="test-repo",
        local_status=None,
        remote_status=remote,
        is_synchronized=True,
        difference_description="No local copy",
    )

    assert comparison.is_synchronized
    assert not comparison.commits_differ


def test_reset_result_success_rate():
    """Test ResetResult success rate calculation."""
    result = ResetResult(
        organization="test-org",
        total_repos=10,
        deleted_repos=8,
        failed_deletions=["repo1", "repo2"],
        unsynchronized_repos=[],
        total_prs=5,
        total_issues=10,
    )

    assert result.success_rate == 0.8
    assert not result.had_unsynchronized


def test_reset_result_perfect_success():
    """Test ResetResult with 100% success."""
    result = ResetResult(
        organization="test-org",
        total_repos=5,
        deleted_repos=5,
        failed_deletions=[],
        unsynchronized_repos=[],
        total_prs=0,
        total_issues=0,
    )

    assert result.success_rate == 1.0


def test_reset_result_empty():
    """Test ResetResult with no repos."""
    result = ResetResult(
        organization="test-org",
        total_repos=0,
        deleted_repos=0,
        failed_deletions=[],
        unsynchronized_repos=[],
        total_prs=0,
        total_issues=0,
    )

    assert result.success_rate == 1.0


def test_reset_result_with_unsynchronized():
    """Test ResetResult with unsynchronized repos."""
    remote = GitHubRepoStatus(
        name="test-repo",
        full_name="org/test-repo",
        url="https://github.com/org/test-repo",
        open_prs=0,
        open_issues=0,
        last_commit_sha="abc123",
        last_commit_date="2025-01-18",
        default_branch="main",
    )

    comparison = SyncComparison(
        repo_name="test-repo",
        local_status=None,
        remote_status=remote,
        is_synchronized=False,
        difference_description="Out of sync",
    )

    result = ResetResult(
        organization="test-org",
        total_repos=1,
        deleted_repos=1,
        failed_deletions=[],
        unsynchronized_repos=[comparison],
        total_prs=0,
        total_issues=0,
    )

    assert result.had_unsynchronized
