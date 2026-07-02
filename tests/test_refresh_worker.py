# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2025 Matthew Watkins <mwatkins@linuxfoundation.org>

"""Unit tests for refresh worker."""

from __future__ import annotations

import subprocess
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import Mock, patch

import pytest

from gerrit_clone.models import Config, RefreshResult, RefreshStatus, RetryPolicy
from gerrit_clone.refresh_worker import (
    RefreshTimeoutError,
    RefreshWorker,
    StashOutcome,
)


@pytest.fixture
def worker():
    """Create a refresh worker for testing."""
    return RefreshWorker(
        retry_policy=RetryPolicy(max_attempts=2, base_delay=0.1),
        timeout=10,
        # Disable SSH handshake jitter so its remote-URL lookup does not
        # perturb tests that mock subprocess.run for the network helpers.
        # Dedicated jitter tests construct their own workers.
        ssh_jitter_seconds=0,
    )


@pytest.fixture
def ssh_config(tmp_path):
    """Create a temporary SSH config that disables the SSH agent.

    This configuration maintains SSH security features (host key verification)
    while preventing SSH agent prompts during tests. No actual SSH connections
    are made in these tests, so the empty known_hosts file is sufficient.
    """
    ssh_config_path = tmp_path / "ssh_config"
    known_hosts_path = tmp_path / "known_hosts"

    # Create empty known_hosts file (enables proper security)
    known_hosts_path.touch()

    ssh_config_content = f"""
# Test SSH config - prevents SSH agent prompts while maintaining security
Host *
    IdentityAgent none
    IdentitiesOnly yes
    IdentityFile /dev/null
    BatchMode yes
    StrictHostKeyChecking yes
    UserKnownHostsFile {known_hosts_path}
    ConnectTimeout 1
"""
    ssh_config_path.write_text(ssh_config_content)
    return ssh_config_path


