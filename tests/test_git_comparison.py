# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2025 Matthew Watkins <mwatkins@linuxfoundation.org>

"""Unit tests for git comparison utilities."""

from pathlib import Path
from unittest.mock import patch

from source_clone.git_comparison import (
    _determine_sync_status,
    compare_local_with_remote,
    scan_local_gerrit_clone,
    transform_github_name_to_gerrit,
)
from source_clone.reset_models import (
    GitHubRepoStatus,
    LocalRepoStatus,
)


def test_determine_sync_status_no_local():
    """Test sync status when no local repo exists."""
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

    is_synced, description = _determine_sync_status(None, remote)

    assert is_synced
    assert description == "No local copy"


def test_determine_sync_status_invalid_local():
    """Test sync status when local repo is invalid."""
    local = LocalRepoStatus(
        name="test-repo",
        path=Path("/tmp/test-repo"),
        last_commit_sha=None,
        commit_count=None,
        current_branch=None,
        is_valid_git_repo=False,
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

    is_synced, description = _determine_sync_status(local, remote)

    assert is_synced
    assert description == "Local repo invalid"


def test_determine_sync_status_exact_match():
    """Test sync status with exact SHA match."""
    local = LocalRepoStatus(
        name="test-repo",
        path=Path("/tmp/test-repo"),
        last_commit_sha="abc1234567890def",
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
        last_commit_sha="abc1234567890def",
        last_commit_date="2025-01-18",
        default_branch="main",
    )

    is_synced, description = _determine_sync_status(local, remote)

    assert is_synced
    assert description == "Synchronized"


def test_determine_sync_status_short_sha_match():
    """Test sync status with short SHA match."""
    local = LocalRepoStatus(
        name="test-repo",
        path=Path("/tmp/test-repo"),
        last_commit_sha="abc12345",
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
        last_commit_sha="abc12345",
        last_commit_date="2025-01-18",
        default_branch="main",
    )

    is_synced, description = _determine_sync_status(local, remote)

    assert is_synced
    # Exact match takes precedence
    assert description == "Synchronized"


def test_determine_sync_status_different_commits():
    """Test sync status with different commits."""
    local = LocalRepoStatus(
        name="test-repo",
        path=Path("/tmp/test-repo"),
        last_commit_sha="abc1234567890def",
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
        last_commit_sha="def9876543210abc",
        last_commit_date="2025-01-18",
        default_branch="main",
    )

    is_synced, description = _determine_sync_status(local, remote)

    assert not is_synced
    assert "Different commits" in description
    assert "abc12345" in description
    assert "def98765" in description


def test_determine_sync_status_missing_local_sha():
    """Test sync status when local SHA is missing."""
    local = LocalRepoStatus(
        name="test-repo",
        path=Path("/tmp/test-repo"),
        last_commit_sha=None,
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

    is_synced, description = _determine_sync_status(local, remote)

    assert not is_synced
    assert description == "Unable to read local commit SHA"


def test_determine_sync_status_missing_remote_sha():
    """Test sync status when remote SHA is missing."""
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
        last_commit_sha=None,
        last_commit_date="2025-01-18",
        default_branch="main",
    )

    is_synced, description = _determine_sync_status(local, remote)

    assert not is_synced
    assert description == "Remote commit SHA unavailable"


def test_compare_local_with_remote():
    """Test comparing local and remote repositories."""
    local_repos = {
        "repo1": LocalRepoStatus(
            name="repo1",
            path=Path("/tmp/repo1"),
            last_commit_sha="abc123",
            commit_count=10,
            current_branch="main",
            is_valid_git_repo=True,
        ),
        "repo2": LocalRepoStatus(
            name="repo2",
            path=Path("/tmp/repo2"),
            last_commit_sha="def456",
            commit_count=20,
            current_branch="main",
            is_valid_git_repo=True,
        ),
    }

    remote_repos = {
        "repo1": GitHubRepoStatus(
            name="repo1",
            full_name="org/repo1",
            url="https://github.com/org/repo1",
            open_prs=0,
            open_issues=0,
            last_commit_sha="abc123",
            last_commit_date="2025-01-18",
            default_branch="main",
        ),
        "repo2": GitHubRepoStatus(
            name="repo2",
            full_name="org/repo2",
            url="https://github.com/org/repo2",
            open_prs=0,
            open_issues=0,
            last_commit_sha="xyz789",  # Different SHA
            last_commit_date="2025-01-18",
            default_branch="main",
        ),
        "repo3": GitHubRepoStatus(
            name="repo3",
            full_name="org/repo3",
            url="https://github.com/org/repo3",
            open_prs=0,
            open_issues=0,
            last_commit_sha="ghi012",
            last_commit_date="2025-01-18",
            default_branch="main",
        ),
    }

    comparisons = compare_local_with_remote(local_repos, remote_repos)

    assert len(comparisons) == 3

    # Find each comparison
    repo1_comp = next(c for c in comparisons if c.repo_name == "repo1")
    repo2_comp = next(c for c in comparisons if c.repo_name == "repo2")
    repo3_comp = next(c for c in comparisons if c.repo_name == "repo3")

    # repo1 should be synchronized
    assert repo1_comp.is_synchronized

    # repo2 should be unsynchronized (different SHA)
    assert not repo2_comp.is_synchronized

    # repo3 should be synchronized (no local copy)
    assert repo3_comp.is_synchronized
    assert repo3_comp.local_status is None


def test_compare_local_with_remote_sorting():
    """Test that unsynchronized repos are sorted first."""
    local_repos = {
        "repo-a": LocalRepoStatus(
            name="repo-a",
            path=Path("/tmp/repo-a"),
            last_commit_sha="abc123",
            commit_count=10,
            current_branch="main",
            is_valid_git_repo=True,
        ),
        "repo-b": LocalRepoStatus(
            name="repo-b",
            path=Path("/tmp/repo-b"),
            last_commit_sha="def456",
            commit_count=20,
            current_branch="main",
            is_valid_git_repo=True,
        ),
    }

    remote_repos = {
        "repo-a": GitHubRepoStatus(
            name="repo-a",
            full_name="org/repo-a",
            url="https://github.com/org/repo-a",
            open_prs=0,
            open_issues=0,
            last_commit_sha="abc123",  # Synchronized
            last_commit_date="2025-01-18",
            default_branch="main",
        ),
        "repo-b": GitHubRepoStatus(
            name="repo-b",
            full_name="org/repo-b",
            url="https://github.com/org/repo-b",
            open_prs=0,
            open_issues=0,
            last_commit_sha="xyz789",  # Not synchronized
            last_commit_date="2025-01-18",
            default_branch="main",
        ),
    }

    comparisons = compare_local_with_remote(local_repos, remote_repos)

    # First item should be the unsynchronized one (repo-b)
    assert comparisons[0].repo_name == "repo-b"
    assert not comparisons[0].is_synchronized

    # Second item should be the synchronized one (repo-a)
    assert comparisons[1].repo_name == "repo-a"
    assert comparisons[1].is_synchronized


def test_transform_github_name_to_gerrit():
    """Test transforming GitHub names back to Gerrit project names."""
    # Simple transformation
    assert transform_github_name_to_gerrit("ccsdk-apps") == "ccsdk/apps"

    # Multiple dashes (only first is replaced)
    assert transform_github_name_to_gerrit("oom-platform-cert") == "oom/platform-cert"

    # No dash
    assert transform_github_name_to_gerrit("simple") == "simple"

    # Edge cases
    assert transform_github_name_to_gerrit("a-b-c-d") == "a/b-c-d"


def test_scan_local_gerrit_clone_skips_git_dirs(tmp_path):
    """Test that scan_local_gerrit_clone efficiently skips .git directory contents."""
    # Create a repository structure with nested .git directory
    repo1 = tmp_path / "repo1"
    repo1.mkdir()
    git1 = repo1 / ".git"
    git1.mkdir()

    # Create some files inside .git to simulate real Git internals
    (git1 / "objects").mkdir()
    (git1 / "objects" / "pack").mkdir()
    (git1 / "refs").mkdir()
    (git1 / "HEAD").write_text("ref: refs/heads/main")

    # Create another repo in a subdirectory
    subdir = tmp_path / "subdir"
    subdir.mkdir()
    repo2 = subdir / "repo2"
    repo2.mkdir()
    git2 = repo2 / ".git"
    git2.mkdir()
    (git2 / "HEAD").write_text("ref: refs/heads/main")

    # Mock _get_local_repo_status to avoid actual Git commands
    mock_status = LocalRepoStatus(
        name="test",
        path=repo1,
        last_commit_sha="abc123",
        commit_count=10,
        current_branch="main",
        is_valid_git_repo=True,
    )

    with patch("source_clone.git_comparison._get_local_repo_status") as mock_get_status:
        mock_get_status.return_value = mock_status

        repos = scan_local_gerrit_clone(tmp_path)

        # Should find both repositories
        assert len(repos) == 2
        assert "repo1" in repos
        assert "repo2" in repos

        # _get_local_repo_status should be called exactly twice (once per repo)
        # This confirms we didn't descend into .git directories
        assert mock_get_status.call_count == 2


def test_scan_local_gerrit_clone_nonexistent_path():
    """Test scan_local_gerrit_clone with non-existent path."""
    repos = scan_local_gerrit_clone(Path("/nonexistent/path"))
    assert repos == {}


def test_scan_local_gerrit_clone_file_not_directory(tmp_path):
    """Test scan_local_gerrit_clone when path is a file, not a directory."""
    test_file = tmp_path / "test.txt"
    test_file.write_text("not a directory")

    repos = scan_local_gerrit_clone(test_file)
    assert repos == {}
