# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2025 Matthew Watkins <mwatkins@linuxfoundation.org>

"""Unit tests for GitHub clone worker."""

from __future__ import annotations

import subprocess
from pathlib import Path
from subprocess import TimeoutExpired
from unittest.mock import MagicMock, patch

from gerrit_clone.github_worker import (
    _is_gh_cli_available,
    clone_github_repository,
)
from gerrit_clone.models import CloneStatus, Config, Project, ProjectState, SourceType


def _mock_successful_git_clone(*args, **kwargs):
    """Mock subprocess.run for git clone that creates the target directory.

    This is needed because AtomicClonePath expects the temp directory to exist
    after git clone runs, so we need to actually create it in our mock.
    """
    # Extract the target path from git clone command
    cmd = args[0] if args else kwargs.get("cmd", [])
    if isinstance(cmd, list) and "clone" in cmd:
        # Last argument is the target path
        target_path = cmd[-1]
        Path(target_path).mkdir(parents=True, exist_ok=True)

    return MagicMock(returncode=0, stderr="", stdout="")


class TestIsGhCliAvailable:
    """Tests for _is_gh_cli_available function."""

    @patch("shutil.which")
    def test_returns_true_when_gh_available(self, mock_which: MagicMock) -> None:
        """Test returns True when gh CLI is available."""
        mock_which.return_value = "/usr/local/bin/gh"
        assert _is_gh_cli_available() is True

    @patch("shutil.which")
    def test_returns_false_when_gh_not_available(self, mock_which: MagicMock) -> None:
        """Test returns False when gh CLI is not available."""
        mock_which.return_value = None
        assert _is_gh_cli_available() is False


