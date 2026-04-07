# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2025 Matthew Watkins <mwatkins@linuxfoundation.org>

"""Unit tests for mirror manager module."""

from __future__ import annotations

import os
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

import pytest

from gerrit_clone.github_api import GitHubRepo, transform_gerrit_name_to_github
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
        assert manager.set_default_branch is True

    def test_init_set_default_branch_explicit_false(self) -> None:
        """Test MirrorManager initialization with set_default_branch=False."""
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
            set_default_branch=False,
        )

        assert manager.set_default_branch is False

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

        with patch.object(manager, "_set_default_branch_from_local") as mock_set_branch:
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
        # Default set_default_branch=True, so should be called
        mock_set_branch.assert_called_once_with(local_path, github_repo)

    @patch("gerrit_clone.mirror_manager.subprocess.run")
    def test_push_to_github_success_sets_default_branch_disabled(
        self, mock_run: Mock
    ) -> None:
        """Test that _set_default_branch_from_local is NOT called when set_default_branch=False."""
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
            set_default_branch=False,
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

        with patch.object(manager, "_set_default_branch_from_local") as mock_set_branch:
            success, error = manager._push_to_github(local_path, github_repo)

        assert success is True
        assert error is None
        mock_set_branch.assert_not_called()

    @patch("gerrit_clone.mirror_manager.subprocess.run")
    def test_push_to_github_failure_does_not_set_default_branch(
        self, mock_run: Mock
    ) -> None:
        """Test that _set_default_branch_from_local is NOT called when push fails."""
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

        with patch.object(manager, "_set_default_branch_from_local") as mock_set_branch:
            success, _error = manager._push_to_github(local_path, github_repo)

        assert success is False
        mock_set_branch.assert_not_called()

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
        # The push call is the first invocation; subsequent calls may
        # come from _set_default_branch_from_local (e.g. git for-each-ref
        # via list_local_branches).  Verify the push was issued and that
        # GIT_SSH_COMMAND was passed in its environment.
        push_call = mock_run.call_args_list[0]
        push_cmd = push_call[0][0]  # positional arg: command list
        assert push_cmd[:3] == ["git", "-C", str(local_path)]
        assert "--mirror" in push_cmd
        push_env = push_call[1].get("env")
        assert push_env is not None
        assert "GIT_SSH_COMMAND" in push_env

    def test_build_push_url_with_token_uses_https(self) -> None:
        """Test that _build_push_url returns plain HTTPS URL when token is set."""
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
            github_token="ghp_test_token_abc123",
        )

        github_repo = GitHubRepo(
            name="test-repo",
            full_name="test-org/test-repo",
            ssh_url="git@github.com:test-org/test-repo.git",
            clone_url="https://github.com/test-org/test-repo.git",
            html_url="https://github.com/test-org/test-repo",
            private=False,
        )

        push_url = manager._build_push_url(github_repo)

        # Token must NOT be embedded in the URL (passed via env instead)
        assert push_url == "https://github.com/test-org/test-repo.git"
        assert "ghp_test_token_abc123" not in push_url
        assert "git@github.com" not in push_url

    def test_build_push_url_without_token_uses_ssh(self) -> None:
        """Test that _build_push_url returns SSH URL when no token is set."""
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

        github_repo = GitHubRepo(
            name="test-repo",
            full_name="test-org/test-repo",
            ssh_url="git@github.com:test-org/test-repo.git",
            clone_url="https://github.com/test-org/test-repo.git",
            html_url="https://github.com/test-org/test-repo",
            private=False,
        )

        push_url = manager._build_push_url(github_repo)

        assert push_url == "git@github.com:test-org/test-repo.git"

    @patch("gerrit_clone.mirror_manager.subprocess.run")
    def test_push_to_github_with_token_uses_https_url(self, mock_run: Mock) -> None:
        """Test that push uses plain HTTPS URL with auth via env vars."""
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
            github_token="ghp_test_token_abc123",
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

        with patch.object(manager, "_set_default_branch_from_local"):
            success, error = manager._push_to_github(local_path, github_repo)

        assert success is True
        assert error is None
        # Only the push call should have been made (default branch
        # setting is mocked out above to avoid extra subprocess calls
        # from git_utils helpers like list_local_branches).
        mock_run.assert_called_once()
        call_args = mock_run.call_args
        # The push URL must be the plain HTTPS URL (no token embedded)
        expected_url = "https://github.com/test-org/test-repo.git"
        assert call_args[0][0] == [
            "git",
            "-C",
            str(local_path),
            "push",
            "--mirror",
            expected_url,
        ]
        # Token must NOT appear anywhere in the command
        cmd_str = " ".join(call_args[0][0])
        assert "ghp_test_token_abc123" not in cmd_str
        # Auth should be passed via GIT_CONFIG_* env vars
        call_env = call_args[1].get("env")
        assert call_env is not None
        assert call_env.get("GIT_CONFIG_COUNT") == "1"
        assert call_env.get("GIT_CONFIG_KEY_0") == "http.extraheader"
        assert "AUTHORIZATION: basic " in call_env.get("GIT_CONFIG_VALUE_0", "")
        # Verify our code did not explicitly inject GIT_SSH_COMMAND.
        # The merged env inherits os.environ, so we check that any
        # value present was NOT added by our code.
        if "GIT_SSH_COMMAND" in call_env:
            assert call_env["GIT_SSH_COMMAND"] == os.environ.get("GIT_SSH_COMMAND")

    @patch("gerrit_clone.mirror_manager.subprocess.run")
    def test_push_to_github_with_token_does_not_set_ssh_command(
        self, mock_run: Mock
    ) -> None:
        """Test that SSH command is not set even with ssh_identity_file when token is present."""
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
            github_token="ghp_test_token_abc123",
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

        with patch.object(manager, "_set_default_branch_from_local"):
            success, _error = manager._push_to_github(local_path, github_repo)

        assert success is True
        mock_run.assert_called_once()
        # Even though ssh_identity_file is set, GIT_SSH_COMMAND should NOT
        # be in env because github_token overrides to HTTPS push.
        # The env should contain GIT_CONFIG_* for token auth instead.
        call_env = mock_run.call_args[1].get("env")
        assert call_env is not None
        # Verify GIT_CONFIG env vars are present for token auth
        assert call_env.get("GIT_CONFIG_COUNT") == "1"
        assert call_env.get("GIT_CONFIG_KEY_0") == "http.extraheader"
        # Verify our code did not explicitly set GIT_SSH_COMMAND.
        # The merged env inherits os.environ, so we check that the
        # value (if present) was NOT injected by our code — i.e. it
        # equals whatever os.environ already had (or is absent from
        # os.environ entirely).
        if "GIT_SSH_COMMAND" in call_env:
            assert call_env["GIT_SSH_COMMAND"] == os.environ.get("GIT_SSH_COMMAND")

    @patch("gerrit_clone.mirror_manager.subprocess.run")
    def test_push_to_github_with_token_sanitizes_error_output(
        self, mock_run: Mock
    ) -> None:
        """Test that token is sanitized from error messages on push failure."""
        config = Config(
            host="gerrit.example.org",
            port=29418,
            path=Path("/tmp/test"),
        )
        github_api = Mock()
        token = "ghp_secret_token_value"
        manager = MirrorManager(
            config=config,
            github_api=github_api,
            github_org="test-org",
            github_token=token,
        )

        mock_run.side_effect = subprocess.CalledProcessError(
            1,
            "git",
            stderr=f"fatal: unable to access 'https://x-access-token:{token}@github.com/test-org/test-repo.git/': The requested URL returned error: 403",
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
        # The token must not appear in the error message
        assert token not in error
        assert "***" in error

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
        github_api.list_repos = Mock(return_value=[])
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
        github_api.list_repos = Mock(return_value=[])
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
        github_api.list_repos = Mock(return_value=[])
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
        github_api.list_repos = Mock(return_value=[])
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
        github_api.list_repos = Mock(return_value=[])
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

    @patch("gerrit_clone.mirror_manager.apply_content_filters")
    def test_mirror_projects_calls_content_filters_remove_patterns(
        self,
        mock_apply_filters: Mock,
    ) -> None:
        """Content filters are invoked when remove_file_patterns is set."""
        config = Config(
            host="gerrit.example.org",
            port=29418,
            path=Path("/tmp/test"),
        )
        github_api = Mock()
        github_api.list_all_repos_graphql = Mock(return_value={})
        github_api.list_repos = Mock(return_value=[])
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

        mock_apply_filters.return_value = (True, None)

        manager = MirrorManager(
            config=config,
            github_api=github_api,
            github_org="test-org",
            remove_file_patterns=[".github/dependabot.yml"],
        )

        projects = [Project("test-repo", ProjectState.ACTIVE)]
        clone_path = Path("/tmp/test/test-repo")
        mock_clone_result = CloneResult(
            project=projects[0],
            status=CloneStatus.SUCCESS,
            path=clone_path,
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

        mock_apply_filters.assert_called_once_with(
            clone_path,
            "test-repo",
            remove_patterns=[".github/dependabot.yml"],
            git_filter_projects=None,
        )
        assert len(result) == 1
        assert result[0].status == MirrorStatus.SUCCESS

    @patch("gerrit_clone.mirror_manager.apply_content_filters")
    def test_mirror_projects_calls_content_filters_git_filter(
        self,
        mock_apply_filters: Mock,
    ) -> None:
        """Content filters are invoked when git_filter_projects is set."""
        config = Config(
            host="gerrit.example.org",
            port=29418,
            path=Path("/tmp/test"),
        )
        github_api = Mock()
        github_api.list_all_repos_graphql = Mock(return_value={})
        github_api.list_repos = Mock(return_value=[])
        github_api.batch_delete_repos = AsyncMock(return_value={})

        test_repo = GitHubRepo(
            name="my-project",
            full_name="test-org/my-project",
            ssh_url="git@github.com:test-org/my-project.git",
            clone_url="https://github.com/test-org/my-project.git",
            html_url="https://github.com/test-org/my-project",
            private=False,
        )
        github_api.batch_create_repos = AsyncMock(
            return_value={"my-project": (test_repo, None)}
        )
        github_api.ensure_repo = AsyncMock(return_value=test_repo)

        mock_apply_filters.return_value = (True, None)

        git_filters = {"my-project": ["secret-token-value"]}
        manager = MirrorManager(
            config=config,
            github_api=github_api,
            github_org="test-org",
            git_filter_projects=git_filters,
        )

        projects = [Project("my-project", ProjectState.ACTIVE)]
        clone_path = Path("/tmp/test/my-project")
        mock_clone_result = CloneResult(
            project=projects[0],
            status=CloneStatus.SUCCESS,
            path=clone_path,
            duration_seconds=3.0,
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

        mock_apply_filters.assert_called_once_with(
            clone_path,
            "my-project",
            remove_patterns=None,
            git_filter_projects=git_filters,
        )
        assert len(result) == 1
        assert result[0].status == MirrorStatus.SUCCESS

    @patch("gerrit_clone.mirror_manager.apply_content_filters")
    def test_mirror_projects_filter_failure_raises(
        self,
        mock_apply_filters: Mock,
    ) -> None:
        """A content-filter failure aborts the batch with RuntimeError."""
        config = Config(
            host="gerrit.example.org",
            port=29418,
            path=Path("/tmp/test"),
        )
        github_api = Mock()
        github_api.list_all_repos_graphql = Mock(return_value={})
        github_api.list_repos = Mock(return_value=[])
        github_api.batch_delete_repos = AsyncMock(return_value={})
        github_api.batch_create_repos = AsyncMock(return_value={})

        mock_apply_filters.return_value = (False, "git filter-repo failed")

        manager = MirrorManager(
            config=config,
            github_api=github_api,
            github_org="test-org",
            remove_file_patterns=["*.jar"],
        )

        projects = [Project("bad-repo", ProjectState.ACTIVE)]
        mock_clone_result = CloneResult(
            project=projects[0],
            status=CloneStatus.SUCCESS,
            path=Path("/tmp/test/bad-repo"),
            duration_seconds=2.0,
        )

        with (
            patch.object(
                manager.clone_manager,
                "clone_projects",
                return_value=[mock_clone_result],
            ),
            pytest.raises(RuntimeError, match="Content filtering failed"),
        ):
            manager.mirror_projects(projects)

    @patch("gerrit_clone.mirror_manager.apply_content_filters")
    def test_mirror_projects_no_filters_skips_content_filtering(
        self,
        mock_apply_filters: Mock,
    ) -> None:
        """Content filters are NOT called when no patterns are configured."""
        config = Config(
            host="gerrit.example.org",
            port=29418,
            path=Path("/tmp/test"),
        )
        github_api = Mock()
        github_api.list_all_repos_graphql = Mock(return_value={})
        github_api.list_repos = Mock(return_value=[])
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
            # No remove_file_patterns or git_filter_projects
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
            manager.mirror_projects(projects)

        mock_apply_filters.assert_not_called()

    @patch("gerrit_clone.mirror_manager.get_current_branch", return_value="main")
    def test_set_default_branch_from_local_success(self, mock_get_branch: Mock) -> None:
        """Test _set_default_branch_from_local detects HEAD branch and calls the API."""
        config = Config(
            host="gerrit.example.org",
            port=29418,
            path=Path("/tmp/test"),
        )
        github_api = Mock()
        github_api.set_default_branch.return_value = True
        manager = MirrorManager(
            config=config,
            github_api=github_api,
            github_org="test-org",
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

        manager._set_default_branch_from_local(local_path, github_repo)

        mock_get_branch.assert_called_once_with(local_path)
        github_api.set_default_branch.assert_called_once_with(
            "test-org", "test-repo", "main"
        )

    @patch("gerrit_clone.mirror_manager.get_current_branch", return_value="master")
    def test_set_default_branch_from_local_with_master(
        self, mock_get_branch: Mock
    ) -> None:
        """Test _set_default_branch_from_local works with non-main branch names."""
        config = Config(
            host="gerrit.example.org",
            port=29418,
            path=Path("/tmp/test"),
        )
        github_api = Mock()
        github_api.set_default_branch.return_value = True
        manager = MirrorManager(
            config=config,
            github_api=github_api,
            github_org="test-org",
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

        manager._set_default_branch_from_local(local_path, github_repo)

        github_api.set_default_branch.assert_called_once_with(
            "test-org", "test-repo", "master"
        )

    @patch("gerrit_clone.mirror_manager.get_current_branch", return_value=None)
    def test_set_default_branch_from_local_no_head_branch(
        self, mock_get_branch: Mock, tmp_path: Path
    ) -> None:
        """Test _set_default_branch_from_local does nothing when HEAD branch cannot be determined.

        For example, an empty repository or one with a detached HEAD.
        Uses tmp_path to ensure no stale HEAD file exists on the runner.
        """
        local_path = tmp_path / "repo"
        local_path.mkdir()

        config = Config(
            host="gerrit.example.org",
            port=29418,
            path=tmp_path,
        )
        github_api = Mock()
        manager = MirrorManager(
            config=config,
            github_api=github_api,
            github_org="test-org",
        )

        github_repo = GitHubRepo(
            name="test-repo",
            full_name="test-org/test-repo",
            ssh_url="git@github.com:test-org/test-repo.git",
            clone_url="https://github.com/test-org/test-repo.git",
            html_url="https://github.com/test-org/test-repo",
            private=False,
        )

        manager._set_default_branch_from_local(local_path, github_repo)

        # Should NOT call the API when branch is unknown
        github_api.set_default_branch.assert_not_called()

    @patch(
        "gerrit_clone.mirror_manager.get_current_branch",
        side_effect=FileNotFoundError("gone"),
    )
    def test_set_default_branch_from_local_repo_path_missing(
        self, mock_get_branch: Mock, tmp_path: Path
    ) -> None:
        """Test _set_default_branch_from_local handles FileNotFoundError gracefully.

        Uses tmp_path so the HEAD-file fallback path is guaranteed not to
        exist, making the test deterministic across runners.
        """
        # Use a subdirectory that does NOT exist inside tmp_path
        local_path = tmp_path / "nonexistent"

        config = Config(
            host="gerrit.example.org",
            port=29418,
            path=tmp_path,
        )
        github_api = Mock()
        manager = MirrorManager(
            config=config,
            github_api=github_api,
            github_org="test-org",
        )

        github_repo = GitHubRepo(
            name="test-repo",
            full_name="test-org/test-repo",
            ssh_url="git@github.com:test-org/test-repo.git",
            clone_url="https://github.com/test-org/test-repo.git",
            html_url="https://github.com/test-org/test-repo",
            private=False,
        )

        # Should not raise
        manager._set_default_branch_from_local(local_path, github_repo)

        github_api.set_default_branch.assert_not_called()

    @patch(
        "gerrit_clone.mirror_manager.get_current_branch",
        side_effect=ValueError("not a repo"),
    )
    def test_set_default_branch_from_local_invalid_repo(
        self, mock_get_branch: Mock, tmp_path: Path
    ) -> None:
        """Test _set_default_branch_from_local handles ValueError gracefully.

        Uses tmp_path so the HEAD-file fallback reads from a controlled
        directory with no HEAD file, making the test deterministic.
        """
        local_path = tmp_path / "not-a-repo"
        local_path.mkdir()

        config = Config(
            host="gerrit.example.org",
            port=29418,
            path=tmp_path,
        )
        github_api = Mock()
        manager = MirrorManager(
            config=config,
            github_api=github_api,
            github_org="test-org",
        )

        github_repo = GitHubRepo(
            name="test-repo",
            full_name="test-org/test-repo",
            ssh_url="git@github.com:test-org/test-repo.git",
            clone_url="https://github.com/test-org/test-repo.git",
            html_url="https://github.com/test-org/test-repo",
            private=False,
        )

        # Should not raise
        manager._set_default_branch_from_local(local_path, github_repo)

        github_api.set_default_branch.assert_not_called()

    @patch("gerrit_clone.mirror_manager.get_current_branch", return_value="main")
    def test_set_default_branch_from_local_malformed_full_name(
        self, mock_get_branch: Mock
    ) -> None:
        """Test _set_default_branch_from_local handles malformed full_name without crashing.

        Upstream's implementation uses full_name.split('/')[0] for the owner,
        which returns the whole string when there is no slash. The API call
        still proceeds — it is the API's job to reject an invalid owner.
        """
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

        local_path = Path("/tmp/test/repo")
        github_repo = GitHubRepo(
            name="test-repo",
            full_name="no-slash-here",  # Malformed: missing owner/repo separator
            ssh_url="git@github.com:test-org/test-repo.git",
            clone_url="https://github.com/test-org/test-repo.git",
            html_url="https://github.com/test-org/test-repo",
            private=False,
        )

        # Should not raise; API is called with the mangled owner
        manager._set_default_branch_from_local(local_path, github_repo)

        github_api.set_default_branch.assert_called_once_with(
            "no-slash-here", "test-repo", "main"
        )

    @patch("gerrit_clone.mirror_manager.get_current_branch", return_value="main")
    def test_set_default_branch_skips_when_already_correct(
        self, mock_get_branch: Mock
    ) -> None:
        """Test that set_default_branch_from_local skips the API call when branch matches."""
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

        local_path = Path("/tmp/test/repo")
        github_repo = GitHubRepo(
            name="test-repo",
            full_name="test-org/test-repo",
            ssh_url="git@github.com:test-org/test-repo.git",
            clone_url="https://github.com/test-org/test-repo.git",
            html_url="https://github.com/test-org/test-repo",
            private=False,
            default_branch="main",
        )

        manager._set_default_branch_from_local(local_path, github_repo)

        # Branch already matches — API must NOT be called
        github_api.set_default_branch.assert_not_called()

    @patch("gerrit_clone.mirror_manager.get_current_branch", return_value="main")
    def test_set_default_branch_calls_api_when_different(
        self, mock_get_branch: Mock
    ) -> None:
        """Test that set_default_branch_from_local calls API when branch differs."""
        config = Config(
            host="gerrit.example.org",
            port=29418,
            path=Path("/tmp/test"),
        )
        github_api = Mock()
        github_api.set_default_branch.return_value = True
        manager = MirrorManager(
            config=config,
            github_api=github_api,
            github_org="test-org",
        )

        local_path = Path("/tmp/test/repo")
        github_repo = GitHubRepo(
            name="test-repo",
            full_name="test-org/test-repo",
            ssh_url="git@github.com:test-org/test-repo.git",
            clone_url="https://github.com/test-org/test-repo.git",
            html_url="https://github.com/test-org/test-repo",
            private=False,
            default_branch="develop",
        )

        manager._set_default_branch_from_local(local_path, github_repo)

        # Branch differs — API must be called
        github_api.set_default_branch.assert_called_once_with(
            "test-org", "test-repo", "main"
        )

    @patch("gerrit_clone.mirror_manager.get_current_branch", return_value="main")
    def test_set_default_branch_calls_api_when_none(
        self, mock_get_branch: Mock
    ) -> None:
        """Test that set_default_branch_from_local calls API when default_branch is None."""
        config = Config(
            host="gerrit.example.org",
            port=29418,
            path=Path("/tmp/test"),
        )
        github_api = Mock()
        github_api.set_default_branch.return_value = True
        manager = MirrorManager(
            config=config,
            github_api=github_api,
            github_org="test-org",
        )

        local_path = Path("/tmp/test/repo")
        github_repo = GitHubRepo(
            name="test-repo",
            full_name="test-org/test-repo",
            ssh_url="git@github.com:test-org/test-repo.git",
            clone_url="https://github.com/test-org/test-repo.git",
            html_url="https://github.com/test-org/test-repo",
            private=False,
            # default_branch not set — defaults to None
        )

        manager._set_default_branch_from_local(local_path, github_repo)

        # default_branch is None — API must be called
        github_api.set_default_branch.assert_called_once_with(
            "test-org", "test-repo", "main"
        )


class TestFixDefaultBranches:
    """Tests for _fix_default_branches skipping repos whose push failed."""

    def _make_manager(self) -> tuple[MirrorManager, Mock]:
        """Create a MirrorManager with a mocked GitHubAPI."""
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
            fix_default_branch=True,
        )
        return manager, github_api

    def _make_clone_result(
        self, name: str, tmp_path: Path, *, success: bool = True
    ) -> Mock:
        """Create a mock CloneResult with a real local path."""
        cr = Mock()
        cr.project = Project(
            name=name,
            state=ProjectState.ACTIVE,
        )
        local_path = tmp_path / transform_gerrit_name_to_github(name)
        local_path.mkdir(parents=True, exist_ok=True)
        cr.path = local_path
        cr.success = success
        cr.error_message = None if success else "clone failed"
        return cr

    def _make_mirror_result(
        self, name: str, *, status: str = MirrorStatus.SUCCESS
    ) -> MirrorResult:
        """Create a MirrorResult for a given project."""
        github_name = transform_gerrit_name_to_github(name)
        return MirrorResult(
            project=Project(name=name, state=ProjectState.ACTIVE),
            github_name=github_name,
            github_url=f"https://github.com/test-org/{github_name}",
            status=status,
            local_path=Path(f"/tmp/test/{github_name}"),
            duration_seconds=1.0,
            started_at=datetime.now(UTC),
            completed_at=datetime.now(UTC),
        )

    @patch("gerrit_clone.mirror_manager.is_gerrit_parent_project", return_value=False)
    @patch(
        "gerrit_clone.mirror_manager.list_local_branches",
        return_value=["master", "develop"],
    )
    @patch("gerrit_clone.mirror_manager.get_head_ref", return_value="refs/heads/master")
    def test_skips_repos_whose_push_failed(
        self,
        _mock_head: Mock,
        _mock_branches: Mock,
        _mock_parent: Mock,
        tmp_path: Path,
    ) -> None:
        """Repos whose push failed should NOT have set_default_branch called."""
        manager, github_api = self._make_manager()

        # One repo whose push failed, one whose push succeeded
        clone_results = [
            self._make_clone_result("project/failed-repo", tmp_path),
            self._make_clone_result("project/good-repo", tmp_path),
        ]

        failed_gh_name = transform_gerrit_name_to_github("project/failed-repo")
        good_gh_name = transform_gerrit_name_to_github("project/good-repo")

        # Both repos show no default branch in existing_repos (GraphQL data)
        existing_repos = {
            failed_gh_name: {"default_branch": None},
            good_gh_name: {"default_branch": None},
        }

        good_github_repo = GitHubRepo(
            name=good_gh_name,
            full_name=f"test-org/{good_gh_name}",
            ssh_url=f"git@github.com:test-org/{good_gh_name}.git",
            clone_url=f"https://github.com/test-org/{good_gh_name}.git",
            html_url=f"https://github.com/test-org/{good_gh_name}",
            private=False,
        )
        repos_lookup = {
            good_gh_name: good_github_repo,
        }

        mirror_results = [
            self._make_mirror_result("project/failed-repo", status=MirrorStatus.FAILED),
            self._make_mirror_result("project/good-repo", status=MirrorStatus.SUCCESS),
        ]

        github_api.set_default_branch.return_value = True

        manager._fix_default_branches(
            clone_results, existing_repos, repos_lookup, mirror_results
        )

        # set_default_branch should only be called for the good repo
        github_api.set_default_branch.assert_called_once_with(
            "test-org", good_gh_name, "master"
        )

    def test_skips_all_when_all_pushes_failed(self, tmp_path: Path) -> None:
        """When every repo's push failed, no set_default_branch calls should happen."""
        manager, github_api = self._make_manager()

        clone_results = [
            self._make_clone_result("repo-a", tmp_path),
            self._make_clone_result("repo-b", tmp_path),
        ]

        existing_repos = {
            "repo-a": {"default_branch": None},
            "repo-b": {"default_branch": None},
        }

        mirror_results = [
            self._make_mirror_result("repo-a", status=MirrorStatus.FAILED),
            self._make_mirror_result("repo-b", status=MirrorStatus.FAILED),
        ]

        manager._fix_default_branches(clone_results, existing_repos, {}, mirror_results)

        # No calls at all — every repo was filtered out
        github_api.set_default_branch.assert_not_called()

    @patch("gerrit_clone.mirror_manager.is_gerrit_parent_project", return_value=False)
    @patch("gerrit_clone.mirror_manager.list_local_branches", return_value=["main"])
    @patch("gerrit_clone.mirror_manager.get_head_ref", return_value="refs/heads/main")
    def test_no_mirror_results_falls_back_to_old_behaviour(
        self,
        _mock_head: Mock,
        _mock_branches: Mock,
        _mock_parent: Mock,
        tmp_path: Path,
    ) -> None:
        """When mirror_results is None (backward compat), no filtering occurs."""
        manager, github_api = self._make_manager()

        clone_results = [
            self._make_clone_result("my-repo", tmp_path),
        ]

        gh_name = transform_gerrit_name_to_github("my-repo")
        existing_repos = {
            gh_name: {"default_branch": None},
        }

        github_repo = GitHubRepo(
            name=gh_name,
            full_name=f"test-org/{gh_name}",
            ssh_url=f"git@github.com:test-org/{gh_name}.git",
            clone_url=f"https://github.com/test-org/{gh_name}.git",
            html_url=f"https://github.com/test-org/{gh_name}",
            private=False,
        )
        repos_lookup = {gh_name: github_repo}

        github_api.set_default_branch.return_value = True

        # Pass None for mirror_results (backward-compatible default)
        manager._fix_default_branches(clone_results, existing_repos, repos_lookup, None)

        # Should still attempt the fix since we have no failure info
        github_api.set_default_branch.assert_called_once_with(
            "test-org", gh_name, "main"
        )

    def test_no_repos_needing_fix_returns_early(self) -> None:
        """When all repos already have a default branch, nothing happens."""
        manager, github_api = self._make_manager()

        existing_repos = {
            "repo-a": {"default_branch": "main"},
            "repo-b": {"default_branch": "master"},
        }

        manager._fix_default_branches([], existing_repos, {}, [])

        github_api.set_default_branch.assert_not_called()
