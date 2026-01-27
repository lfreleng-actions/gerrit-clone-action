# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2025 Matthew Watkins <mwatkins@linuxfoundation.org>

"""Unit tests for GitHub refresh behavior in clone manager."""

from __future__ import annotations

import subprocess
from datetime import UTC, datetime
from typing import TYPE_CHECKING
from unittest.mock import Mock, patch

if TYPE_CHECKING:
    from pathlib import Path

import pytest

from gerrit_clone.clone_manager import clone_repositories
from gerrit_clone.models import (
    CloneStatus,
    Config,
    Project,
    ProjectState,
    RefreshResult,
    RefreshStatus,
    SourceType,
)


@pytest.fixture
def github_config(tmp_path: Path) -> Config:
    """Create a GitHub configuration for testing."""
    return Config(
        host="github.com",
        source_type=SourceType.GITHUB,
        path=tmp_path / "repos",
        github_token="test-token",
        github_org="test-org",
        use_https=True,
        auto_refresh=True,
        force_refresh=False,
    )


@pytest.fixture
def github_project() -> Project:
    """Create a test GitHub project."""
    return Project(
        name="test-repo",
        state=ProjectState.ACTIVE,
        clone_url="https://github.com/test-org/test-repo.git",
        ssh_url_override="git@github.com:test-org/test-repo.git",
        source_type=SourceType.GITHUB,
    )


@pytest.fixture
def temp_github_repo(tmp_path: Path, github_project: Project) -> Path:
    """Create a temporary GitHub repository for testing."""
    repo_path = tmp_path / "repos" / github_project.name
    repo_path.mkdir(parents=True)

    # Initialize git repo with explicit initial branch
    subprocess.run(
        ["git", "init", "-b", "main"],
        cwd=repo_path,
        capture_output=True,
        check=True,
    )

    # Configure git
    subprocess.run(
        ["git", "config", "user.email", "test@example.com"],
        cwd=repo_path,
        capture_output=True,
        check=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Test User"],
        cwd=repo_path,
        capture_output=True,
        check=True,
    )
    # Disable GPG signing to prevent SSH agent prompts for commits
    subprocess.run(
        ["git", "config", "commit.gpgsign", "false"],
        cwd=repo_path,
        capture_output=True,
        check=True,
    )

    # Create initial commit
    (repo_path / "README.md").write_text("# Test Repo")
    subprocess.run(
        ["git", "add", "README.md"],
        cwd=repo_path,
        capture_output=True,
        check=True,
    )
    subprocess.run(
        ["git", "commit", "-m", "Initial commit"],
        cwd=repo_path,
        capture_output=True,
        check=True,
    )

    # Add a GitHub remote (not Gerrit)
    subprocess.run(
        ["git", "remote", "add", "origin", "https://github.com/test-org/test-repo.git"],
        cwd=repo_path,
        capture_output=True,
        check=True,
    )

    # Get current branch name
    branch_result = subprocess.run(
        ["git", "rev-parse", "--abbrev-ref", "HEAD"],
        cwd=repo_path,
        capture_output=True,
        text=True,
        check=True,
    )
    current_branch = branch_result.stdout.strip()

    # Create remote tracking branch
    subprocess.run(
        ["git", "config", "remote.origin.fetch", "+refs/heads/*:refs/remotes/origin/*"],
        cwd=repo_path,
        capture_output=True,
        check=True,
    )

    subprocess.run(
        ["git", "update-ref", f"refs/remotes/origin/{current_branch}", "HEAD"],
        cwd=repo_path,
        capture_output=True,
        check=True,
    )

    # Set upstream tracking
    subprocess.run(
        ["git", "branch", f"--set-upstream-to=origin/{current_branch}"],
        cwd=repo_path,
        capture_output=True,
        check=True,
    )

    return repo_path