@pytest.fixture
def temp_git_repo(tmp_path, ssh_config, monkeypatch):
    """Create a temporary git repository for testing."""
    # Set GIT_SSH_COMMAND to use our custom SSH config that doesn't use the agent
    git_ssh_command = f"ssh -F {ssh_config} -o IdentityAgent=none"
    monkeypatch.setenv("GIT_SSH_COMMAND", git_ssh_command)

    repo_path = tmp_path / "test-repo"
    repo_path.mkdir()

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
    # Set SSH command to use our isolated config
    subprocess.run(
        ["git", "config", "core.sshCommand", git_ssh_command],
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
        ["git", "-c", "commit.gpgsign=false", "commit", "-m", "Initial commit"],
        cwd=repo_path,
        capture_output=True,
        check=True,
    )

    # Add a Gerrit-like remote
    subprocess.run(
        ["git", "remote", "add", "origin", "ssh://gerrit.example.org:29418/test-repo"],
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

    # Create a fake remote tracking branch by fetching from a local mirror
    # This ensures we have origin/<branch> for upstream tracking
    subprocess.run(
        ["git", "config", "remote.origin.fetch", "+refs/heads/*:refs/remotes/origin/*"],
        cwd=repo_path,
        capture_output=True,
        check=True,
    )

    # Create the remote tracking branch manually
    subprocess.run(
        ["git", "update-ref", f"refs/remotes/origin/{current_branch}", "HEAD"],
        cwd=repo_path,
        capture_output=True,
        check=True,
    )

    # Set up upstream tracking for the current branch
    subprocess.run(
        ["git", "branch", f"--set-upstream-to=origin/{current_branch}", current_branch],
        cwd=repo_path,
        capture_output=True,
        check=True,
    )

    return repo_path


class TestRefreshWorker:
    """Tests for RefreshWorker class."""

    def test_init(self):
        """Test worker initialization."""
        worker = RefreshWorker(
            timeout=120,
            fetch_only=True,
            prune=False,
            auto_stash=True,
            strategy="rebase",
        )

        assert worker.timeout == 120
        assert worker.fetch_only is True
        assert worker.prune is False
        assert worker.auto_stash is True
        assert worker.strategy == "rebase"

    def test_is_git_repository_valid(self, worker, temp_git_repo):
        """Test detecting valid git repository."""
        assert worker._is_git_repository(temp_git_repo) is True

    def test_is_git_repository_invalid(self, worker, tmp_path):
        """Test detecting non-git directory."""
        assert worker._is_git_repository(tmp_path) is False

    def test_get_remote_url(self, worker, temp_git_repo):
        """Test getting remote URL."""
        url = worker._get_remote_url(temp_git_repo)
        assert url == "ssh://gerrit.example.org:29418/test-repo"

    def test_get_remote_url_no_remote(self, worker, tmp_path):
        """Test getting remote URL when no remote exists."""
        repo_path = tmp_path / "no-remote"
        repo_path.mkdir()
        subprocess.run(
            ["git", "init", "-b", "main"],
            cwd=repo_path,
            capture_output=True,
            check=True,
        )

        url = worker._get_remote_url(repo_path)
        assert url is None

    def test_is_gerrit_repository_ssh(self, worker):
        """Test detecting Gerrit repository from SSH URL."""
        assert (
            worker._is_gerrit_repository("ssh://gerrit.example.org:29418/project")
            is True
        )
        assert worker._is_gerrit_repository("ssh://host:29418/project") is True

    def test_is_gerrit_repository_https(self, worker):
        """Test detecting Gerrit repository from HTTPS URL."""
        assert (
            worker._is_gerrit_repository("https://gerrit.example.org/r/project") is True
        )
        assert worker._is_gerrit_repository("https://host/gerrit/project") is True
        assert (
            worker._is_gerrit_repository("https://review.example.org/project") is True
        )

    def test_is_gerrit_repository_non_gerrit(self, worker):
        """Test rejecting non-Gerrit URLs."""
        assert worker._is_gerrit_repository("https://github.com/user/repo") is False
        assert worker._is_gerrit_repository("git@gitlab.com:user/repo.git") is False
        assert worker._is_gerrit_repository(None) is False

    def test_check_repository_state_clean(self, worker, temp_git_repo):
        """Test checking repository state with clean working directory."""
        state = worker._check_repository_state(temp_git_repo)

        assert state["branch"] is not None  # Should have a branch
        assert state["detached_head"] is False
        assert state["has_uncommitted"] is False

    def test_check_repository_state_uncommitted(self, worker, temp_git_repo):
        """Test checking repository state with uncommitted changes."""
        # Create uncommitted file
        (temp_git_repo / "new_file.txt").write_text("new content")

        state = worker._check_repository_state(temp_git_repo)

        assert state["has_uncommitted"] is True

    def test_check_repository_state_detached_head(self, worker, temp_git_repo):
        """Test checking repository state in detached HEAD."""
        # Get current commit hash
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=temp_git_repo,
            capture_output=True,
            text=True,
            check=True,
        )
        commit_hash = result.stdout.strip()

        # Checkout detached HEAD
        subprocess.run(
            ["git", "checkout", commit_hash],
            cwd=temp_git_repo,
            capture_output=True,
            check=True,
        )

        state = worker._check_repository_state(temp_git_repo)

        assert state["detached_head"] is True

    def test_stash_changes(self, worker, temp_git_repo):
        """Test stashing uncommitted changes."""
        # Create uncommitted changes
        (temp_git_repo / "uncommitted.txt").write_text("uncommitted")

        outcome = worker._stash_changes(temp_git_repo)
        assert outcome is StashOutcome.CREATED

        # Verify working directory is clean
        result = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=temp_git_repo,
            capture_output=True,
            text=True,
            check=True,
        )
        assert result.stdout.strip() == ""

    def test_stash_changes_nothing_to_stash(self, worker, temp_git_repo):
        """A clean tree yields NOTHING_TO_STASH, not a failure."""
        outcome = worker._stash_changes(temp_git_repo)
        assert outcome is StashOutcome.NOTHING_TO_STASH

    def test_pop_stash(self, worker, temp_git_repo):
        """Test popping stashed changes."""
        # Create and stash changes
        (temp_git_repo / "uncommitted.txt").write_text("uncommitted")
        worker._stash_changes(temp_git_repo)

        success = worker._pop_stash(temp_git_repo)
        assert success is True

        # Verify changes are restored
        assert (temp_git_repo / "uncommitted.txt").read_text() == "uncommitted"

    def test_build_git_environment_basic(self, worker):
        """Test building git environment without config."""
        env = worker._build_git_environment()

        assert "GIT_TERMINAL_PROMPT" in env
        assert env["GIT_TERMINAL_PROMPT"] == "0"

    def test_build_git_environment_with_ssh(self):
        """Test building git environment with SSH config."""
        config = Config(
            host="gerrit.example.org",
            ssh_user="testuser",
            strict_host_checking=False,
        )

        worker = RefreshWorker(config=config)
        env = worker._build_git_environment()

        assert "GIT_SSH_COMMAND" in env
        assert "StrictHostKeyChecking" in env["GIT_SSH_COMMAND"]

    def test_analyze_git_error_network(self, worker):
        """Test analyzing network errors."""
        process_result = Mock()
        process_result.returncode = 128
        process_result.stdout = ""
        process_result.stderr = "fatal: Could not resolve host: gerrit.example.org"

        error_msg = worker._analyze_git_error(process_result, "fetch")

        assert "Network error" in error_msg

    def test_analyze_git_error_auth(self, worker):
        """Test analyzing authentication errors."""
        process_result = Mock()
        process_result.returncode = 128
        process_result.stdout = ""
        process_result.stderr = "Permission denied (publickey)"

        error_msg = worker._analyze_git_error(process_result, "fetch")

        assert "Authentication error" in error_msg

    def test_analyze_git_error_conflict(self, worker):
        """Test analyzing conflict errors."""
        process_result = Mock()
        process_result.returncode = 1
        process_result.stdout = "CONFLICT (content): Merge conflict in file.txt"
        process_result.stderr = ""

        error_msg = worker._analyze_git_error(process_result, "pull")

        assert "conflicts" in error_msg.lower()

    def test_is_retryable_git_error_network(self, worker):
        """Test identifying retryable network errors."""
        process_result = Mock()
        process_result.returncode = 128
        process_result.stdout = ""
        process_result.stderr = "Connection timed out"

        assert worker._is_retryable_git_error(process_result) is True

    def test_is_retryable_git_error_auth(self, worker):
        """Test identifying non-retryable auth errors."""
        process_result = Mock()
        process_result.returncode = 128
        process_result.stdout = ""
        process_result.stderr = "Permission denied"

        assert worker._is_retryable_git_error(process_result) is False

    def test_is_retryable_error_network(self, worker):
        """Test identifying retryable error messages."""
        assert worker._is_retryable_error("Network error during fetch") is True
        assert worker._is_retryable_error("Connection timeout") is True

    def test_is_retryable_error_non_retryable(self, worker):
        """Test identifying non-retryable error messages."""
        assert worker._is_retryable_error("Authentication failed") is False
        assert worker._is_retryable_error("Permission denied") is False

    def test_calculate_adaptive_delay(self, worker):
        """Test adaptive delay calculation."""
        delay1 = worker._calculate_adaptive_delay(1)
        delay2 = worker._calculate_adaptive_delay(2)
        delay3 = worker._calculate_adaptive_delay(3)

        # Delays should increase exponentially
        assert delay2 > delay1
        assert delay3 > delay2

    def test_count_pulled_commits_fast_forward(self, worker):
        """Test counting commits from fast-forward output."""
        output = (
            "Updating abc123..def456\nFast-forward\n 1 file changed, 2 insertions(+)"
        )

        count = worker._count_pulled_commits(output)
        assert count >= 1

    def test_count_pulled_commits_up_to_date(self, worker):
        """Test counting commits when already up-to-date."""
        output = "Already up to date."

        count = worker._count_pulled_commits(output)
        assert count == 0

    def test_count_changed_files(self, worker):
        """Test counting changed files from output."""
        output = "3 files changed, 10 insertions(+), 5 deletions(-)"

        count = worker._count_changed_files(output)
        assert count == 3

    def test_count_changed_files_no_changes(self, worker):
        """Test counting files when no changes."""
        output = "Already up to date."

        count = worker._count_changed_files(output)
        assert count == 0

    def test_get_project_name(self, worker, tmp_path):
        """Test getting project name from path."""
        repo_path = tmp_path / "my-project"
        repo_path.mkdir()

        name = worker._get_project_name(repo_path)
        assert name == "my-project"

    def test_refresh_repository_not_git(self, worker, tmp_path):
        """Test refreshing non-git directory."""
        result = worker.refresh_repository(tmp_path)

        assert result.status == RefreshStatus.NOT_GIT_REPO
        assert result.error_message == "Not a Git repository"

    def test_refresh_repository_not_gerrit(self, tmp_path):
        """Test refreshing non-Gerrit repository."""
        # Create git repo with GitHub remote
        repo_path = tmp_path / "github-repo"
        repo_path.mkdir()
        subprocess.run(
            ["git", "init", "-b", "main"],
            cwd=repo_path,
            capture_output=True,
            check=True,
        )
        subprocess.run(
            ["git", "remote", "add", "origin", "https://github.com/user/repo.git"],
            cwd=repo_path,
            capture_output=True,
            check=True,
        )

        worker = RefreshWorker(filter_gerrit_only=True)
        result = worker.refresh_repository(repo_path)

        assert result.status == RefreshStatus.NOT_GERRIT_REPO

    def test_refresh_repository_detached_head(self, worker, temp_git_repo):
        """Test refreshing repository in detached HEAD state."""
        # Get current commit and checkout detached HEAD
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=temp_git_repo,
            capture_output=True,
            text=True,
            check=True,
        )
        commit_hash = result.stdout.strip()
        subprocess.run(
            ["git", "checkout", commit_hash],
            cwd=temp_git_repo,
            capture_output=True,
            check=True,
        )

        result = worker.refresh_repository(temp_git_repo)

        assert result.status == RefreshStatus.DETACHED_HEAD
        assert result.detached_head is True

    def test_refresh_repository_uncommitted_skip(self, temp_git_repo):
        """Test skipping repository with uncommitted changes."""
        # Create uncommitted changes
        (temp_git_repo / "uncommitted.txt").write_text("uncommitted")

        worker = RefreshWorker(
            skip_conflicts=True, auto_stash=False, filter_gerrit_only=False
        )
        result = worker.refresh_repository(temp_git_repo)

        assert result.status == RefreshStatus.UNCOMMITTED_CHANGES
        assert result.had_uncommitted_changes is True

    def test_refresh_repository_uncommitted_auto_stash(self, temp_git_repo):
        """Test auto-stashing uncommitted changes."""
        # Create uncommitted changes
        (temp_git_repo / "uncommitted.txt").write_text("uncommitted")

        worker = RefreshWorker(
            skip_conflicts=False,
            auto_stash=True,
            filter_gerrit_only=False,  # Allow any remote for testing
        )

        with patch.object(worker, "_execute_adaptive_refresh", return_value=True):
            result = worker.refresh_repository(temp_git_repo)

        assert result.stash_created is True
        assert result.had_uncommitted_changes is True

    @patch("gerrit_clone.refresh_worker.subprocess.run")
    def test_execute_git_fetch_success(self, mock_run, worker, temp_git_repo):
        """Test successful git fetch execution."""
        mock_result = Mock()
        mock_result.returncode = 0
        mock_result.stdout = ""
        mock_result.stderr = "From ssh://gerrit.example.org:29418/test-repo\n   abc123..def456  main -> origin/main"
        mock_run.return_value = mock_result

        result = RefreshResult(
            path=temp_git_repo,
            project_name="test-repo",
            status=RefreshStatus.PENDING,
            started_at=datetime.now(UTC),
        )

        success = worker._execute_git_fetch(temp_git_repo, result)

        assert success is True
        assert result.commits_pulled > 0

    @patch("gerrit_clone.refresh_worker.subprocess.run")
    def test_execute_git_fetch_timeout(self, mock_run, worker, temp_git_repo):
        """Test git fetch timeout."""
        mock_run.side_effect = subprocess.TimeoutExpired(
            cmd=["git", "fetch"], timeout=10
        )

        result = RefreshResult(
            path=temp_git_repo,
            project_name="test-repo",
            status=RefreshStatus.PENDING,
            started_at=datetime.now(UTC),
        )

        with pytest.raises(RefreshTimeoutError):
            worker._execute_git_fetch(temp_git_repo, result)

    @patch("gerrit_clone.refresh_worker.subprocess.run")
    def test_execute_git_pull_success(self, mock_run, worker, temp_git_repo):
        """Test successful git pull execution."""
        mock_result = Mock()
        mock_result.returncode = 0
        mock_result.stdout = (
            "Updating abc123..def456\nFast-forward\n 2 files changed, 10 insertions(+)"
        )
        mock_result.stderr = ""
        mock_run.return_value = mock_result

        result = RefreshResult(
            path=temp_git_repo,
            project_name="test-repo",
            status=RefreshStatus.PENDING,
            started_at=datetime.now(UTC),
        )

        success = worker._execute_git_pull(temp_git_repo, result)

        assert success is True
        assert result.commits_pulled >= 1
        assert result.files_changed == 2

    @patch("gerrit_clone.refresh_worker.subprocess.run")
    def test_execute_git_pull_conflict(self, mock_run, worker, temp_git_repo):
        """Test git pull with conflicts."""
        mock_result = Mock()
        mock_result.returncode = 1
        mock_result.stdout = "CONFLICT (content): Merge conflict in file.txt"
        mock_result.stderr = ""
        mock_run.return_value = mock_result

        result = RefreshResult(
            path=temp_git_repo,
            project_name="test-repo",
            status=RefreshStatus.PENDING,
            started_at=datetime.now(UTC),
        )

        success = worker._execute_git_pull(temp_git_repo, result)

        assert success is False
        assert result.status == RefreshStatus.CONFLICTS


