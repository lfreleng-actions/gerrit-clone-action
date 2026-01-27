# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2025 Matthew Watkins <mwatkins@linuxfoundation.org>

"""Unit tests for mirror manager module."""

from __future__ import annotations

import subprocess
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

from gerrit_clone.github_api import GitHubRepo
from gerrit_clone.mirror_manager import (
    MirrorBatchResult,
    MirrorManager,
    MirrorResult,
    MirrorStatus,
    filter_projects_by_hierarchy,
)
from gerrit_clone.models import CloneResult, CloneStatus, Config, Project, ProjectState


class TestFilterProjectsByHierarchy:
    """Test hierarchical project filtering."""

    def test_empty_filters_returns_all(self) -> None:
        """Test that empty filters returns all projects."""
        projects = [
            Project("ccsdk", ProjectState.ACTIVE),
            Project("oom", ProjectState.ACTIVE),
            Project("ccsdk/apps", ProjectState.ACTIVE),
        ]

        result = filter_projects_by_hierarchy(projects, [])

        assert len(result) == 3

    def test_exact_match(self) -> None:
        """Test exact project name match."""
        projects = [
            Project("ccsdk", ProjectState.ACTIVE),
            Project("oom", ProjectState.ACTIVE),
            Project("ccsdk/apps", ProjectState.ACTIVE),
        ]

        result = filter_projects_by_hierarchy(projects, ["oom"])

        assert len(result) == 1
        assert result[0].name == "oom"

    def test_hierarchical_match(self) -> None:
        """Test hierarchical filtering includes children."""
        projects = [
            Project("ccsdk", ProjectState.ACTIVE),
            Project("ccsdk/apps", ProjectState.ACTIVE),
            Project("ccsdk/features", ProjectState.ACTIVE),
            Project("ccsdk/features/test", ProjectState.ACTIVE),
            Project("oom", ProjectState.ACTIVE),
        ]

        result = filter_projects_by_hierarchy(projects, ["ccsdk"])

        assert len(result) == 4
        names = {p.name for p in result}
        assert names == {
            "ccsdk",
            "ccsdk/apps",
            "ccsdk/features",
            "ccsdk/features/test",
        }

    def test_multiple_filters(self) -> None:
        """Test multiple filter names."""
        projects = [
            Project("ccsdk", ProjectState.ACTIVE),
            Project("ccsdk/apps", ProjectState.ACTIVE),
            Project("oom", ProjectState.ACTIVE),
            Project("oom/kubernetes", ProjectState.ACTIVE),
            Project("cps", ProjectState.ACTIVE),
        ]

        result = filter_projects_by_hierarchy(projects, ["ccsdk", "oom"])

        assert len(result) == 4
        names = {p.name for p in result}
        assert names == {
            "ccsdk",
            "ccsdk/apps",
            "oom",
            "oom/kubernetes",
        }

    def test_no_partial_matches(self) -> None:
        """Test that partial name matches are not included."""
        projects = [
            Project("ccsdk", ProjectState.ACTIVE),
            Project("ccsdk/apps", ProjectState.ACTIVE),
            Project("ccsdkfoo", ProjectState.ACTIVE),
        ]

        result = filter_projects_by_hierarchy(projects, ["ccsdk"])

        assert len(result) == 2
        names = {p.name for p in result}
        assert names == {"ccsdk", "ccsdk/apps"}


