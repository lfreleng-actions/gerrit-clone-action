# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2025 Matthew Watkins <mwatkins@linuxfoundation.org>

"""Unit tests for GitHub clone worker."""

from __future__ import annotations

import base64
import subprocess
from pathlib import Path
from subprocess import TimeoutExpired
from unittest.mock import MagicMock, patch

import pytest

from gerrit_clone.github_token_hygiene import remove_token_from_remote_url
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
    def test_single_branch_is_an_option_not_the_repository(
        self,
        mock_run: MagicMock,
        tmp_path: Path,
    ) -> None:
        """``--single-branch`` must land before the ``--`` separator.

        Inserted after it, git would read it as the repository argument
        and the clone would fail.
        """
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
            mirror=False,
            branch="develop",
        )

        result = clone_github_repository(project, config)

        assert result.status == CloneStatus.SUCCESS
        cmd = mock_run.call_args[0][0]
        separator = cmd.index("--")
        assert cmd.index("--single-branch") < separator
        assert cmd[separator + 1] == "https://github.com/org/test-repo.git"

    @patch("gerrit_clone.github_worker.subprocess.run")
    def test_a_branch_named_like_the_separator_keeps_its_argument(
        self,
        mock_run: MagicMock,
        tmp_path: Path,
    ) -> None:
        """The separator has to be located by position, not by value.

        ``git`` rejects ``--`` as a branch name, so this never reaches
        a real clone -- but searching for the separator would find the
        branch argument first and split ``--branch`` from its value,
        turning a clean rejection into a malformed command.
        """
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
            mirror=False,
            branch="--",
        )

        clone_github_repository(project, config)

        cmd = mock_run.call_args[0][0]
        # --branch keeps its own argument ...
        assert cmd[cmd.index("--branch") + 1] == "--"
        # ... and the trailing triple is intact, with the option ahead
        # of the separator rather than between the pair above.
        assert cmd[-3] == "--"
        assert cmd[-2] == "https://github.com/org/test-repo.git"
        assert cmd[-4] == "--single-branch"

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
    def test_keeps_token_out_of_the_clone_url(
        self,
        mock_run: MagicMock,
        tmp_path: Path,
    ) -> None:
        """The token must never reach the git command line.

        A URL argument is visible in the host's process listing for the
        lifetime of the clone, which nothing can redact after the fact.
        """
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
        assert "https://github.com/org/test-repo.git" in cmd
        assert "ghp_test123456789" not in " ".join(cmd)
        assert "@github.com" not in " ".join(cmd)

    @patch("gerrit_clone.github_worker.subprocess.run")
    def test_authenticates_through_the_environment(
        self,
        mock_run: MagicMock,
        tmp_path: Path,
    ) -> None:
        """Token auth travels in GIT_CONFIG_*, as the push path does."""
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
        env = mock_run.call_args_list[0][1]["env"]
        expected = base64.b64encode(b"x-access-token:ghp_test123456789").decode()
        entries = {
            env[f"GIT_CONFIG_KEY_{index}"]: env[f"GIT_CONFIG_VALUE_{index}"]
            for index in range(int(env["GIT_CONFIG_COUNT"]))
        }
        # Scoped to the clone URL's origin rather than a global
        # http.extraheader, which git would send to any host it reached.
        auth_key = "http.https://github.com.extraheader"
        assert entries[auth_key] == f"AUTHORIZATION: basic {expected}"
        # The raw token itself is never placed in the environment.
        assert "ghp_test123456789" not in " ".join(entries.values())

    @patch("gerrit_clone.github_worker.subprocess.run")
    def test_no_token_removal_needed_after_clone(
        self,
        mock_run: MagicMock,
        tmp_path: Path,
    ) -> None:
        """A credential-free clone URL leaves nothing to strip afterwards."""

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
        assert mock_run.call_count == 1, (
            "Only the clone itself should run: the remote URL never held a token"
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
        # Credential helpers stay disabled alongside the token header, so
        # both entries share one GIT_CONFIG_COUNT block.
        assert env["GIT_CONFIG_COUNT"] == "2"
        assert env["GIT_CONFIG_KEY_0"] == "credential.helper"
        assert env["GIT_CONFIG_VALUE_0"] == ""
        assert env["GIT_CONFIG_KEY_1"] == "http.https://github.com.extraheader"

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

    def test_a_credentialed_project_url_is_not_written_back(
        self, tmp_path: Path
    ) -> None:
        """The replacement comes from the value under suspicion.

        ``project.clone_url`` is what carried the token in, so setting
        the remote back to it would leave the credential in
        .git/config while reporting a successful scrub.  Refusing takes
        the destroy-the-clone path instead.
        """
        token = "ghp_test123456789"
        repo_path = tmp_path / "repo"
        (repo_path / ".git").mkdir(parents=True)

        project = Project(
            name="org/test-repo",
            state=ProjectState.ACTIVE,
            source_type=SourceType.GITHUB,
            clone_url=f"https://{token}@github.com/org/test-repo.git",
        )
        config = Config(
            host="github.com/org",
            source_type=SourceType.GITHUB,
            path=tmp_path,
            use_https=True,
            github_token=token,
        )

        with patch("gerrit_clone.github_token_hygiene.subprocess.run") as mock_run:
            with pytest.raises(RuntimeError) as excinfo:
                remove_token_from_remote_url(repo_path, project, config)

            # Never even attempted: there was no clean URL to write.
            mock_run.assert_not_called()

        assert "SECURITY" in str(excinfo.value)
        assert token not in str(excinfo.value)
        assert not repo_path.exists()

    @patch("gerrit_clone.github_token_hygiene.subprocess.run")
    def test_handles_token_removal_failure_gracefully(
        self,
        mock_run: MagicMock,
        tmp_path: Path,
    ) -> None:
        """Token removal is security-critical, so a failure destroys the clone.

        Driven directly rather than through ``clone_github_repository``:
        the resolved clone URL is credential-free, so the clone path no
        longer reaches this. It stays as the check on that invariant.
        """
        mock_run.side_effect = subprocess.CalledProcessError(
            1, "git", stderr="error setting remote"
        )

        repo_path = tmp_path / "test-repo"
        repo_path.mkdir()

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

        with pytest.raises(RuntimeError) as excinfo:
            remove_token_from_remote_url(repo_path, project, config)

        assert "SECURITY" in str(excinfo.value)
        assert "token" in str(excinfo.value).lower()
        # The repository is destroyed rather than left holding a credential.
        assert not repo_path.exists()