class TestRefreshWorkerIntegration:
    """Integration tests for RefreshWorker."""

    def test_full_refresh_workflow_no_changes(self, temp_git_repo):
        """Test complete refresh workflow when already up-to-date."""
        worker = RefreshWorker(
            filter_gerrit_only=False,  # Allow any remote for testing
            fetch_only=True,  # Use fetch only to avoid needing real remote
        )

        # Mock both _execute_git_fetch and _check_repository_state to ensure upstream exists
        with (
            patch.object(worker, "_execute_git_fetch") as mock_fetch,
            patch.object(worker, "_check_repository_state") as mock_state,
        ):
            mock_fetch.return_value = True
            mock_state.return_value = {
                "branch": "master",
                "detached_head": False,
                "has_uncommitted": False,
                "has_upstream": True,
                "on_meta_config": False,
            }

            result = worker.refresh_repository(temp_git_repo)

            assert result.success is True
            assert result.project_name == temp_git_repo.name
            assert result.completed_at is not None
            assert result.duration_seconds > 0


class TestMetaOnlyRepo:
    """Test Gerrit meta-only repository detection."""

    @patch("gerrit_clone.refresh_worker.subprocess.run")
    def test_is_meta_only_repo_with_no_heads_and_meta_config(
        self, mock_run, worker, temp_git_repo
    ):
        """Test detection of meta-only repo (no heads, has meta/config)."""
        # First call: ls-remote --heads (no output = no heads)
        mock_heads_result = Mock()
        mock_heads_result.returncode = 0
        mock_heads_result.stdout = ""

        # Second call: ls-remote refs/meta/config (exists)
        mock_meta_result = Mock()
        mock_meta_result.returncode = 0
        mock_meta_result.stdout = "abc123def456\trefs/meta/config"

        mock_run.side_effect = [mock_heads_result, mock_meta_result]

        result = worker._is_meta_only_repo(temp_git_repo)

        assert result is True
        assert mock_run.call_count == 2

    @patch("gerrit_clone.refresh_worker.subprocess.run")
    def test_is_meta_only_repo_with_heads(self, mock_run, worker, temp_git_repo):
        """Test repo with regular heads is not meta-only."""
        mock_result = Mock()
        mock_result.returncode = 0
        mock_result.stdout = "abc123def456\trefs/heads/master\n"
        mock_run.return_value = mock_result

        result = worker._is_meta_only_repo(temp_git_repo)

        assert result is False
        assert mock_run.call_count == 1

    @patch("gerrit_clone.refresh_worker.subprocess.run")
    def test_is_meta_only_repo_no_heads_no_meta(self, mock_run, worker, temp_git_repo):
        """Test repo with no heads and no meta/config is not meta-only."""
        # First call: ls-remote --heads (no output)
        mock_heads_result = Mock()
        mock_heads_result.returncode = 0
        mock_heads_result.stdout = ""

        # Second call: ls-remote refs/meta/config (doesn't exist)
        mock_meta_result = Mock()
        mock_meta_result.returncode = 0
        mock_meta_result.stdout = ""

        mock_run.side_effect = [mock_heads_result, mock_meta_result]

        result = worker._is_meta_only_repo(temp_git_repo)

        assert result is False

    @patch("gerrit_clone.refresh_worker.subprocess.run")
    def test_is_meta_only_repo_git_error(self, mock_run, worker, temp_git_repo):
        """Test meta-only check handles git errors gracefully."""
        mock_result = Mock()
        mock_result.returncode = 1
        mock_result.stdout = ""
        mock_run.return_value = mock_result

        result = worker._is_meta_only_repo(temp_git_repo)

        assert result is False

    @patch("gerrit_clone.refresh_worker.subprocess.run")
    def test_is_meta_only_repo_exception(self, mock_run, worker, temp_git_repo):
        """Test meta-only check handles exceptions gracefully."""
        mock_run.side_effect = Exception("Network error")

        result = worker._is_meta_only_repo(temp_git_repo)

        assert result is False