class TestCloneGitHubRepository:
    """Tests for clone_github_repository function."""

    def test_returns_already_exists_for_existing_repo(self, tmp_path: Path) -> None:
        """Test returns ALREADY_EXISTS status for existing repository."""
        project = Project(
            name="test-repo",
            state=ProjectState.ACTIVE,
            source_type=SourceType.GITHUB,
            clone_url="https://github.com/org/test-repo.git",
        )

        config = Config(
            host="github.com/org",
            source_type=SourceType.GITHUB,
            path=tmp_path,
        )

        # Create existing repo
        repo_path = tmp_path / "test-repo"
        repo_path.mkdir()
        (repo_path / ".git").mkdir()

        result = clone_github_repository(project, config)

        assert result.status == CloneStatus.ALREADY_EXISTS
        assert result.project.name == "test-repo"
        assert result.path == repo_path

    def test_fails_for_non_git_directory(self, tmp_path: Path) -> None:
        """Test fails when directory exists but is not a git repo."""
        project = Project(
            name="test-repo",
            state=ProjectState.ACTIVE,
            source_type=SourceType.GITHUB,
            clone_url="https://github.com/org/test-repo.git",
        )

        config = Config(
            host="github.com/org",
            source_type=SourceType.GITHUB,
            path=tmp_path,
        )

        # Create non-git directory
        repo_path = tmp_path / "test-repo"
        repo_path.mkdir()

        result = clone_github_repository(project, config)

        assert result.status == CloneStatus.FAILED
        assert result.error_message is not None
        assert "not a git repository" in result.error_message

    @patch("gerrit_clone.github_worker.subprocess.run")
    @patch("gerrit_clone.github_worker._is_gh_cli_available")
    def test_uses_gh_cli_when_available_and_enabled(
        self,
        mock_gh_available: MagicMock,
        mock_run: MagicMock,
        tmp_path: Path,
    ) -> None:
        """Test uses gh CLI when available and enabled."""
        mock_gh_available.return_value = True
        mock_run.return_value = MagicMock(returncode=0, stderr="", stdout="")

        project = Project(
            name="test-repo",
            state=ProjectState.ACTIVE,
            source_type=SourceType.GITHUB,
            clone_url="https://github.com/org/test-repo.git",
        )

        config = Config(
            host="github.com/org",
            source_type=SourceType.GITHUB,
            path=tmp_path,
            use_gh_cli=True,
        )

        result = clone_github_repository(project, config)

        assert result.status == CloneStatus.SUCCESS
        # Verify gh command was used
        mock_run.assert_called_once()
        cmd = mock_run.call_args[0][0]
        assert isinstance(cmd, list)
        assert cmd[0] == "gh"
        assert cmd[1] == "repo"
        assert cmd[2] == "clone"

    @patch("gerrit_clone.github_worker._is_gh_cli_available")
    @patch("gerrit_clone.github_worker.subprocess.run")
    def test_falls_back_to_git_when_gh_not_available(
        self,
        mock_run: MagicMock,
        mock_gh_available: MagicMock,
        tmp_path: Path,
    ) -> None:
        """Test falls back to git clone when gh CLI is not available."""
        mock_gh_available.return_value = False
        mock_run.side_effect = _mock_successful_git_clone

        project = Project(
            name="test-repo",
            state=ProjectState.ACTIVE,
            source_type=SourceType.GITHUB,
            clone_url="https://github.com/org/test-repo.git",
        )

        config = Config(
            host="github.com/org",
            source_type=SourceType.GITHUB,
            path=tmp_path,
            use_gh_cli=True,
        )

        result = clone_github_repository(project, config)

        assert result.status == CloneStatus.SUCCESS
        # Verify git command was used
        mock_run.assert_called_once()
        cmd = mock_run.call_args[0][0]
        assert cmd[0] == "git"
        assert cmd[1] == "clone"

    @patch("gerrit_clone.github_worker.subprocess.run")
    def test_uses_git_by_default(
        self,
        mock_run: MagicMock,
        tmp_path: Path,
    ) -> None:
        """Test uses git clone by default."""
        mock_run.side_effect = _mock_successful_git_clone

        project = Project(
            name="test-repo",
            state=ProjectState.ACTIVE,
            source_type=SourceType.GITHUB,
            clone_url="https://github.com/org/test-repo.git",
        )

        config = Config(
            host="github.com/org",
            source_type=SourceType.GITHUB,
            path=tmp_path,
            use_gh_cli=False,
        )

        result = clone_github_repository(project, config)

        assert result.status == CloneStatus.SUCCESS
        mock_run.assert_called_once()
        cmd = mock_run.call_args[0][0]
        assert cmd[0] == "git"
        assert cmd[1] == "clone"

    @patch("gerrit_clone.github_worker.subprocess.run")
    def test_includes_depth_for_shallow_clone(
        self,
        mock_run: MagicMock,
        tmp_path: Path,
    ) -> None:
        """Test includes --depth flag for shallow clones."""
        mock_run.side_effect = _mock_successful_git_clone

        project = Project(
            name="test-repo",
            state=ProjectState.ACTIVE,
            source_type=SourceType.GITHUB,
            clone_url="https://github.com/org/test-repo.git",
        )

        config = Config(
            host="github.com/org",
            source_type=SourceType.GITHUB,
            path=tmp_path,
            depth=1,
            mirror=False,
        )

        result = clone_github_repository(project, config)

        assert result.status == CloneStatus.SUCCESS
        cmd = mock_run.call_args[0][0]
        assert isinstance(cmd, list)
        assert "--depth" in cmd
        assert "1" in cmd

    @patch("gerrit_clone.github_worker.subprocess.run")
    def test_includes_branch_when_specified(
        self,
        mock_run: MagicMock,
        tmp_path: Path,
    ) -> None:
        """Test includes --branch flag when branch is specified."""
        mock_run.side_effect = _mock_successful_git_clone

        project = Project(
            name="test-repo",
            state=ProjectState.ACTIVE,
            source_type=SourceType.GITHUB,
            clone_url="https://github.com/org/test-repo.git",
        )

        config = Config(
            host="github.com/org",
            source_type=SourceType.GITHUB,
            path=tmp_path,
            branch="develop",
            mirror=False,
        )

        result = clone_github_repository(project, config)

        assert result.status == CloneStatus.SUCCESS
        cmd = mock_run.call_args[0][0]
        assert "--branch" in cmd
        assert "develop" in cmd

    @patch("gerrit_clone.github_worker.subprocess.run")
    def test_full_clone_by_default(
        self,
        mock_run: MagicMock,
        tmp_path: Path,
    ) -> None:
        """Test performs full clone by default (no --depth or --branch)."""
        mock_run.side_effect = _mock_successful_git_clone

        project = Project(
            name="test-repo",
            state=ProjectState.ACTIVE,
            source_type=SourceType.GITHUB,
            clone_url="https://github.com/org/test-repo.git",
            default_branch="main",
        )

        config = Config(
            host="github.com/org",
            source_type=SourceType.GITHUB,
            path=tmp_path,
        )

        result = clone_github_repository(project, config)

        assert result.status == CloneStatus.SUCCESS
        cmd = mock_run.call_args[0][0]
        # Should NOT have --branch or --single-branch for full clone
        assert "--branch" not in cmd
        assert "--single-branch" not in cmd
        # Should NOT have --depth for full history
        assert "--depth" not in cmd

    @patch("gerrit_clone.github_worker.subprocess.run")
    def test_handles_clone_failure(
        self,
        mock_run: MagicMock,
        tmp_path: Path,
    ) -> None:
        """Test handles clone failure gracefully."""
        mock_run.return_value = MagicMock(
            returncode=1,
            stderr="fatal: repository not found",
            stdout="",
        )

        project = Project(
            name="test-repo",
            state=ProjectState.ACTIVE,
            source_type=SourceType.GITHUB,
            clone_url="https://github.com/org/test-repo.git",
        )

        config = Config(
            host="github.com/org",
            source_type=SourceType.GITHUB,
            path=tmp_path,
        )

        result = clone_github_repository(project, config)

        assert result.status == CloneStatus.FAILED
        assert result.error_message is not None
        assert "Repository not found" in result.error_message

    @patch("gerrit_clone.github_worker.subprocess.run")
    def test_handles_timeout(
        self,
        mock_run: MagicMock,
        tmp_path: Path,
    ) -> None:
        """Test handles timeout gracefully."""
        mock_run.side_effect = TimeoutExpired("git", 10)

        project = Project(
            name="test-repo",
            state=ProjectState.ACTIVE,
            source_type=SourceType.GITHUB,
            clone_url="https://github.com/org/test-repo.git",
        )

        config = Config(
            host="github.com/org",
            source_type=SourceType.GITHUB,
            path=tmp_path,
            clone_timeout=10,
        )

        result = clone_github_repository(project, config)

        assert result.status == CloneStatus.FAILED
        assert result.error_message is not None
        assert "timeout" in result.error_message.lower()

    @patch("gerrit_clone.github_worker.subprocess.run")
    def test_uses_ssh_url_by_default(
        self,
        mock_run: MagicMock,
        tmp_path: Path,
    ) -> None:
        """Test uses SSH URL by default."""
        mock_run.side_effect = _mock_successful_git_clone

        project = Project(
            name="test-repo",
            state=ProjectState.ACTIVE,
            source_type=SourceType.GITHUB,
            clone_url="https://github.com/org/test-repo.git",
            ssh_url_override="git@github.com:org/test-repo.git",
        )

        config = Config(
            host="github.com/org",
            source_type=SourceType.GITHUB,
            path=tmp_path,
            # use_https not specified - should default to SSH
        )

        result = clone_github_repository(project, config)

        assert result.status == CloneStatus.SUCCESS
        cmd = mock_run.call_args[0][0]
        # Verify SSH URL was used by default
        assert "git@github.com:org/test-repo.git" in cmd

    @patch("gerrit_clone.github_worker.subprocess.run")
    def test_uses_ssh_url_when_not_https(
        self,
        mock_run: MagicMock,
        tmp_path: Path,
    ) -> None:
        """Test uses SSH URL when HTTPS is not requested."""
        mock_run.side_effect = _mock_successful_git_clone

        project = Project(
            name="test-repo",
            state=ProjectState.ACTIVE,
            source_type=SourceType.GITHUB,
            clone_url="https://github.com/org/test-repo.git",
            ssh_url_override="git@github.com:org/test-repo.git",
        )

        config = Config(
            host="github.com/org",
            source_type=SourceType.GITHUB,
            path=tmp_path,
            use_https=False,
        )

        result = clone_github_repository(project, config)

        assert result.status == CloneStatus.SUCCESS
        cmd = mock_run.call_args[0][0]
        # Verify SSH URL was used when explicitly set to False
        assert "git@github.com:org/test-repo.git" in cmd

    @patch("gerrit_clone.github_worker.subprocess.run")
    def test_uses_https_url_when_requested(
        self,
        mock_run: MagicMock,
        tmp_path: Path,
    ) -> None:
        """Test uses HTTPS URL when use_https is True."""
        mock_run.side_effect = _mock_successful_git_clone

        project = Project(
            name="test-repo",
            state=ProjectState.ACTIVE,
            source_type=SourceType.GITHUB,
            clone_url="https://github.com/org/test-repo.git",
            ssh_url_override="git@github.com:org/test-repo.git",
        )

        config = Config(
            host="github.com/org",
            source_type=SourceType.GITHUB,
            path=tmp_path,
            use_https=True,
        )

        result = clone_github_repository(project, config)

        assert result.status == CloneStatus.SUCCESS
        cmd = mock_run.call_args[0][0]
        # Verify HTTPS URL was used when explicitly requested
        assert "https://github.com/org/test-repo.git" in cmd

    @patch("gerrit_clone.github_worker.subprocess.run")
    def test_creates_parent_directory(
        self,
        mock_run: MagicMock,
        tmp_path: Path,
    ) -> None:
        """Test creates parent directories if they don't exist."""
        mock_run.side_effect = _mock_successful_git_clone

        project = Project(
            name="nested/test-repo",
            state=ProjectState.ACTIVE,
            source_type=SourceType.GITHUB,
            clone_url="https://github.com/org/test-repo.git",
        )

        config = Config(
            host="github.com/org",
            source_type=SourceType.GITHUB,
            path=tmp_path,
        )

        result = clone_github_repository(project, config)

        assert result.status == CloneStatus.SUCCESS
        # Verify parent directory was created
        assert (tmp_path / "nested").exists()

    @patch("gerrit_clone.github_worker.subprocess.run")
    def test_embeds_token_in_https_url(
        self,
        mock_run: MagicMock,
        tmp_path: Path,
    ) -> None:
        """Test embeds GitHub token in HTTPS URL."""
        mock_run.side_effect = _mock_successful_git_clone

        project = Project(
            name="test-repo",
            state=ProjectState.ACTIVE,
            source_type=SourceType.GITHUB,
            clone_url="https://github.com/org/test-repo.git",
            ssh_url_override="git@github.com:org/test-repo.git",
        )

        config = Config(
            host="github.com/org",
            source_type=SourceType.GITHUB,
            path=tmp_path,
            use_https=True,
            github_token="ghp_test123456789",
        )

        result = clone_github_repository(project, config)

        assert result.status == CloneStatus.SUCCESS
        cmd = mock_run.call_args_list[0][0][0]
        # Verify token was embedded in URL
        assert "https://ghp_test123456789@github.com/org/test-repo.git" in cmd

    @patch("gerrit_clone.github_worker.subprocess.run")
    def test_removes_token_from_remote_url_after_clone(
        self,
        mock_run: MagicMock,
        tmp_path: Path,
    ) -> None:
        """Test removes token from remote URL after successful clone."""

        # First call is git clone (success), second is git remote set-url
        def mock_git_calls(*args, **kwargs):
            cmd = args[0] if args else kwargs.get("cmd", [])
            if isinstance(cmd, list) and "clone" in cmd:
                # Create target directory for clone
                target_path = cmd[-1]
                Path(target_path).mkdir(parents=True, exist_ok=True)
            return MagicMock(returncode=0, stderr="", stdout="")

        mock_run.side_effect = mock_git_calls

        project = Project(
            name="test-repo",
            state=ProjectState.ACTIVE,
            source_type=SourceType.GITHUB,
            clone_url="https://github.com/org/test-repo.git",
        )

        config = Config(
            host="github.com/org",
            source_type=SourceType.GITHUB,
            path=tmp_path,
            use_https=True,
            github_token="ghp_test123456789",
        )

        result = clone_github_repository(project, config)

        assert result.status == CloneStatus.SUCCESS

        # Verify exactly two subprocess calls were made
        assert mock_run.call_count == 2, (
            "Should have two subprocess calls: git clone with token and git "
            "remote set-url to remove token"
        )

        # Verify git clone was called with token embedded
        clone_cmd = mock_run.call_args_list[0][0][0]
        clone_url = None
        for _, arg in enumerate(clone_cmd):
            if arg.startswith("https://"):
                clone_url = arg
                break

        assert clone_url is not None, "Clone command should contain an HTTPS URL"
        assert "ghp_test123456789@github.com" in clone_url, (
            f"Token should be embedded in clone URL: {clone_url}"
        )
        assert clone_url == "https://ghp_test123456789@github.com/org/test-repo.git", (
            f"Clone URL should have token embedded: {clone_url}"
        )

        # Verify git remote set-url was called to remove token
        remote_cmd = mock_run.call_args_list[1][0][0]
        assert remote_cmd == [
            "git",
            "remote",
            "set-url",
            "origin",
            "https://github.com/org/test-repo.git",
        ], "Remote set-url should remove the token from the URL"

        # Verify the URL in set-url does NOT contain the token
        assert "ghp_test123456789" not in " ".join(remote_cmd), (
            "Token should not be present in remote set-url command"
        )

    @patch("gerrit_clone.github_worker.subprocess.run")
    def test_sets_git_terminal_prompt_for_https(
        self,
        mock_run: MagicMock,
        tmp_path: Path,
    ) -> None:
        """Test sets GIT_TERMINAL_PROMPT=0 for HTTPS clones."""
        mock_run.side_effect = _mock_successful_git_clone

        project = Project(
            name="test-repo",
            state=ProjectState.ACTIVE,
            source_type=SourceType.GITHUB,
            clone_url="https://github.com/org/test-repo.git",
        )

        config = Config(
            host="github.com/org",
            source_type=SourceType.GITHUB,
            path=tmp_path,
            use_https=True,
            github_token="ghp_test123456789",
        )

        result = clone_github_repository(project, config)

        assert result.status == CloneStatus.SUCCESS
        # Verify environment variables were set
        env = mock_run.call_args_list[0][1]["env"]
        assert env["GIT_TERMINAL_PROMPT"] == "0"
        assert env["GIT_CONFIG_COUNT"] == "1"
        assert env["GIT_CONFIG_KEY_0"] == "credential.helper"
        assert env["GIT_CONFIG_VALUE_0"] == ""

    @patch("gerrit_clone.github_worker.subprocess.run")
    def test_https_without_token_uses_credential_helper(
        self,
        mock_run: MagicMock,
        tmp_path: Path,
    ) -> None:
        """Test HTTPS without token relies on git credential helper."""
        mock_run.side_effect = _mock_successful_git_clone

        project = Project(
            name="test-repo",
            state=ProjectState.ACTIVE,
            source_type=SourceType.GITHUB,
            clone_url="https://github.com/org/test-repo.git",
        )

        config = Config(
            host="github.com/org",
            source_type=SourceType.GITHUB,
            path=tmp_path,
            use_https=True,
            # No github_token provided
        )

        result = clone_github_repository(project, config)

        assert result.status == CloneStatus.SUCCESS
        cmd = mock_run.call_args[0][0]
        # Verify URL has no token embedded
        assert "https://github.com/org/test-repo.git" in cmd
        assert "@github.com" not in " ".join(cmd)

    @patch("gerrit_clone.github_worker.subprocess.run")
    def test_handles_token_removal_failure_gracefully(
        self,
        mock_run: MagicMock,
        tmp_path: Path,
    ) -> None:
        """Test that clone fails when token removal fails (security-critical)."""
        # First call is git clone (success), second is git remote set-url (failure)
        mock_run.side_effect = [
            MagicMock(returncode=0, stderr="", stdout=""),  # git clone
            subprocess.CalledProcessError(
                1, "git", stderr="error setting remote"
            ),  # git remote set-url
        ]

        project = Project(
            name="test-repo",
            state=ProjectState.ACTIVE,
            source_type=SourceType.GITHUB,
            clone_url="https://github.com/org/test-repo.git",
        )

        config = Config(
            host="github.com/org",
            source_type=SourceType.GITHUB,
            path=tmp_path,
            use_https=True,
            github_token="ghp_test123456789",
        )

        result = clone_github_repository(project, config)

        # Clone should FAIL if token removal fails (security-critical operation)
        # We cannot leave credentials in .git/config
        assert result.status == CloneStatus.FAILED
        assert result.error_message is not None
        assert "SECURITY" in result.error_message
        assert "token" in result.error_message.lower()