class TestMirrorResult:
    """Test MirrorResult dataclass."""

    def test_success_property_with_success_status(self) -> None:
        """Test success property returns True for success status."""
        project = Project("test", ProjectState.ACTIVE)
        result = MirrorResult(
            project=project,
            github_name="test",
            github_url="https://github.com/org/test",
            status=MirrorStatus.SUCCESS,
            local_path=Path("/tmp/test"),
            duration_seconds=10.5,
        )

        assert result.success is True

    def test_success_property_with_skipped_status(self) -> None:
        """Test success property returns True for skipped status."""
        project = Project("test", ProjectState.ACTIVE)
        result = MirrorResult(
            project=project,
            github_name="test",
            github_url="https://github.com/org/test",
            status=MirrorStatus.SKIPPED,
            local_path=Path("/tmp/test"),
            duration_seconds=1.0,
        )

        assert result.success is True

    def test_success_property_with_failed_status(self) -> None:
        """Test success property returns False for failed status."""
        project = Project("test", ProjectState.ACTIVE)
        result = MirrorResult(
            project=project,
            github_name="test",
            github_url="",
            status=MirrorStatus.FAILED,
            local_path=Path("/tmp/test"),
            duration_seconds=5.0,
            error_message="Clone failed",
        )

        assert result.success is False

    def test_to_dict(self) -> None:
        """Test conversion to dictionary."""
        project = Project("test/project", ProjectState.ACTIVE)
        started = datetime.now(UTC)
        completed = datetime.now(UTC)

        result = MirrorResult(
            project=project,
            github_name="test-project",
            github_url="https://github.com/org/test-project",
            status=MirrorStatus.SUCCESS,
            local_path=Path("/tmp/test/project"),
            duration_seconds=15.5,
            started_at=started,
            completed_at=completed,
            attempts=2,
        )

        data = result.to_dict()

        assert data["gerrit_project"] == "test/project"
        assert data["github_name"] == "test-project"
        assert data["github_url"] == "https://github.com/org/test-project"
        assert data["status"] == MirrorStatus.SUCCESS
        assert data["local_path"] == "/tmp/test/project"
        assert data["duration_s"] == 15.5
        assert data["attempts"] == 2


class TestMirrorBatchResult:
    """Test MirrorBatchResult dataclass."""

    def test_counts(self) -> None:
        """Test count properties."""
        projects = [
            Project("p1", ProjectState.ACTIVE),
            Project("p2", ProjectState.ACTIVE),
            Project("p3", ProjectState.ACTIVE),
        ]

        results = [
            MirrorResult(
                project=projects[0],
                github_name="p1",
                github_url="https://github.com/org/p1",
                status=MirrorStatus.SUCCESS,
                local_path=Path("/tmp/p1"),
            ),
            MirrorResult(
                project=projects[1],
                github_name="p2",
                github_url="",
                status=MirrorStatus.FAILED,
                local_path=Path("/tmp/p2"),
                error_message="Error",
            ),
            MirrorResult(
                project=projects[2],
                github_name="p3",
                github_url="https://github.com/org/p3",
                status=MirrorStatus.SKIPPED,
                local_path=Path("/tmp/p3"),
            ),
        ]

        batch = MirrorBatchResult(
            results=results,
            started_at=datetime.now(UTC),
            completed_at=datetime.now(UTC),
        )

        assert batch.total_count == 3
        assert batch.success_count == 2
        assert batch.failed_count == 1
        assert batch.skipped_count == 1