class TestGetDefaultBranch:
    """Test default branch detection."""

    @patch("gerrit_clone.refresh_worker.subprocess.run")
    def test_get_default_branch_via_ls_remote(self, mock_run, worker, temp_git_repo):
        """Test getting default branch via ls-remote --symref."""
        mock_result = Mock()
        mock_result.returncode = 0
        mock_result.stdout = "ref: refs/heads/master\tHEAD\nabc123\tHEAD"
        mock_run.return_value = mock_result

        result = worker._get_default_branch(temp_git_repo)

        assert result == "master"

    @patch("gerrit_clone.refresh_worker.subprocess.run")
    def test_get_default_branch_via_ls_remote_main(
        self, mock_run, worker, temp_git_repo
    ):
        """Test getting default branch when it's 'main'."""
        mock_result = Mock()
        mock_result.returncode = 0
        mock_result.stdout = "ref: refs/heads/main\tHEAD"
        mock_run.return_value = mock_result

        result = worker._get_default_branch(temp_git_repo)

        assert result == "main"

    @patch("gerrit_clone.refresh_worker.subprocess.run")
    def test_get_default_branch_skips_meta_refs(self, mock_run, worker, temp_git_repo):
        """Test that meta/* refs are skipped when looking for default branch."""
        # First call: ls-remote returns meta/config (should be skipped)
        mock_ls_remote = Mock()
        mock_ls_remote.returncode = 0
        mock_ls_remote.stdout = "ref: refs/heads/meta/config\tHEAD"

        # Second call: symbolic-ref fails
        mock_symbolic = Mock()
        mock_symbolic.returncode = 1
        mock_symbolic.stdout = ""

        # Third call: check for master
        mock_master = Mock()
        mock_master.returncode = 0
        mock_master.stdout = "abc123\trefs/heads/master"

        mock_run.side_effect = [mock_ls_remote, mock_symbolic, mock_master]

        result = worker._get_default_branch(temp_git_repo)

        assert result == "master"

    @patch("gerrit_clone.refresh_worker.subprocess.run")
    def test_get_default_branch_via_symbolic_ref(self, mock_run, worker, temp_git_repo):
        """Test getting default branch via symbolic-ref."""
        # First call: ls-remote fails
        mock_ls_remote = Mock()
        mock_ls_remote.returncode = 1
        mock_ls_remote.stdout = ""

        # Second call: symbolic-ref succeeds
        mock_symbolic = Mock()
        mock_symbolic.returncode = 0
        mock_symbolic.stdout = "refs/remotes/origin/develop\n"

        mock_run.side_effect = [mock_ls_remote, mock_symbolic]

        result = worker._get_default_branch(temp_git_repo)

        assert result == "develop"

    @patch("gerrit_clone.refresh_worker.subprocess.run")
    def test_get_default_branch_fallback_to_common_names(
        self, mock_run, worker, temp_git_repo
    ):
        """Test fallback to checking common branch names."""
        # First call: ls-remote --symref fails
        mock_ls_remote = Mock()
        mock_ls_remote.returncode = 1

        # Second call: symbolic-ref fails
        mock_symbolic = Mock()
        mock_symbolic.returncode = 1

        # Third call: check master (fails)
        mock_master = Mock()
        mock_master.returncode = 0
        mock_master.stdout = ""

        # Fourth call: check main (succeeds)
        mock_main = Mock()
        mock_main.returncode = 0
        mock_main.stdout = "abc123\trefs/heads/main"

        mock_run.side_effect = [mock_ls_remote, mock_symbolic, mock_master, mock_main]

        result = worker._get_default_branch(temp_git_repo)

        assert result == "main"

    @patch("gerrit_clone.refresh_worker.subprocess.run")
    def test_get_default_branch_not_found(self, mock_run, worker, temp_git_repo):
        """Test when no default branch is found."""
        mock_result = Mock()
        mock_result.returncode = 1
        mock_result.stdout = ""
        mock_run.return_value = mock_result

        result = worker._get_default_branch(temp_git_repo)

        assert result is None

    @patch("gerrit_clone.refresh_worker.subprocess.run")
    def test_get_default_branch_exception(self, mock_run, worker, temp_git_repo):
        """Test exception handling in get_default_branch."""
        mock_run.side_effect = Exception("Network timeout")

        result = worker._get_default_branch(temp_git_repo)

        assert result is None