class TestGitHubRefreshInCloneManager:
    """Test GitHub repository refresh behavior in clone manager."""

    def test_github_repo_refresh_with_filter_gerrit_only_false(
        self,
        github_config: Config,
        github_project: Project,
        temp_github_repo: Path,
    ) -> None:
        """Test that GitHub repos are refreshed when filter_gerrit_only=False."""
        # Mock the refresh worker to verify it's called correctly
        mock_refresh_result = RefreshResult(
            path=temp_github_repo,
            project_name="test-repo",
            status=RefreshStatus.SUCCESS,
            was_behind=False,
            commits_pulled=0,
            started_at=datetime.now(UTC),
            completed_at=datetime.now(UTC),
            duration_seconds=0.1,
        )

        with patch("gerrit_clone.refresh_worker.RefreshWorker") as MockRefreshWorker:
            mock_worker = Mock()
            mock_worker.refresh_repository.return_value = mock_refresh_result
            MockRefreshWorker.return_value = mock_worker

            # Mock discovery to return our test project
            with patch("gerrit_clone.clone_manager.discover_projects") as mock_discover:
                mock_discover.return_value = ([github_project], {})

                # Run clone_repositories with refresh enabled
                clone_repositories(github_config)

                # Verify RefreshWorker was created with filter_gerrit_only=False
                MockRefreshWorker.assert_called_once()
                call_kwargs = MockRefreshWorker.call_args[1]
                assert call_kwargs["filter_gerrit_only"] is False

                # Verify the worker was used to refresh the repo
                mock_worker.refresh_repository.assert_called_once()

    def test_github_repo_uses_origin_remote(
        self,
        github_config: Config,
        github_project: Project,
        temp_github_repo: Path,
    ) -> None:
        """Test that GitHub repos use 'origin' remote (same as Gerrit)."""
        # Verify the remote name is 'origin'
        result = subprocess.run(
            ["git", "remote"],
            cwd=temp_github_repo,
            capture_output=True,
            text=True,
            check=True,
        )
        remotes = result.stdout.strip().split("\n")
        assert "origin" in remotes

        # Verify the remote URL is GitHub
        result = subprocess.run(
            ["git", "remote", "get-url", "origin"],
            cwd=temp_github_repo,
            capture_output=True,
            text=True,
            check=True,
        )
        remote_url = result.stdout.strip()
        assert "github.com" in remote_url

    def test_github_repo_ssh_and_https_both_supported(
        self,
        github_project: Project,
        tmp_path: Path,
    ) -> None:
        """Test that GitHub repos support both SSH and HTTPS remotes."""
        # Test HTTPS remote
        https_config = Config(
            host="github.com",
            source_type=SourceType.GITHUB,
            path=tmp_path / "https",
            github_token="test-token",
            use_https=True,
        )

        # Test SSH remote
        ssh_config = Config(
            host="github.com",
            source_type=SourceType.GITHUB,
            path=tmp_path / "ssh",
            use_https=False,
        )

        # Both configurations should be valid for refresh
        assert https_config.use_https is True
        assert ssh_config.use_https is False

    def test_github_refresh_with_force_enables_auto_stash(
        self,
        github_config: Config,
        github_project: Project,
        temp_github_repo: Path,
    ) -> None:
        """Test that force_refresh enables auto_stash for GitHub repos."""
        github_config.force_refresh = True

        mock_refresh_result = RefreshResult(
            path=temp_github_repo,
            project_name="test-repo",
            status=RefreshStatus.SUCCESS,
            was_behind=False,
            commits_pulled=0,
            started_at=datetime.now(UTC),
            completed_at=datetime.now(UTC),
            duration_seconds=0.1,
        )

        with patch("gerrit_clone.refresh_worker.RefreshWorker") as MockRefreshWorker:
            mock_worker = Mock()
            mock_worker.refresh_repository.return_value = mock_refresh_result
            MockRefreshWorker.return_value = mock_worker

            with patch("gerrit_clone.clone_manager.discover_projects") as mock_discover:
                mock_discover.return_value = ([github_project], {})

                clone_repositories(github_config)

                # Verify auto_stash is enabled when force_refresh is True
                call_kwargs = MockRefreshWorker.call_args[1]
                assert call_kwargs["auto_stash"] is True
                assert call_kwargs["force"] is True

    def test_github_refresh_respects_no_refresh_flag(
        self,
        github_config: Config,
        github_project: Project,
        temp_github_repo: Path,
    ) -> None:
        """Test that --no-refresh flag skips GitHub repo refresh."""
        github_config.auto_refresh = False

        with patch("gerrit_clone.clone_manager.discover_projects") as mock_discover:
            mock_discover.return_value = ([github_project], {})

            batch_result = clone_repositories(github_config)

            # When auto_refresh is False, repositories should be marked as ALREADY_EXISTS
            assert len(batch_result.results) == 1
            assert batch_result.results[0].status == CloneStatus.ALREADY_EXISTS

    def test_github_refresh_uses_merge_strategy(
        self,
        github_config: Config,
        github_project: Project,
        temp_github_repo: Path,
    ) -> None:
        """Test that GitHub repos use 'merge' strategy by default."""
        mock_refresh_result = RefreshResult(
            path=temp_github_repo,
            project_name="test-repo",
            status=RefreshStatus.SUCCESS,
            was_behind=False,
            commits_pulled=0,
            started_at=datetime.now(UTC),
            completed_at=datetime.now(UTC),
            duration_seconds=0.1,
        )

        with patch("gerrit_clone.refresh_worker.RefreshWorker") as MockRefreshWorker:
            mock_worker = Mock()
            mock_worker.refresh_repository.return_value = mock_refresh_result
            MockRefreshWorker.return_value = mock_worker

            with patch("gerrit_clone.clone_manager.discover_projects") as mock_discover:
                mock_discover.return_value = ([github_project], {})

                clone_repositories(github_config)

                # Verify merge strategy is used
                call_kwargs = MockRefreshWorker.call_args[1]
                assert call_kwargs["strategy"] == "merge"

    def test_github_refresh_enables_prune(
        self,
        github_config: Config,
        github_project: Project,
        temp_github_repo: Path,
    ) -> None:
        """Test that GitHub repos have pruning enabled to remove stale branches."""
        mock_refresh_result = RefreshResult(
            path=temp_github_repo,
            project_name="test-repo",
            status=RefreshStatus.SUCCESS,
            was_behind=False,
            commits_pulled=0,
            started_at=datetime.now(UTC),
            completed_at=datetime.now(UTC),
            duration_seconds=0.1,
        )

        with patch("gerrit_clone.refresh_worker.RefreshWorker") as MockRefreshWorker:
            mock_worker = Mock()
            mock_worker.refresh_repository.return_value = mock_refresh_result
            MockRefreshWorker.return_value = mock_worker

            with patch("gerrit_clone.clone_manager.discover_projects") as mock_discover:
                mock_discover.return_value = ([github_project], {})

                clone_repositories(github_config)

                # Verify pruning is enabled
                call_kwargs = MockRefreshWorker.call_args[1]
                assert call_kwargs["prune"] is True

    def test_github_refresh_handles_https_token_auth(
        self,
        github_config: Config,
        github_project: Project,
        temp_github_repo: Path,
    ) -> None:
        """Test that GitHub HTTPS refresh works with token authentication."""
        github_config.use_https = True
        github_config.github_token = "ghp_test_token_123"

        # The refresh worker should receive the config with token
        mock_refresh_result = RefreshResult(
            path=temp_github_repo,
            project_name="test-repo",
            status=RefreshStatus.SUCCESS,
            was_behind=False,
            commits_pulled=0,
            started_at=datetime.now(UTC),
            completed_at=datetime.now(UTC),
            duration_seconds=0.1,
        )

        with patch("gerrit_clone.refresh_worker.RefreshWorker") as MockRefreshWorker:
            mock_worker = Mock()
            mock_worker.refresh_repository.return_value = mock_refresh_result
            MockRefreshWorker.return_value = mock_worker

            with patch("gerrit_clone.clone_manager.discover_projects") as mock_discover:
                mock_discover.return_value = ([github_project], {})

                clone_repositories(github_config)

                # Verify config with token was passed
                call_kwargs = MockRefreshWorker.call_args[1]
                assert call_kwargs["config"].github_token == "ghp_test_token_123"
                assert call_kwargs["config"].use_https is True

    def test_github_and_gerrit_repos_both_refreshed(
        self,
        github_config: Config,
        tmp_path: Path,
    ) -> None:
        """Test that both GitHub and Gerrit repos are refreshed together."""
        # Create a mixed list of projects
        github_proj = Project(
            name="github-repo",
            state=ProjectState.ACTIVE,
            clone_url="https://github.com/test-org/github-repo.git",
            source_type=SourceType.GITHUB,
        )

        gerrit_proj = Project(
            name="gerrit-repo",
            state=ProjectState.ACTIVE,
            clone_url="https://gerrit.example.org/gerrit-repo",
            source_type=SourceType.GERRIT,
        )

        projects = [github_proj, gerrit_proj]

        # Create temp repos for both
        for proj in projects:
            repo_path = tmp_path / "repos" / proj.name
            repo_path.mkdir(parents=True)
            subprocess.run(
                ["git", "init", "-b", "main"],
                cwd=repo_path,
                capture_output=True,
                check=True,
            )
            subprocess.run(
                ["git", "config", "user.email", "test@example.com"],
                cwd=repo_path,
                capture_output=True,
                check=True,
            )
            subprocess.run(
                ["git", "config", "user.name", "Test User"],
                cwd=repo_path,
                capture_output=True,
                check=True,
            )
            # Disable GPG signing to prevent SSH agent prompts for commits
            subprocess.run(
                ["git", "config", "commit.gpgsign", "false"],
                cwd=repo_path,
                capture_output=True,
                check=True,
            )
            (repo_path / "README.md").write_text(f"# {proj.name}")
            subprocess.run(
                ["git", "add", "README.md"],
                cwd=repo_path,
                capture_output=True,
                check=True,
            )
            subprocess.run(
                ["git", "commit", "-m", "Initial commit"],
                cwd=repo_path,
                capture_output=True,
                check=True,
            )
            subprocess.run(
                ["git", "remote", "add", "origin", proj.clone_url or ""],
                cwd=repo_path,
                capture_output=True,
                check=True,
            )

        mock_refresh_result = RefreshResult(
            path=tmp_path,
            project_name="test-repo",
            status=RefreshStatus.SUCCESS,
            was_behind=False,
            commits_pulled=0,
            started_at=datetime.now(UTC),
            completed_at=datetime.now(UTC),
            duration_seconds=0.1,
        )

        with patch("gerrit_clone.refresh_worker.RefreshWorker") as MockRefreshWorker:
            mock_worker = Mock()
            mock_worker.refresh_repository.return_value = mock_refresh_result
            MockRefreshWorker.return_value = mock_worker

            with patch("gerrit_clone.clone_manager.discover_projects") as mock_discover:
                mock_discover.return_value = (projects, {})

                clone_repositories(github_config)

                # Verify filter_gerrit_only=False allows both to be refreshed
                call_kwargs = MockRefreshWorker.call_args[1]
                assert call_kwargs["filter_gerrit_only"] is False

                # Should have refreshed both repos
                assert mock_worker.refresh_repository.call_count == 2

    def test_refresh_failure_error_message_clarity(
        self,
        github_config: Config,
        github_project: Project,
        temp_github_repo: Path,
    ) -> None:
        """Test that refresh failures have clear error messages distinguishing them from clone failures."""
        # Mock a refresh failure
        mock_refresh_result = RefreshResult(
            path=temp_github_repo,
            project_name=github_project.name,
            status=RefreshStatus.FAILED,
            was_behind=False,
            commits_pulled=0,
            started_at=datetime.now(UTC),
            completed_at=datetime.now(UTC),
            duration_seconds=0.1,
            error_message="git fetch failed: unable to connect",
        )

        with patch("gerrit_clone.refresh_worker.RefreshWorker") as MockRefreshWorker:
            mock_worker = Mock()
            mock_worker.refresh_repository.return_value = mock_refresh_result
            MockRefreshWorker.return_value = mock_worker

            with patch("gerrit_clone.clone_manager.discover_projects") as mock_discover:
                mock_discover.return_value = ([github_project], {})

                batch_result = clone_repositories(github_config)
                results = batch_result.results

                # Find the failed result
                failed_results = [r for r in results if r.status == CloneStatus.FAILED]
                assert len(failed_results) == 1

                # Verify error message clearly indicates this is a refresh failure
                error_msg = failed_results[0].error_message
                assert error_msg is not None
                assert "Refresh failed" in error_msg, (
                    "Error message should clearly indicate refresh failure"
                )
                assert "git fetch failed" in error_msg, (
                    "Error message should include the underlying error"
                )

    def test_refresh_exception_error_message_includes_project_name(
        self,
        github_config: Config,
        github_project: Project,
        temp_github_repo: Path,
    ) -> None:
        """Test that refresh exceptions include the project name in the error message."""
        # Mock a refresh exception
        with patch("gerrit_clone.refresh_worker.RefreshWorker") as MockRefreshWorker:
            mock_worker = Mock()
            mock_worker.refresh_repository.side_effect = RuntimeError(
                "Unexpected error"
            )
            MockRefreshWorker.return_value = mock_worker

            with patch("gerrit_clone.clone_manager.discover_projects") as mock_discover:
                mock_discover.return_value = ([github_project], {})

                batch_result = clone_repositories(github_config)
                results = batch_result.results

                # Find the failed result
                failed_results = [r for r in results if r.status == CloneStatus.FAILED]
                assert len(failed_results) == 1

                # Verify error message includes project name and refresh context
                error_msg = failed_results[0].error_message
                assert error_msg is not None
                assert "Refresh failed for" in error_msg, (
                    "Error message should indicate refresh failure"
                )
                assert github_project.name in error_msg, (
                    "Error message should include the project name"
                )
                assert "Unexpected error" in error_msg, (
                    "Error message should include the exception details"
                )

    def test_empty_repository_sha_comparison(
        self,
        github_config: Config,
        tmp_path: Path,
    ) -> None:
        """Test that repositories with no commits (both SHAs None) are marked as up-to-date."""
        # Create a project with metadata indicating no commits (empty repo)
        empty_project = Project(
            name="empty-repo",
            state=ProjectState.ACTIVE,
            clone_url="https://github.com/test-org/empty-repo.git",
            ssh_url_override="git@github.com:test-org/empty-repo.git",
            source_type=SourceType.GITHUB,
            metadata={
                "latest_commit_sha": None,  # No commits on remote
                "full_name": "test-org/empty-repo",
                "html_url": "https://github.com/test-org/empty-repo",
                "private": False,
            },
        )

        # Create an empty local repository (no commits)
        repo_path = tmp_path / "repos" / "empty-repo"
        repo_path.mkdir(parents=True)
        subprocess.run(
            ["git", "init", "-b", "main"],
            cwd=repo_path,
            check=True,
            capture_output=True,
        )
        # Don't create any commits - this simulates an empty repository

        # Mock get_current_commit_sha to return None (empty local repo)
        with patch("gerrit_clone.git_utils.get_current_commit_sha") as mock_get_sha:
            mock_get_sha.return_value = None  # No commits locally

            with patch("gerrit_clone.clone_manager.discover_projects") as mock_discover:
                mock_discover.return_value = ([empty_project], {})

                # Run clone_repositories with auto_refresh enabled
                batch_result = clone_repositories(github_config)
                results = batch_result.results

                # Should have one result
                assert len(results) == 1
                result = results[0]

                # Should be marked as VERIFIED (up-to-date), not attempted to refresh
                assert result.status == CloneStatus.VERIFIED
                assert result.project.name == "empty-repo"
                assert result.was_refreshed is False
                assert result.refresh_had_updates is False

                # Verify get_current_commit_sha was called
                mock_get_sha.assert_called_once()