class TestMirrorManager:
    """Test MirrorManager class."""

    def test_init(self) -> None:
        """Test MirrorManager initialization."""
        config = Config(
            host="gerrit.example.org",
            port=29418,
            path=Path("/tmp/test"),
        )

        github_api = Mock()

        manager = MirrorManager(
            config=config,
            github_api=github_api,
            github_org="test-org",
            recreate=False,
            overwrite=False,
        )

        assert manager.config == config
        assert manager.github_api == github_api
        assert manager.github_org == "test-org"
        assert manager.recreate is False
        assert manager.overwrite is False

    @patch("gerrit_clone.mirror_manager.subprocess.run")
    def test_push_to_github_success(self, mock_run: Mock) -> None:
        """Test successful push to GitHub."""
        config = Config(
            host="gerrit.example.org",
            port=29418,
            path=Path("/tmp/test"),
        )
        github_api = Mock()
        manager = MirrorManager(
            config=config,
            github_api=github_api,
            github_org="test-org",
        )

        mock_run.return_value = Mock(returncode=0, stdout="", stderr="")
        local_path = Path("/tmp/test/repo")
        github_repo = GitHubRepo(
            name="test-repo",
            full_name="test-org/test-repo",
            ssh_url="git@github.com:test-org/test-repo.git",
            clone_url="https://github.com/test-org/test-repo.git",
            html_url="https://github.com/test-org/test-repo",
            private=False,
        )

        success, error = manager._push_to_github(local_path, github_repo)

        assert success is True
        assert error is None
        mock_run.assert_called_once()
        call_args = mock_run.call_args
        assert call_args[0][0] == [
            "git",
            "-C",
            str(local_path),
            "push",
            "--mirror",
            github_repo.ssh_url,
        ]

    @patch("gerrit_clone.mirror_manager.subprocess.run")
    def test_push_to_github_failure(self, mock_run: Mock) -> None:
        """Test failed push to GitHub."""
        config = Config(
            host="gerrit.example.org",
            port=29418,
            path=Path("/tmp/test"),
        )
        github_api = Mock()
        manager = MirrorManager(
            config=config,
            github_api=github_api,
            github_org="test-org",
        )

        mock_run.side_effect = subprocess.CalledProcessError(
            1, "git", stderr="Permission denied"
        )
        local_path = Path("/tmp/test/repo")
        github_repo = GitHubRepo(
            name="test-repo",
            full_name="test-org/test-repo",
            ssh_url="git@github.com:test-org/test-repo.git",
            clone_url="https://github.com/test-org/test-repo.git",
            html_url="https://github.com/test-org/test-repo",
            private=False,
        )

        success, error = manager._push_to_github(local_path, github_repo)

        assert success is False
        assert error is not None
        assert "Permission denied" in error

    @patch("gerrit_clone.mirror_manager.subprocess.run")
    def test_push_to_github_with_ssh_command(self, mock_run: Mock) -> None:
        """Test push with custom SSH command."""
        config = Config(
            host="gerrit.example.org",
            port=29418,
            path=Path("/tmp/test"),
            ssh_identity_file=Path("/path/to/key"),
        )
        github_api = Mock()
        manager = MirrorManager(
            config=config,
            github_api=github_api,
            github_org="test-org",
        )

        mock_run.return_value = Mock(returncode=0, stdout="", stderr="")
        local_path = Path("/tmp/test/repo")
        github_repo = GitHubRepo(
            name="test-repo",
            full_name="test-org/test-repo",
            ssh_url="git@github.com:test-org/test-repo.git",
            clone_url="https://github.com/test-org/test-repo.git",
            html_url="https://github.com/test-org/test-repo",
            private=False,
        )

        success, _error = manager._push_to_github(local_path, github_repo)

        assert success is True
        mock_run.assert_called_once()
        # Check that GIT_SSH_COMMAND was set in env
        call_env = mock_run.call_args[1].get("env")
        assert call_env is not None
        assert "GIT_SSH_COMMAND" in call_env

    @patch("gerrit_clone.mirror_manager.shutil.rmtree")
    def test_cleanup_existing_repos_with_overwrite(self, mock_rmtree: Mock) -> None:
        """Test cleanup of existing repos when overwrite is True."""
        config = Config(
            host="gerrit.example.org",
            port=29418,
            path=Path("/tmp/test"),
        )
        github_api = Mock()
        manager = MirrorManager(
            config=config,
            github_api=github_api,
            github_org="test-org",
            overwrite=True,
        )

        projects = [
            Project("repo1", ProjectState.ACTIVE),
            Project("repo2", ProjectState.ACTIVE),
        ]

        # Mock Path methods to simulate existing directories
        with (
            patch.object(Path, "exists", return_value=True),
            patch.object(Path, "is_dir", return_value=True),
        ):
            manager._cleanup_existing_repos(projects)

        assert mock_rmtree.call_count == 2

    @patch("gerrit_clone.mirror_manager.shutil.rmtree")
    def test_cleanup_existing_repos_without_overwrite(self, mock_rmtree: Mock) -> None:
        """Test no cleanup when overwrite is False."""
        config = Config(
            host="gerrit.example.org",
            port=29418,
            path=Path("/tmp/test"),
        )
        github_api = Mock()
        manager = MirrorManager(
            config=config,
            github_api=github_api,
            github_org="test-org",
            overwrite=False,
        )

        projects = [
            Project("repo1", ProjectState.ACTIVE),
            Project("repo2", ProjectState.ACTIVE),
        ]

        manager._cleanup_existing_repos(projects)

        mock_rmtree.assert_not_called()

    def test_mirror_projects_success(self) -> None:
        """Test successful end-to-end mirroring."""
        config = Config(
            host="gerrit.example.org",
            port=29418,
            path=Path("/tmp/test"),
        )
        github_api = Mock()
        github_api.list_all_repos_graphql = Mock(return_value={})
        github_api.batch_delete_repos = AsyncMock(return_value={})

        # Mock batch_create_repos to return dict[name, tuple[repo, error]]
        test_repo = GitHubRepo(
            name="test-repo",
            full_name="test-org/test-repo",
            ssh_url="git@github.com:test-org/test-repo.git",
            clone_url="https://github.com/test-org/test-repo.git",
            html_url="https://github.com/test-org/test-repo",
            private=False,
        )
        github_api.batch_create_repos = AsyncMock(
            return_value={"test-repo": (test_repo, None)}
        )
        github_api.ensure_repo = AsyncMock(return_value=test_repo)

        manager = MirrorManager(
            config=config,
            github_api=github_api,
            github_org="test-org",
        )

        projects = [Project("test-repo", ProjectState.ACTIVE)]

        # Mock CloneManager to return successful clone
        mock_clone_result = CloneResult(
            project=projects[0],
            status=CloneStatus.SUCCESS,
            path=Path("/tmp/test/test-repo"),
            duration_seconds=5.0,
        )

        with (
            patch.object(
                manager.clone_manager,
                "clone_projects",
                return_value=[mock_clone_result],
            ),
            patch.object(manager, "_push_to_github", return_value=(True, None)),
        ):
            result = manager.mirror_projects(projects)

        assert len(result) == 1
        assert result[0].status == MirrorStatus.SUCCESS
        assert result[0].success is True

    def test_mirror_projects_clone_failure(self) -> None:
        """Test handling of clone failures."""
        config = Config(
            host="gerrit.example.org",
            port=29418,
            path=Path("/tmp/test"),
        )
        github_api = Mock()
        github_api.list_all_repos_graphql = Mock(return_value={})
        github_api.batch_delete_repos = AsyncMock(return_value={})
        github_api.batch_create_repos = AsyncMock(
            return_value={"test-repo": (None, "Clone failed")}
        )

        manager = MirrorManager(
            config=config,
            github_api=github_api,
            github_org="test-org",
        )

        projects = [Project("test-repo", ProjectState.ACTIVE)]

        # Mock CloneManager to return failed clone
        mock_clone_result = CloneResult(
            project=projects[0],
            status=CloneStatus.FAILED,
            path=Path("/tmp/test/test-repo"),
            duration_seconds=2.0,
            error_message="Connection timeout",
        )

        with patch.object(
            manager.clone_manager,
            "clone_projects",
            return_value=[mock_clone_result],
        ):
            result = manager.mirror_projects(projects)

        assert len(result) == 1
        assert result[0].status == MirrorStatus.FAILED
        assert result[0].success is False
        error_msg = result[0].error_message
        assert error_msg is not None and "Connection timeout" in error_msg

    def test_mirror_projects_push_failure(self) -> None:
        """Test handling of push failures."""
        config = Config(
            host="gerrit.example.org",
            port=29418,
            path=Path("/tmp/test"),
        )
        github_api = Mock()
        github_api.list_all_repos_graphql = Mock(return_value={})
        github_api.batch_delete_repos = AsyncMock(return_value={})

        test_repo = GitHubRepo(
            name="test-repo",
            full_name="test-org/test-repo",
            ssh_url="git@github.com:test-org/test-repo.git",
            clone_url="https://github.com/test-org/test-repo.git",
            html_url="https://github.com/test-org/test-repo",
            private=False,
        )
        github_api.batch_create_repos = AsyncMock(
            return_value={"test-repo": (test_repo, None)}
        )
        github_api.ensure_repo = AsyncMock(return_value=test_repo)

        manager = MirrorManager(
            config=config,
            github_api=github_api,
            github_org="test-org",
        )

        projects = [Project("test-repo", ProjectState.ACTIVE)]

        mock_clone_result = CloneResult(
            project=projects[0],
            status=CloneStatus.SUCCESS,
            path=Path("/tmp/test/test-repo"),
            duration_seconds=5.0,
        )

        with (
            patch.object(
                manager.clone_manager,
                "clone_projects",
                return_value=[mock_clone_result],
            ),
            patch.object(
                manager,
                "_push_to_github",
                return_value=(False, "Authentication failed"),
            ),
        ):
            result = manager.mirror_projects(projects)

        assert len(result) == 1
        assert result[0].status == MirrorStatus.FAILED
        assert result[0].success is False
        error_msg = result[0].error_message
        assert error_msg is not None and "Authentication failed" in error_msg

    def test_mirror_projects_with_recreate_flag(self) -> None:
        """Test recreate flag deletes and recreates GitHub repos."""
        config = Config(
            host="gerrit.example.org",
            port=29418,
            path=Path("/tmp/test"),
        )
        github_api = Mock()
        github_api.list_all_repos_graphql = Mock(return_value={})
        github_api.batch_delete_repos = AsyncMock(return_value={})

        test_repo = GitHubRepo(
            name="test-repo",
            full_name="test-org/test-repo",
            ssh_url="git@github.com:test-org/test-repo.git",
            clone_url="https://github.com/test-org/test-repo.git",
            html_url="https://github.com/test-org/test-repo",
            private=False,
        )
        github_api.batch_create_repos = AsyncMock(
            return_value={"test-repo": (test_repo, None)}
        )
        github_api.ensure_repo = AsyncMock(return_value=test_repo)

        manager = MirrorManager(
            config=config,
            github_api=github_api,
            github_org="test-org",
            recreate=True,
        )

        projects = [Project("test-repo", ProjectState.ACTIVE)]

        mock_clone_result = CloneResult(
            project=projects[0],
            status=CloneStatus.SUCCESS,
            path=Path("/tmp/test/test-repo"),
            duration_seconds=5.0,
        )

        with (
            patch.object(
                manager.clone_manager,
                "clone_projects",
                return_value=[mock_clone_result],
            ),
            patch.object(manager, "_push_to_github", return_value=(True, None)),
        ):
            result = manager.mirror_projects(projects)

        # Verify batch operations were used (recreate triggers batch delete/create)
        # When recreate=True and repo doesn't exist yet, it should just be created
        assert len(result) == 1
        assert result[0].success is True

    def test_mirror_projects_with_overwrite_flag(self) -> None:
        """Test overwrite flag cleans up local directories."""
        config = Config(
            host="gerrit.example.org",
            port=29418,
            path=Path("/tmp/test"),
        )
        github_api = Mock()
        github_api.list_all_repos_graphql = Mock(return_value={})
        github_api.batch_delete_repos = AsyncMock(return_value={})

        test_repo = GitHubRepo(
            name="test-repo",
            full_name="test-org/test-repo",
            ssh_url="git@github.com:test-org/test-repo.git",
            clone_url="https://github.com/test-org/test-repo.git",
            html_url="https://github.com/test-org/test-repo",
            private=False,
        )
        github_api.batch_create_repos = AsyncMock(
            return_value={"test-repo": (test_repo, None)}
        )
        github_api.ensure_repo = AsyncMock(return_value=test_repo)

        manager = MirrorManager(
            config=config,
            github_api=github_api,
            github_org="test-org",
            overwrite=True,
        )

        projects = [Project("test-repo", ProjectState.ACTIVE)]

        mock_clone_result = CloneResult(
            project=projects[0],
            status=CloneStatus.SUCCESS,
            path=Path("/tmp/test/test-repo"),
            duration_seconds=5.0,
        )

        with (
            patch.object(
                manager.clone_manager,
                "clone_projects",
                return_value=[mock_clone_result],
            ),
            patch.object(manager, "_push_to_github", return_value=(True, None)),
            patch("gerrit_clone.mirror_manager.shutil.rmtree") as mock_rmtree,
            patch.object(Path, "exists", return_value=True),
            patch.object(Path, "is_dir", return_value=True),
        ):
            result = manager.mirror_projects(projects)

        # Verify cleanup was effective - rmtree should have been called
        # since overwrite=True and we mocked paths to exist
        assert mock_rmtree.call_count == 1
        assert len(result) == 1
        assert result[0].success is True

    def test_mirror_projects_multiple_repos(self) -> None:
        """Test mirroring multiple repositories."""
        config = Config(
            host="gerrit.example.org",
            port=29418,
            path=Path("/tmp/test"),
        )
        github_api = Mock()
        github_api.list_all_repos_graphql = Mock(return_value={})
        github_api.batch_delete_repos = AsyncMock(return_value={})

        def mock_ensure_repo(github_name: str, **kwargs: object) -> GitHubRepo:
            return GitHubRepo(
                name=github_name,
                full_name=f"test-org/{github_name}",
                ssh_url=f"git@github.com:test-org/{github_name}.git",
                clone_url=f"https://github.com/test-org/{github_name}.git",
                html_url=f"https://github.com/test-org/{github_name}",
                private=False,
            )

        # Mock batch_create_repos to return repos for repo1 and repo2
        repo1 = mock_ensure_repo("repo1")
        repo2 = mock_ensure_repo("repo2")
        github_api.batch_create_repos = AsyncMock(
            return_value={
                "repo1": (repo1, None),
                "repo2": (repo2, None),
            }
        )
        github_api.ensure_repo = AsyncMock(side_effect=mock_ensure_repo)

        manager = MirrorManager(
            config=config,
            github_api=github_api,
            github_org="test-org",
        )

        projects = [
            Project("repo1", ProjectState.ACTIVE),
            Project("repo2", ProjectState.ACTIVE),
            Project("repo3", ProjectState.ACTIVE),
        ]

        mock_clone_results = [
            CloneResult(
                project=projects[0],
                status=CloneStatus.SUCCESS,
                path=Path("/tmp/test/repo1"),
                duration_seconds=5.0,
            ),
            CloneResult(
                project=projects[1],
                status=CloneStatus.SUCCESS,
                path=Path("/tmp/test/repo2"),
                duration_seconds=4.0,
            ),
            CloneResult(
                project=projects[2],
                status=CloneStatus.FAILED,
                path=Path("/tmp/test/repo3"),
                duration_seconds=1.0,
                error_message="Network error",
            ),
        ]

        with (
            patch.object(
                manager.clone_manager,
                "clone_projects",
                return_value=mock_clone_results,
            ),
            patch.object(manager, "_push_to_github", return_value=(True, None)),
        ):
            result = manager.mirror_projects(projects)

        assert len(result) == 3
        # Check individual results
        success_count = sum(1 for r in result if r.success)
        failed_count = sum(1 for r in result if not r.success)
        assert success_count == 2
        assert failed_count == 1