class TestFixDetachedHead:
    """Test detached HEAD fixing functionality."""

    @patch("gerrit_clone.refresh_worker.RefreshWorker._is_meta_only_repo")
    @patch("gerrit_clone.refresh_worker.RefreshWorker._get_default_branch")
    @patch("gerrit_clone.refresh_worker.RefreshWorker._is_on_meta_config")
    @patch("gerrit_clone.refresh_worker.subprocess.run")
    def test_fix_detached_head_success(
        self,
        mock_run,
        mock_meta_config,
        mock_default_branch,
        mock_meta_only,
        worker,
        temp_git_repo,
    ):
        """Test successfully fixing detached HEAD."""
        mock_meta_config.return_value = False
        mock_meta_only.return_value = False
        mock_default_branch.return_value = "main"

        # Mock fetch
        mock_fetch = Mock()
        mock_fetch.returncode = 0

        # Mock checkout
        mock_checkout = Mock()
        mock_checkout.returncode = 0

        # Mock set-upstream
        mock_upstream = Mock()
        mock_upstream.returncode = 0

        mock_run.side_effect = [mock_fetch, mock_checkout, mock_upstream]

        result = RefreshResult(
            path=temp_git_repo,
            project_name="test-repo",
            status=RefreshStatus.PENDING,
            started_at=datetime.now(UTC),
        )

        success = worker._fix_detached_head(temp_git_repo, result)

        assert success is True
        assert mock_run.call_count == 3

    @patch("gerrit_clone.refresh_worker.RefreshWorker._is_meta_only_repo")
    @patch("gerrit_clone.refresh_worker.RefreshWorker._get_default_branch")
    @patch("gerrit_clone.refresh_worker.RefreshWorker._is_on_meta_config")
    @patch("gerrit_clone.refresh_worker.subprocess.run")
    def test_fix_detached_head_meta_only_repo(
        self,
        mock_run,
        mock_meta_config,
        mock_default_branch,
        mock_meta_only,
        worker,
        temp_git_repo,
    ):
        """Test handling of Gerrit meta-only repos."""
        mock_meta_config.return_value = True
        mock_meta_only.return_value = True

        # Mock fetch
        mock_fetch = Mock()
        mock_fetch.returncode = 0
        mock_run.return_value = mock_fetch

        result = RefreshResult(
            path=temp_git_repo,
            project_name="test-repo",
            status=RefreshStatus.PENDING,
            started_at=datetime.now(UTC),
        )

        success = worker._fix_detached_head(temp_git_repo, result)

        assert success is False
        assert result.error_message is not None
        assert "meta-only" in result.error_message

    @patch("gerrit_clone.refresh_worker.RefreshWorker._is_meta_only_repo")
    @patch("gerrit_clone.refresh_worker.RefreshWorker._get_default_branch")
    @patch("gerrit_clone.refresh_worker.RefreshWorker._is_on_meta_config")
    @patch("gerrit_clone.refresh_worker.subprocess.run")
    def test_fix_detached_head_no_default_branch(
        self,
        mock_run,
        mock_meta_config,
        mock_default_branch,
        mock_meta_only,
        worker,
        temp_git_repo,
    ):
        """Test when default branch cannot be determined."""
        mock_meta_config.return_value = False
        mock_meta_only.return_value = False
        mock_default_branch.return_value = None

        # Mock fetch
        mock_fetch = Mock()
        mock_fetch.returncode = 0
        mock_run.return_value = mock_fetch

        result = RefreshResult(
            path=temp_git_repo,
            project_name="test-repo",
            status=RefreshStatus.PENDING,
            started_at=datetime.now(UTC),
        )

        success = worker._fix_detached_head(temp_git_repo, result)

        assert success is False

    @patch("gerrit_clone.refresh_worker.RefreshWorker._is_meta_only_repo")
    @patch("gerrit_clone.refresh_worker.RefreshWorker._get_default_branch")
    @patch("gerrit_clone.refresh_worker.RefreshWorker._is_on_meta_config")
    @patch("gerrit_clone.refresh_worker.subprocess.run")
    def test_fix_detached_head_checkout_fails(
        self,
        mock_run,
        mock_meta_config,
        mock_default_branch,
        mock_meta_only,
        worker,
        temp_git_repo,
    ):
        """Test when checkout fails."""
        mock_meta_config.return_value = False
        mock_meta_only.return_value = False
        mock_default_branch.return_value = "main"

        # Mock fetch (success)
        mock_fetch = Mock()
        mock_fetch.returncode = 0

        # Mock checkout (failure)
        mock_checkout = Mock()
        mock_checkout.returncode = 1
        mock_checkout.stderr = (
            "error: pathspec 'main' did not match any file(s) known to git"
        )

        mock_run.side_effect = [mock_fetch, mock_checkout]

        result = RefreshResult(
            path=temp_git_repo,
            project_name="test-repo",
            status=RefreshStatus.PENDING,
            started_at=datetime.now(UTC),
        )

        success = worker._fix_detached_head(temp_git_repo, result)

        assert success is False

    @patch("gerrit_clone.refresh_worker.RefreshWorker._is_meta_only_repo")
    @patch("gerrit_clone.refresh_worker.RefreshWorker._get_default_branch")
    @patch("gerrit_clone.refresh_worker.RefreshWorker._is_on_meta_config")
    @patch("gerrit_clone.refresh_worker.subprocess.run")
    def test_fix_detached_head_fetch_fails(
        self,
        mock_run,
        mock_meta_config,
        mock_default_branch,
        mock_meta_only,
        worker,
        temp_git_repo,
    ):
        """Test when fetch fails but continues."""
        mock_meta_config.return_value = False
        mock_meta_only.return_value = False
        mock_default_branch.return_value = "main"

        # Mock fetch (failure)
        mock_fetch = Mock()
        mock_fetch.returncode = 1
        mock_fetch.stderr = "Could not resolve host"

        # Mock checkout (success)
        mock_checkout = Mock()
        mock_checkout.returncode = 0

        # Mock set-upstream
        mock_upstream = Mock()
        mock_upstream.returncode = 0

        mock_run.side_effect = [mock_fetch, mock_checkout, mock_upstream]

        result = RefreshResult(
            path=temp_git_repo,
            project_name="test-repo",
            status=RefreshStatus.PENDING,
            started_at=datetime.now(UTC),
        )

        success = worker._fix_detached_head(temp_git_repo, result)

        assert success is True  # Should succeed despite fetch failure

    @patch("gerrit_clone.refresh_worker.RefreshWorker._is_on_meta_config")
    @patch("gerrit_clone.refresh_worker.subprocess.run")
    def test_fix_detached_head_exception(
        self, mock_run, mock_meta_config, worker, temp_git_repo
    ):
        """Test exception handling in fix_detached_head."""
        mock_meta_config.side_effect = Exception("Unexpected error")

        result = RefreshResult(
            path=temp_git_repo,
            project_name="test-repo",
            status=RefreshStatus.PENDING,
            started_at=datetime.now(UTC),
        )

        success = worker._fix_detached_head(temp_git_repo, result)

        assert success is False


class TestFixUpstreamTracking:
    """Test upstream tracking fixing functionality."""

    @patch("gerrit_clone.refresh_worker.subprocess.run")
    def test_fix_upstream_tracking_success(self, mock_run, worker, temp_git_repo):
        """Test successfully fixing upstream tracking."""
        # Mock rev-parse check (remote branch exists)
        mock_check = Mock()
        mock_check.returncode = 0

        # Mock set-upstream
        mock_upstream = Mock()
        mock_upstream.returncode = 0

        mock_run.side_effect = [mock_check, mock_upstream]

        result = RefreshResult(
            path=temp_git_repo,
            project_name="test-repo",
            status=RefreshStatus.PENDING,
            started_at=datetime.now(UTC),
        )
        result.current_branch = "main"

        success = worker._fix_upstream_tracking(temp_git_repo, result)

        assert success is True
        assert mock_run.call_count == 2

    @patch("gerrit_clone.refresh_worker.subprocess.run")
    def test_fix_upstream_tracking_no_current_branch(
        self, mock_run, worker, temp_git_repo
    ):
        """Test when current_branch is not set."""
        result = RefreshResult(
            path=temp_git_repo,
            project_name="test-repo",
            status=RefreshStatus.PENDING,
            started_at=datetime.now(UTC),
        )
        result.current_branch = None

        success = worker._fix_upstream_tracking(temp_git_repo, result)

        assert success is False
        assert mock_run.call_count == 0

    @patch("gerrit_clone.refresh_worker.subprocess.run")
    def test_fix_upstream_tracking_remote_branch_missing(
        self, mock_run, worker, temp_git_repo
    ):
        """Test when remote branch doesn't exist."""
        # Mock rev-parse check (remote branch doesn't exist)
        mock_check = Mock()
        mock_check.returncode = 1

        mock_run.return_value = mock_check

        result = RefreshResult(
            path=temp_git_repo,
            project_name="test-repo",
            status=RefreshStatus.PENDING,
            started_at=datetime.now(UTC),
        )
        result.current_branch = "feature-branch"

        success = worker._fix_upstream_tracking(temp_git_repo, result)

        assert success is False
        assert mock_run.call_count == 1

    @patch("gerrit_clone.refresh_worker.subprocess.run")
    def test_fix_upstream_tracking_set_upstream_fails(
        self, mock_run, worker, temp_git_repo
    ):
        """Test when set-upstream command fails."""
        # Mock rev-parse check (success)
        mock_check = Mock()
        mock_check.returncode = 0

        # Mock set-upstream (failure)
        mock_upstream = Mock()
        mock_upstream.returncode = 1
        mock_upstream.stderr = "error: branch 'main' does not exist"

        mock_run.side_effect = [mock_check, mock_upstream]

        result = RefreshResult(
            path=temp_git_repo,
            project_name="test-repo",
            status=RefreshStatus.PENDING,
            started_at=datetime.now(UTC),
        )
        result.current_branch = "main"

        success = worker._fix_upstream_tracking(temp_git_repo, result)

        assert success is False

    @patch("gerrit_clone.refresh_worker.subprocess.run")
    def test_fix_upstream_tracking_exception(self, mock_run, worker, temp_git_repo):
        """Test exception handling in fix_upstream_tracking."""
        mock_run.side_effect = Exception("Unexpected error")

        result = RefreshResult(
            path=temp_git_repo,
            project_name="test-repo",
            status=RefreshStatus.PENDING,
            started_at=datetime.now(UTC),
        )
        result.current_branch = "main"

        success = worker._fix_upstream_tracking(temp_git_repo, result)

        assert success is False


