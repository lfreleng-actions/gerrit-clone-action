# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2025 Matthew Watkins <mwatkins@linuxfoundation.org>

"""Unit tests for refresh worker."""

from __future__ import annotations

import subprocess
from datetime import UTC, datetime
from unittest.mock import Mock, patch

import pytest

from gerrit_clone.models import Config, RefreshResult, RefreshStatus, RetryPolicy
from gerrit_clone.refresh_worker import RefreshTimeoutError, RefreshWorker


@pytest.fixture
def worker():
    """Create a refresh worker for testing."""
    return RefreshWorker(
        retry_policy=RetryPolicy(max_attempts=2, base_delay=0.1),
        timeout=10,
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
        ["git", "commit", "-m", "Initial commit"],
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

        success = worker._stash_changes(temp_git_repo)
        assert success is True

        # Verify working directory is clean
        result = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=temp_git_repo,
            capture_output=True,
            text=True,
            check=True,
        )
        assert result.stdout.strip() == ""

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
