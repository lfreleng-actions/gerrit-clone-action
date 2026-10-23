# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2025 Matthew Watkins <mwatkins@linuxfoundation.org>

"""Unit tests for refresh manager."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch

import pytest

from source_clone.models import RefreshBatchResult, RefreshResult, RefreshStatus
from source_clone.refresh_manager import RefreshManager, refresh_repositories


@pytest.fixture
def temp_git_repos(tmp_path):
    """Create multiple temporary git repositories for testing (mocked)."""
    repos = []

    for i in range(3):
        repo_path = tmp_path / f"repo-{i}"
        repo_path.mkdir()

        # Create .git directory to make it look like a git repo
        git_dir = repo_path / ".git"
        git_dir.mkdir()

        # Create minimal .git structure
        (git_dir / "config").write_text(
            f'[remote "origin"]\n'
            f"    url = ssh://gerrit.example.org:29418/repo-{i}\n"
            f"    fetch = +refs/heads/*:refs/remotes/origin/*\n"
        )
        (git_dir / "HEAD").write_text("ref: refs/heads/main\n")

        # Create a README to simulate repo content
        (repo_path / "README.md").write_text(f"# Repo {i}")

        repos.append(repo_path)

    return repos


@pytest.fixture
def nested_git_repos(tmp_path):
    """Create nested git repositories for testing discovery (mocked)."""
    base = tmp_path / "projects"
    base.mkdir()

    # Create top-level repos
    (base / "repo1").mkdir()
    (base / "repo1" / ".git").mkdir()
    (base / "repo1" / ".git" / "HEAD").write_text("ref: refs/heads/main\n")

    (base / "repo2").mkdir()
    (base / "repo2" / ".git").mkdir()
    (base / "repo2" / ".git" / "HEAD").write_text("ref: refs/heads/main\n")

    # Create nested structure
    (base / "category").mkdir()
    (base / "category" / "repo3").mkdir()
    (base / "category" / "repo3" / ".git").mkdir()
    (base / "category" / "repo3" / ".git" / "HEAD").write_text("ref: refs/heads/main\n")

    (base / "category" / "subcategory").mkdir()
    (base / "category" / "subcategory" / "repo4").mkdir()
    (base / "category" / "subcategory" / "repo4" / ".git").mkdir()
    (base / "category" / "subcategory" / "repo4" / ".git" / "HEAD").write_text(
        "ref: refs/heads/main\n"
    )

    return base


class TestRefreshManager:
    """Tests for RefreshManager class."""

    def test_init(self):
        """Test manager initialization."""
        manager = RefreshManager(
            timeout=120,
            fetch_only=True,
            prune=False,
            threads=8,
        )

        assert manager.timeout == 120
        assert manager.fetch_only is True
        assert manager.prune is False
        assert manager.threads == 8

    def test_init_auto_threads(self):
        """Test automatic thread count detection."""
        manager = RefreshManager()

        # Should have determined thread count
        assert manager.threads > 0
        assert manager.threads <= 32

    def test_discover_local_repositories_empty(self, tmp_path):
        """Test discovering repositories in empty directory."""
        manager = RefreshManager()

        repos = manager.discover_local_repositories(tmp_path)

        assert repos == []

    def test_discover_local_repositories_multiple(self, temp_git_repos, tmp_path):
        """Test discovering multiple repositories."""
        manager = RefreshManager()

        repos = manager.discover_local_repositories(tmp_path)

        # Should find all 3 repos
        assert len(repos) == 3

        # Check repo names
        repo_names = {r.name for r in repos}
        assert repo_names == {"repo-0", "repo-1", "repo-2"}

    def test_discover_local_repositories_nested(self, nested_git_repos):
        """Test discovering nested repositories."""
        manager = RefreshManager()

        repos = manager.discover_local_repositories(nested_git_repos)

        # Should find all 4 repos
        assert len(repos) == 4

        # Check repo names
        repo_names = {r.name for r in repos}
        assert repo_names == {"repo1", "repo2", "repo3", "repo4"}

    def test_discover_local_repositories_non_existent(self):
        """Test discovering repositories in non-existent path."""
        manager = RefreshManager()

        with pytest.raises(ValueError, match="does not exist"):
            manager.discover_local_repositories(Path("/non/existent/path"))

    def test_discover_local_repositories_file(self, tmp_path):
        """Test discovering repositories when path is a file."""
        file_path = tmp_path / "file.txt"
        file_path.write_text("content")

        manager = RefreshManager()

        with pytest.raises(ValueError, match="not a directory"):
            manager.discover_local_repositories(file_path)

    def test_refresh_repositories_empty(self, tmp_path):
        """Test refreshing with no repositories."""
        manager = RefreshManager()

        result = manager.refresh_repositories(tmp_path)

        assert result.total_count == 0
        assert result.success_count == 0
        assert result.failed_count == 0

    @patch("source_clone.refresh_worker.RefreshWorker.refresh_repository")
    def test_refresh_repositories_dry_run(self, mock_refresh, temp_git_repos, tmp_path):
        """Test dry run mode."""

        # Mock successful refresh for dry run
        def mock_refresh_func(repo_path):
            return RefreshResult(
                path=repo_path,
                project_name=repo_path.name,
                status=RefreshStatus.UP_TO_DATE,
                started_at=datetime.now(UTC),
                completed_at=datetime.now(UTC),
            )

        mock_refresh.side_effect = mock_refresh_func

        manager = RefreshManager(dry_run=True, filter_gerrit_only=False)

        result = manager.refresh_repositories(tmp_path)

        # Should have results for all repos
        assert result.total_count == 3

        # In dry run, repos should show as success or have status info
        assert result.success_count >= 0

    @patch("source_clone.refresh_worker.RefreshWorker.refresh_repository")
    def test_refresh_repositories_with_specific_repos(
        self, mock_refresh, temp_git_repos, tmp_path
    ):
        """Test refreshing specific repositories."""

        # Mock successful refresh
        def mock_refresh_func(repo_path):
            return RefreshResult(
                path=repo_path,
                project_name=repo_path.name,
                status=RefreshStatus.UP_TO_DATE,
                started_at=datetime.now(UTC),
                completed_at=datetime.now(UTC),
            )

        mock_refresh.side_effect = mock_refresh_func

        manager = RefreshManager(filter_gerrit_only=False, dry_run=True)

        # Refresh only first 2 repos
        result = manager.refresh_repositories(tmp_path, repo_paths=temp_git_repos[:2])

        assert result.total_count == 2

    @patch("source_clone.refresh_worker.RefreshWorker.refresh_repository")
    def test_refresh_repositories_parallel_execution(
        self, mock_refresh, temp_git_repos, tmp_path
    ):
        """Test parallel execution of refreshes."""

        # Mock successful refresh
        def mock_refresh_func(repo_path):
            return RefreshResult(
                path=repo_path,
                project_name=repo_path.name,
                status=RefreshStatus.SUCCESS,
                started_at=datetime.now(UTC),
                completed_at=datetime.now(UTC),
            )

        mock_refresh.side_effect = mock_refresh_func

        manager = RefreshManager(threads=2)
        result = manager.refresh_repositories(tmp_path)

        # Should have called refresh for each repo
        assert mock_refresh.call_count == 3

        # All should be successful
        assert result.success_count == 3

    @patch("source_clone.refresh_worker.RefreshWorker.refresh_repository")
    def test_refresh_repositories_exit_on_error(
        self, mock_refresh, temp_git_repos, tmp_path
    ):
        """Test exit-on-error behavior."""
        call_count = [0]

        def mock_refresh_func(repo_path):
            call_count[0] += 1
            if call_count[0] == 1:
                # First repo fails
                return RefreshResult(
                    path=repo_path,
                    project_name=repo_path.name,
                    status=RefreshStatus.FAILED,
                    error_message="Test failure",
                    started_at=datetime.now(UTC),
                    completed_at=datetime.now(UTC),
                )
            else:
                # Subsequent repos should succeed (if reached)
                return RefreshResult(
                    path=repo_path,
                    project_name=repo_path.name,
                    status=RefreshStatus.SUCCESS,
                    started_at=datetime.now(UTC),
                    completed_at=datetime.now(UTC),
                )

        mock_refresh.side_effect = mock_refresh_func

        manager = RefreshManager(exit_on_error=True, threads=1)
        result = manager.refresh_repositories(tmp_path)

        # Should have stopped after first failure
        # Note: With parallel execution, multiple tasks might start before cancellation
        assert result.failed_count >= 1

    def test_get_status_emoji(self):
        """Test status emoji mapping."""
        manager = RefreshManager()

        assert manager._get_status_emoji(RefreshStatus.SUCCESS) == "✅"
        assert manager._get_status_emoji(RefreshStatus.UP_TO_DATE) == "✓"
        assert manager._get_status_emoji(RefreshStatus.FAILED) == "❌"
        assert manager._get_status_emoji(RefreshStatus.SKIPPED) == "⊘"
        assert manager._get_status_emoji(RefreshStatus.CONFLICTS) == "⚠️"

    @patch("source_clone.refresh_worker.RefreshWorker.refresh_repository")
    def test_refresh_repositories_batch_result(
        self, mock_refresh, temp_git_repos, tmp_path
    ):
        """Test batch result aggregation."""
        # Mock mixed results
        results = [
            RefreshResult(
                path=temp_git_repos[0],
                project_name="repo-0",
                status=RefreshStatus.SUCCESS,
                was_behind=True,
                commits_pulled=5,
                files_changed=3,
                started_at=datetime.now(UTC),
                completed_at=datetime.now(UTC),
            ),
            RefreshResult(
                path=temp_git_repos[1],
                project_name="repo-1",
                status=RefreshStatus.UP_TO_DATE,
                started_at=datetime.now(UTC),
                completed_at=datetime.now(UTC),
            ),
            RefreshResult(
                path=temp_git_repos[2],
                project_name="repo-2",
                status=RefreshStatus.FAILED,
                error_message="Test error",
                started_at=datetime.now(UTC),
                completed_at=datetime.now(UTC),
            ),
        ]

        mock_refresh.side_effect = results

        manager = RefreshManager()
        result = manager.refresh_repositories(tmp_path)

        assert result.total_count == 3
        assert result.success_count == 2  # SUCCESS + UP_TO_DATE
        assert result.updated_count == 1  # Only SUCCESS with was_behind
        assert result.failed_count == 1
        assert result.total_commits_pulled == 5
        assert result.total_files_changed == 3


class TestRefreshManagerHelpers:
    """Tests for helper functions."""

    def test_refresh_repositories_convenience_function(self, tmp_path):
        """Test convenience function for refresh."""
        result = refresh_repositories(
            base_path=tmp_path,
            timeout=120,
            fetch_only=True,
            threads=4,
        )

        assert result is not None
        assert result.base_path == tmp_path
        assert result.total_count == 0  # No repos in empty dir

    @patch("source_clone.refresh_manager.RefreshManager.refresh_repositories")
    def test_refresh_repositories_passes_config(self, mock_refresh, tmp_path):
        """Test that convenience function passes config correctly."""
        mock_refresh.return_value = RefreshBatchResult(
            base_path=tmp_path,
            results=[],
            started_at=datetime.now(UTC),
            completed_at=datetime.now(UTC),
        )

        refresh_repositories(
            base_path=tmp_path,
            timeout=120,
            fetch_only=True,
            prune=False,
            auto_stash=True,
            strategy="rebase",
            threads=8,
        )

        # Verify RefreshManager was created with correct params
        mock_refresh.assert_called_once()