class TestTransientAndDivergedClassification:
    """Classification of transient SSH and diverged-branch failures."""

    def test_analyze_transient_ssh_is_network(self, worker):
        """Transient SSH failures are reported as retryable network errors."""
        process_result = Mock()
        process_result.returncode = 128
        process_result.stdout = ""
        process_result.stderr = "fatal: Could not read from remote repository."

        assert "Network error" in worker._analyze_git_error(process_result, "pull")

    def test_transient_ssh_is_retryable(self, worker):
        """SSH handshake throttling is retryable."""
        process_result = Mock()
        process_result.returncode = 128
        process_result.stdout = ""
        process_result.stderr = (
            "kex_exchange_identification: Connection closed by remote host"
        )

        assert worker._is_retryable_git_error(process_result) is True

    def test_auth_with_could_not_read_still_non_retryable(self, worker):
        """Real auth failures print 'could not read' too but must not retry."""
        process_result = Mock()
        process_result.returncode = 128
        process_result.stdout = ""
        process_result.stderr = (
            "Permission denied (publickey).\n"
            "fatal: Could not read from remote repository."
        )

        assert worker._is_retryable_git_error(process_result) is False
        assert "Authentication error" in worker._analyze_git_error(
            process_result, "pull"
        )

    def test_analyze_diverging_branches(self, worker):
        """Diverging branches are recognised and hint at --force-hard."""
        process_result = Mock()
        process_result.returncode = 1
        process_result.stdout = ""
        process_result.stderr = "hint: Diverging branches can't be fast-forwarded"

        msg = worker._analyze_git_error(process_result, "pull")
        assert "Diverging branches" in msg
        assert "force-hard" in msg.lower()

    def test_diverging_branches_non_retryable(self, worker):
        """Diverging branches are not retryable."""
        process_result = Mock()
        process_result.returncode = 1
        process_result.stdout = ""
        process_result.stderr = "hint: Diverging branches can't be fast-forwarded"

        assert worker._is_retryable_git_error(process_result) is False

    def test_diverging_with_transfer_stats_first_line(self, worker):
        """Transfer stats before the hint must not mask the divergence."""
        process_result = Mock()
        process_result.returncode = 1
        process_result.stdout = ""
        process_result.stderr = (
            "Total 5 (delta 3), reused 5 (delta 3)\n"
            "hint: Diverging branches can't be fast-forwarded"
        )

        assert "Diverging branches" in worker._analyze_git_error(process_result, "pull")

    def test_missing_repo_with_could_not_read_is_not_found(self, worker):
        """Missing repos print 'could not read' too but are not transient."""
        process_result = Mock()
        process_result.returncode = 128
        process_result.stdout = ""
        process_result.stderr = (
            "fatal: 'nonexistent' does not exist\n"
            "fatal: Could not read from remote repository."
        )

        assert "Repository not found" in worker._analyze_git_error(
            process_result, "fetch"
        )

    def test_missing_repo_is_not_retryable(self, worker):
        """A missing repository must not be retried as a network blip."""
        process_result = Mock()
        process_result.returncode = 128
        process_result.stdout = ""
        process_result.stderr = (
            "ERROR: Repository not found.\n"
            "fatal: Could not read from remote repository."
        )

        assert worker._is_retryable_git_error(process_result) is False


class TestPopStashSubmoduleNoise:
    """Tests for stash-pop success detection under submodule status noise."""

    def test_pop_stash_success_returns_true(self, worker, temp_git_repo):
        """A clean pop (exit 0) is reported as success."""
        (temp_git_repo / "uncommitted.txt").write_text("uncommitted")
        worker._stash_changes(temp_git_repo)

        assert worker._pop_stash(temp_git_repo) is True
        assert (temp_git_repo / "uncommitted.txt").read_text() == "uncommitted"

    @patch("gerrit_clone.refresh_worker.subprocess.run")
    def test_pop_stash_nonzero_but_dropped_is_success(self, mock_run, worker):
        """Non-zero pop that still drops the stash counts as success.

        Reproduces the submodule-gitlink case where ``git stash pop`` applies
        the working-tree changes and drops the stash entry but exits non-zero
        because of submodule status reporting.
        """
        pop = Mock(returncode=1, stdout="", stderr="error in submodule")
        before = Mock(returncode=0, stdout="stash@{0}: WIP\n", stderr="")
        after = Mock(returncode=0, stdout="", stderr="")
        # Call order: _stash_count (before), stash pop, _stash_count (after)
        mock_run.side_effect = [before, pop, after]

        assert worker._pop_stash(Path("/tmp/repo")) is True

    @patch("gerrit_clone.refresh_worker.subprocess.run")
    def test_pop_stash_nonzero_and_retained_is_failure(self, mock_run, worker):
        """Non-zero pop that leaves the stash in place is a real failure."""
        pop = Mock(returncode=1, stdout="", stderr="CONFLICT (content)")
        before = Mock(returncode=0, stdout="stash@{0}: WIP\n", stderr="")
        after = Mock(returncode=0, stdout="stash@{0}: WIP\n", stderr="")
        mock_run.side_effect = [before, pop, after]

        assert worker._pop_stash(Path("/tmp/repo")) is False


class TestForceHard:
    """Tests for force-hard behaviour and its helpers."""

    def test_force_hard_implies_force(self):
        """--force-hard is a superset of --force."""
        worker = RefreshWorker(force_hard=True)
        assert worker.force is True
        assert worker.force_hard is True

    def test_force_default_not_hard(self):
        """--force alone does not enable hard reset."""
        worker = RefreshWorker(force=True)
        assert worker.force is True
        assert worker.force_hard is False

    @patch("gerrit_clone.refresh_worker.subprocess.run")
    def test_reset_to_upstream_success(self, mock_run, worker, temp_git_repo):
        """Hard reset succeeds when an upstream exists."""
        upstream_check = Mock(returncode=0, stdout="origin/master", stderr="")
        reset = Mock(returncode=0, stdout="", stderr="")
        mock_run.side_effect = [upstream_check, reset]

        result = RefreshResult(
            path=temp_git_repo,
            project_name="test-repo",
            status=RefreshStatus.PENDING,
            started_at=datetime.now(UTC),
        )
        result.current_branch = "master"

        assert worker._reset_to_upstream(temp_git_repo, result) is True

    @patch("gerrit_clone.refresh_worker.subprocess.run")
    def test_reset_to_upstream_no_upstream(self, mock_run, worker, temp_git_repo):
        """Hard reset is skipped when there is no upstream to reset to."""
        upstream_check = Mock(returncode=128, stdout="", stderr="fatal: no upstream")
        mock_run.side_effect = [upstream_check]

        result = RefreshResult(
            path=temp_git_repo,
            project_name="test-repo",
            status=RefreshStatus.PENDING,
            started_at=datetime.now(UTC),
        )
        result.current_branch = "master"

        assert worker._reset_to_upstream(temp_git_repo, result) is False

    def test_reset_to_upstream_no_branch(self, worker, temp_git_repo):
        """Hard reset requires a current branch."""
        result = RefreshResult(
            path=temp_git_repo,
            project_name="test-repo",
            status=RefreshStatus.PENDING,
            started_at=datetime.now(UTC),
        )
        result.current_branch = None

        assert worker._reset_to_upstream(temp_git_repo, result) is False

    @patch("gerrit_clone.refresh_worker.subprocess.run")
    def test_switch_to_default_branch_success(self, mock_run, worker, temp_git_repo):
        """Switching to the default branch checks it out and sets upstream."""
        checkout = Mock(returncode=0, stdout="", stderr="")
        set_upstream = Mock(returncode=0, stdout="", stderr="")
        mock_run.side_effect = [checkout, set_upstream]

        assert worker._switch_to_default_branch(temp_git_repo, "master") is True

    @patch("gerrit_clone.refresh_worker.subprocess.run")
    def test_switch_to_default_branch_checkout_fails(
        self, mock_run, worker, temp_git_repo
    ):
        """A failed checkout is reported as failure."""
        checkout = Mock(returncode=1, stdout="", stderr="error: pathspec")
        mock_run.side_effect = [checkout]

        assert worker._switch_to_default_branch(temp_git_repo, "master") is False

    def test_force_hard_skips_reset_when_not_on_default(self, temp_git_repo):
        """Force-hard must not hard-reset a feature branch on switch failure."""
        worker = RefreshWorker(force_hard=True, filter_gerrit_only=False)
        state = {
            "branch": "feature/x",
            "detached_head": False,
            "has_uncommitted": False,
            "has_upstream": True,
            "on_meta_config": False,
        }
        with (
            patch.object(worker, "_check_repository_state", return_value=state),
            patch.object(worker, "_get_default_branch_local", return_value="main"),
            patch.object(worker, "_switch_to_default_branch", return_value=False),
            patch.object(worker, "_reset_to_upstream") as mock_reset,
            patch.object(worker, "_execute_adaptive_refresh", return_value=True),
        ):
            result = worker.refresh_repository(temp_git_repo)

        # Still parked on the feature branch, so the destructive reset that
        # would discard its local-only commits must be skipped.
        mock_reset.assert_not_called()
        assert result.hard_reset is False

    def test_force_hard_resets_on_default_branch(self, temp_git_repo):
        """Force-hard hard-resets when already on the default branch."""
        worker = RefreshWorker(force_hard=True, filter_gerrit_only=False)
        state = {
            "branch": "main",
            "detached_head": False,
            "has_uncommitted": False,
            "has_upstream": True,
            "on_meta_config": False,
        }
        with (
            patch.object(worker, "_check_repository_state", return_value=state),
            patch.object(worker, "_get_default_branch_local", return_value="main"),
            patch.object(worker, "_reset_to_upstream", return_value=True) as mock_reset,
            patch.object(worker, "_execute_adaptive_refresh", return_value=True),
        ):
            result = worker.refresh_repository(temp_git_repo)

        mock_reset.assert_called_once()
        assert result.hard_reset is True

    def test_force_preswitch_stash_clears_dirty_flag(self, temp_git_repo):
        """A successful pre-switch stash prevents a redundant second stash."""
        worker = RefreshWorker(force_hard=True, filter_gerrit_only=False)
        state = {
            "branch": "feature/x",
            "detached_head": False,
            "has_uncommitted": True,
            "has_upstream": True,
            "on_meta_config": False,
        }
        with (
            patch.object(worker, "_check_repository_state", return_value=state),
            patch.object(worker, "_get_default_branch_local", return_value="main"),
            patch.object(worker, "_switch_to_default_branch", return_value=False),
            patch.object(
                worker, "_stash_changes", return_value=StashOutcome.CREATED
            ) as mock_stash,
            patch.object(worker, "_reset_to_upstream") as mock_reset,
            patch.object(worker, "_execute_adaptive_refresh", return_value=True),
        ):
            result = worker.refresh_repository(temp_git_repo)

        # The tree is stashed once before the (failed) switch; the later
        # "always stash" block must not re-stash a now-clean tree.
        mock_stash.assert_called_once()
        mock_reset.assert_not_called()
        assert result.stash_created is True

    def test_force_stash_not_popped_on_different_branch(self, temp_git_repo):
        """A stash taken on a feature branch is not popped onto default."""
        worker = RefreshWorker(force=True, filter_gerrit_only=False)
        feature_state = {
            "branch": "feature/x",
            "detached_head": False,
            "has_uncommitted": True,
            "has_upstream": True,
            "on_meta_config": False,
        }
        main_state = {
            "branch": "main",
            "detached_head": False,
            "has_uncommitted": False,
            "has_upstream": True,
            "on_meta_config": False,
        }
        pending = [feature_state]

        def fake_state(_repo):
            return pending.pop(0) if pending else main_state

        with (
            patch.object(worker, "_check_repository_state", side_effect=fake_state),
            patch.object(worker, "_get_default_branch_local", return_value="main"),
            patch.object(worker, "_stash_changes", return_value=StashOutcome.CREATED),
            patch.object(worker, "_switch_to_default_branch", return_value=True),
            patch.object(worker, "_pop_stash") as mock_pop,
            patch.object(worker, "_execute_adaptive_refresh", return_value=True),
        ):
            result = worker.refresh_repository(temp_git_repo)

        # Stash came from feature/x but we ended on main, so it must be left
        # intact rather than popped onto the wrong branch.
        mock_pop.assert_not_called()
        assert result.stash_created is True
        assert result.stash_popped is False
        assert result.stash_branch == "feature/x"
        assert result.current_branch == "main"

    def test_force_nothing_to_stash_does_not_fail_or_pop(self, temp_git_repo):
        """A submodule-only dirty tree (nothing to stash) is benign.

        Reproduces the ci-management case: the only change is a modified
        submodule gitlink, which git stash does not capture, so no stash is
        created. Force mode must not fail, must not set stash_created, and must
        not attempt (and warn about) a pop of a non-existent stash.
        """
        worker = RefreshWorker(force=True, filter_gerrit_only=False)
        state = {
            "branch": "master",
            "detached_head": False,
            "has_uncommitted": True,
            "has_upstream": True,
            "on_meta_config": False,
        }
        with (
            patch.object(worker, "_check_repository_state", return_value=state),
            patch.object(worker, "_get_default_branch_local", return_value="master"),
            patch.object(
                worker,
                "_stash_changes",
                return_value=StashOutcome.NOTHING_TO_STASH,
            ),
            patch.object(worker, "_pop_stash") as mock_pop,
            patch.object(worker, "_execute_adaptive_refresh", return_value=True),
        ):
            result = worker.refresh_repository(temp_git_repo)

        assert result.status is not RefreshStatus.FAILED
        assert result.stash_created is False
        assert result.stash_popped is False
        mock_pop.assert_not_called()


class TestSshJitter:
    """Tests for SSH handshake jitter."""

    def test_jitter_zero_no_sleep(self):
        """Zero jitter performs no sleep."""
        worker = RefreshWorker(ssh_jitter_seconds=0)
        with patch("gerrit_clone.refresh_worker.time.sleep") as mock_sleep:
            worker._ssh_handshake_jitter(Path("/tmp/repo"))
            mock_sleep.assert_not_called()

    def test_jitter_positive_sleeps_within_bounds(self):
        """Positive jitter sleeps within [0, ssh_jitter_seconds] for SSH."""
        worker = RefreshWorker(ssh_jitter_seconds=0.1)
        with (
            patch.object(
                worker, "_get_remote_url", return_value="ssh://gerrit:29418/x"
            ),
            patch("gerrit_clone.refresh_worker.time.sleep") as mock_sleep,
        ):
            worker._ssh_handshake_jitter(Path("/tmp/repo"))
            mock_sleep.assert_called_once()
            slept = mock_sleep.call_args[0][0]
            assert 0 <= slept <= 0.1

    def test_jitter_skipped_for_http_remote(self):
        """HTTP(S) remotes perform no SSH handshake, so jitter is skipped."""
        worker = RefreshWorker(ssh_jitter_seconds=0.1)
        with (
            patch.object(
                worker,
                "_get_remote_url",
                return_value="https://github.com/org/repo.git",
            ),
            patch("gerrit_clone.refresh_worker.time.sleep") as mock_sleep,
        ):
            worker._ssh_handshake_jitter(Path("/tmp/repo"))
            mock_sleep.assert_not_called()

    def test_jitter_applied_for_scp_style_ssh_remote(self):
        """scp-style user@host:path remotes are treated as SSH."""
        worker = RefreshWorker(ssh_jitter_seconds=0.1)
        with (
            patch.object(
                worker, "_get_remote_url", return_value="git@github.com:org/repo.git"
            ),
            patch("gerrit_clone.refresh_worker.time.sleep") as mock_sleep,
        ):
            worker._ssh_handshake_jitter(Path("/tmp/repo"))
            mock_sleep.assert_called_once()

    def test_jitter_applied_for_unknown_remote(self):
        """An unreadable remote conservatively still jitters."""
        worker = RefreshWorker(ssh_jitter_seconds=0.1)
        with (
            patch.object(worker, "_get_remote_url", return_value=None),
            patch("gerrit_clone.refresh_worker.time.sleep") as mock_sleep,
        ):
            worker._ssh_handshake_jitter(Path("/tmp/repo"))
            mock_sleep.assert_called_once()

    def test_jitter_skipped_for_file_and_local_remotes(self):
        """file:// URLs and local paths perform no SSH handshake."""
        worker = RefreshWorker(ssh_jitter_seconds=0.1)
        for url in ("file:///srv/git/repo.git", "/srv/git/repo.git", "../peer"):
            with (
                patch.object(worker, "_get_remote_url", return_value=url),
                patch("gerrit_clone.refresh_worker.time.sleep") as mock_sleep,
            ):
                worker._ssh_handshake_jitter(Path("/tmp/repo"))
                mock_sleep.assert_not_called()

    def test_remote_uses_ssh_classification(self):
        """Direct classification of remote URL transports."""
        ssh = RefreshWorker._remote_uses_ssh
        assert ssh("ssh://gerrit.example.org:29418/proj") is True
        assert ssh("git@github.com:org/repo.git") is True
        assert ssh(None) is True  # unknown -> conservative
        assert ssh("https://github.com/org/repo.git") is False
        assert ssh("http://example.org/repo.git") is False
        assert ssh("git://example.org/repo.git") is False
        assert ssh("file:///srv/git/repo.git") is False
        assert ssh("/srv/git/repo.git") is False
        assert ssh("./peer") is False
        assert ssh("~/repos/peer") is False

    @patch("gerrit_clone.refresh_worker.subprocess.run")
    def test_get_default_branch_applies_jitter(self, mock_run, temp_git_repo):
        """Networked default-branch lookup is preceded by SSH jitter."""
        worker = RefreshWorker(ssh_jitter_seconds=0.1)
        mock_run.return_value = Mock(
            returncode=0,
            stdout="ref: refs/heads/main\tHEAD\n",
            stderr="",
        )
        with patch.object(worker, "_ssh_handshake_jitter") as mock_jitter:
            branch = worker._get_default_branch(temp_git_repo)

        mock_jitter.assert_called_once()
        assert branch == "main"

    @patch("gerrit_clone.refresh_worker.subprocess.run")
    def test_is_meta_only_repo_applies_jitter(self, mock_run, temp_git_repo):
        """The networked meta-only check is preceded by SSH jitter."""
        worker = RefreshWorker(ssh_jitter_seconds=0.1)
        mock_run.return_value = Mock(
            returncode=0,
            stdout="abc123\trefs/heads/main\n",
            stderr="",
        )
        with patch.object(worker, "_ssh_handshake_jitter") as mock_jitter:
            worker._is_meta_only_repo(temp_git_repo)

        mock_jitter.assert_called()

    @patch("gerrit_clone.refresh_worker.subprocess.run")
    def test_fix_detached_head_applies_jitter(self, mock_run, temp_git_repo):
        """The networked detached-HEAD fetch is preceded by SSH jitter."""
        worker = RefreshWorker(ssh_jitter_seconds=0.1)
        mock_run.return_value = Mock(returncode=0, stdout="", stderr="")
        result = RefreshResult(
            path=temp_git_repo,
            project_name="test-repo",
            status=RefreshStatus.PENDING,
            started_at=datetime.now(UTC),
        )
        with (
            patch.object(worker, "_is_on_meta_config", return_value=False),
            patch.object(worker, "_get_default_branch", return_value="main"),
            patch.object(worker, "_ssh_handshake_jitter") as mock_jitter,
        ):
            worker._fix_detached_head(temp_git_repo, result)

        mock_jitter.assert_called()
