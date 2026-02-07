# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2025 The Linux Foundation

"""Pytest configuration and fixtures for bulletproof git environment isolation.

This module provides fixtures and hooks that GUARANTEE complete isolation for
git-related tests, preventing environment pollution regardless of:
- Local user git configuration (including GPG/SSH signing)
- Test execution order
- Pre-commit vs direct pytest invocation
- CI vs local execution
- Any other environmental factors

The isolation is achieved through multiple layers:
1. pytest_configure hook sets git CONFIG isolation (signing disabled) at process level
   NOTE: SSH/GPG agent blocking is NOT done here to allow integration tests to work
2. Session-scoped fixture ensures consistent environment throughout test run
3. Function-scoped autouse fixture provides per-test isolation including SSH blocking
   for unit tests, while relaxing isolation for integration tests
4. Helper utilities for git operations with guaranteed isolation

Integration Test Behavior:
- Integration tests (marked with @pytest.mark.integration or in tests/integration/)
  are NOT subject to SSH agent blocking, allowing them to use real SSH credentials
- They still get git signing disabled to prevent GPG/SSH signing issues
"""

from __future__ import annotations

import contextlib
import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar

import pytest

if TYPE_CHECKING:
    from collections.abc import Generator


# =============================================================================
# LAYER 1: Process-level environment setup (runs before test collection)
# =============================================================================


class _GitIsolationState:
    """Container for git isolation session state.

    Using a class avoids global statements and dynamic attributes on pytest.Config,
    which keeps both mypy and ruff happy.
    """

    original_git_env: ClassVar[dict[str, str | None]] = {}
    isolation_tmpdir: ClassVar[str | None] = None


def pytest_configure(config: pytest.Config) -> None:
    """Set git isolation environment variables before ANY tests are collected.

    This hook runs at the very start of the pytest session, before test
    collection, ensuring that even fixture setup code runs in an isolated
    git environment.
    """
    # Environment variables that affect git behavior
    git_env_vars = [
        "HOME",
        "GIT_CONFIG",
        "GIT_CONFIG_GLOBAL",
        "GIT_CONFIG_SYSTEM",
        "GIT_CONFIG_NOSYSTEM",
        "GIT_AUTHOR_NAME",
        "GIT_AUTHOR_EMAIL",
        "GIT_COMMITTER_NAME",
        "GIT_COMMITTER_EMAIL",
        "GIT_SSH_COMMAND",
        "SSH_AUTH_SOCK",
        "SSH_AGENT_PID",
        "GPG_AGENT_INFO",
        "GNUPGHOME",
    ]

    # Store original environment for potential restoration
    for var in git_env_vars:
        _GitIsolationState.original_git_env[var] = os.environ.get(var)

    # Create a temporary directory for isolated git config
    # This will persist for the entire pytest session
    _GitIsolationState.isolation_tmpdir = tempfile.mkdtemp(
        prefix="pytest_git_isolation_"
    )
    isolation_home = Path(_GitIsolationState.isolation_tmpdir)

    # Create isolated .gitconfig with safe defaults
    gitconfig = isolation_home / ".gitconfig"
    gitconfig.write_text(
        """[user]
    name = Test User
    email = test@example.com
[init]
    defaultBranch = main
[commit]
    gpgsign = false
[tag]
    gpgsign = false
[gpg]
    program = /bin/false
[core]
    autocrlf = false
    hooksPath = /dev/null
[advice]
    detachedHead = false
[safe]
    directory = *
"""
    )

    # Create empty SSH directory
    ssh_dir = isolation_home / ".ssh"
    ssh_dir.mkdir(mode=0o700)
    (ssh_dir / "known_hosts").touch()

    # Set environment variables for git CONFIG isolation only
    # These affect ALL git operations in the test process
    # NOTE: We do NOT block SSH agent here - that's done per-test in ensure_git_isolation
    # so that integration tests can still use real SSH credentials
    os.environ["GIT_CONFIG_NOSYSTEM"] = "1"
    os.environ["GIT_CONFIG_GLOBAL"] = str(gitconfig)
    os.environ["GIT_AUTHOR_NAME"] = "Test User"
    os.environ["GIT_AUTHOR_EMAIL"] = "test@example.com"
    os.environ["GIT_COMMITTER_NAME"] = "Test User"
    os.environ["GIT_COMMITTER_EMAIL"] = "test@example.com"
    # Set GNUPGHOME to isolated directory to prevent GPG signing attempts
    os.environ["GNUPGHOME"] = str(isolation_home / ".gnupg")


def pytest_unconfigure(config: pytest.Config) -> None:
    """Clean up temporary directory and restore environment after test session."""
    # Clean up temporary directory
    if _GitIsolationState.isolation_tmpdir is not None:
        with contextlib.suppress(Exception):
            shutil.rmtree(_GitIsolationState.isolation_tmpdir)
        _GitIsolationState.isolation_tmpdir = None

    # Restore original environment
    for var, value in _GitIsolationState.original_git_env.items():
        if value is None:
            os.environ.pop(var, None)
        else:
            os.environ[var] = value
    _GitIsolationState.original_git_env = {}


# =============================================================================
# LAYER 2: Session-scoped fixture for consistent environment
# =============================================================================


@pytest.fixture(scope="session")
def git_isolation_dir(request: pytest.FixtureRequest) -> Generator[Path, None, None]:
    """Session-scoped fixture providing the isolation directory path.

    This fixture provides access to the isolation directory created in
    pytest_configure, ensuring all tests in the session use the same
    isolated git configuration.
    """
    if _GitIsolationState.isolation_tmpdir is not None:
        yield Path(_GitIsolationState.isolation_tmpdir)
    else:
        # Fallback if pytest_configure didn't run (shouldn't happen)
        with tempfile.TemporaryDirectory(prefix="pytest_git_fallback_") as tmpdir:
            yield Path(tmpdir)


# =============================================================================
# LAYER 3: Function-scoped autouse fixture for per-test guarantees
# =============================================================================


def _is_integration_test(item: Any) -> bool:
    """Check if a test item is an integration test."""
    if item.get_closest_marker("integration"):
        return True
    return "integration" in str(getattr(item, "fspath", ""))


@pytest.fixture(autouse=True)
def ensure_git_isolation(
    request: pytest.FixtureRequest,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> Generator[dict[str, Any], None, None]:
    """Ensure complete git environment isolation for EVERY test.

    This autouse fixture runs for all tests and provides multiple layers
    of protection:

    1. Verifies process-level environment variables are set
    2. Sets additional per-test environment variables via monkeypatch
    3. Provides isolated HOME for test-specific operations
    4. Returns isolation info for tests that need to customize behavior

    For integration tests, some isolation is relaxed to allow network access.
    """
    # Check if this is an integration test
    is_integration = _is_integration_test(request.node)

    # Create per-test isolation directory
    test_home = tmp_path / "home"
    test_home.mkdir()

    # Create per-test gitconfig with safe defaults
    gitconfig = test_home / ".gitconfig"
    gitconfig.write_text(
        """[user]
    name = Test User
    email = test@example.com
[init]
    defaultBranch = main
[commit]
    gpgsign = false
[tag]
    gpgsign = false
[gpg]
    program = /bin/false
[core]
    autocrlf = false
    hooksPath = /dev/null
[advice]
    detachedHead = false
[safe]
    directory = *
"""
    )

    # Create SSH directory
    ssh_dir = test_home / ".ssh"
    ssh_dir.mkdir(mode=0o700)
    (ssh_dir / "known_hosts").touch()

    if not is_integration:
        # Full isolation for unit tests
        monkeypatch.setenv("HOME", str(test_home))
        monkeypatch.setenv("USERPROFILE", str(test_home))  # Windows
        monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
        monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(gitconfig))
        monkeypatch.setenv("GIT_AUTHOR_NAME", "Test User")
        monkeypatch.setenv("GIT_AUTHOR_EMAIL", "test@example.com")
        monkeypatch.setenv("GIT_COMMITTER_NAME", "Test User")
        monkeypatch.setenv("GIT_COMMITTER_EMAIL", "test@example.com")
        monkeypatch.setenv(
            "GIT_SSH_COMMAND",
            "ssh -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null "
            "-o IdentitiesOnly=yes -o IdentityFile=/dev/null -o BatchMode=yes",
        )
        monkeypatch.setenv("SSH_AUTH_SOCK", "")
        monkeypatch.delenv("SSH_AGENT_PID", raising=False)
        monkeypatch.delenv("GPG_AGENT_INFO", raising=False)
        monkeypatch.setenv("GNUPGHOME", str(test_home / ".gnupg"))

        # XDG directories for complete isolation
        xdg_config = test_home / ".config"
        xdg_config.mkdir()
        monkeypatch.setenv("XDG_CONFIG_HOME", str(xdg_config))
        monkeypatch.setenv("XDG_DATA_HOME", str(test_home / ".local" / "share"))
        monkeypatch.setenv("XDG_CACHE_HOME", str(test_home / ".cache"))

    yield {
        "home": test_home,
        "gitconfig": gitconfig,
        "ssh_dir": ssh_dir,
        "is_integration": is_integration,
    }


# =============================================================================
# LAYER 4: Helper utilities for git operations with guaranteed isolation
# =============================================================================


def get_isolated_git_env() -> dict[str, str]:
    """Get environment dictionary for isolated git operations.

    Use this when you need to pass an explicit environment to subprocess.run()
    to guarantee git isolation. This is useful for tests that need to run
    git commands with specific environment overrides.

    Returns:
        Dictionary of environment variables for isolated git operations.
    """
    env = os.environ.copy()
    env.update(
        {
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_AUTHOR_NAME": "Test User",
            "GIT_AUTHOR_EMAIL": "test@example.com",
            "GIT_COMMITTER_NAME": "Test User",
            "GIT_COMMITTER_EMAIL": "test@example.com",
            "SSH_AUTH_SOCK": "",
        }
    )
    # Remove problematic variables
    env.pop("SSH_AGENT_PID", None)
    env.pop("GPG_AGENT_INFO", None)
    return env


def run_git(
    args: list[str],
    cwd: Path | str | None = None,
    check: bool = True,
    capture_output: bool = True,
    **kwargs: Any,
) -> subprocess.CompletedProcess[str]:
    """Run a git command with guaranteed environment isolation.

    This helper function wraps subprocess.run() and ensures that:
    1. GPG/SSH signing is disabled via -c options
    2. Environment variables are properly set
    3. The command runs in an isolated context

    Args:
        args: Git command arguments (e.g., ["init", "-b", "main"])
        cwd: Working directory for the command
        check: If True, raise CalledProcessError on non-zero exit
        capture_output: If True, capture stdout and stderr
        **kwargs: Additional arguments to subprocess.run()

    Returns:
        CompletedProcess instance with the command result.

    Example:
        >>> run_git(["init", "-b", "main"], cwd=repo_path)
        >>> run_git(["commit", "-m", "Test"], cwd=repo_path)
    """
    # Build the command with config overrides
    cmd = [
        "git",
        "-c",
        "commit.gpgsign=false",
        "-c",
        "tag.gpgsign=false",
        "-c",
        "gpg.program=/bin/false",
        "-c",
        "user.name=Test User",
        "-c",
        "user.email=test@example.com",
        "-c",
        "init.defaultBranch=main",
        "-c",
        "core.hooksPath=/dev/null",
        *args,
    ]

    # Get isolated environment
    env = kwargs.pop("env", None)
    if env is None:
        env = get_isolated_git_env()
    else:
        # Merge with isolated env, user env takes precedence
        isolated = get_isolated_git_env()
        isolated.update(env)
        env = isolated

    return subprocess.run(
        cmd,
        cwd=cwd,
        check=check,
        capture_output=capture_output,
        text=True,
        env=env,
        **kwargs,
    )


def create_test_repo(
    base_path: Path,
    name: str = "test-repo",
    with_commit: bool = True,
    with_remote: bool = False,
    remote_url: str = "https://github.com/test/test-repo.git",
) -> Path:
    """Create an isolated test git repository with guaranteed clean state.

    This helper creates a git repository with all the necessary configuration
    for isolated testing, including:
    - Proper user configuration
    - GPG signing disabled
    - Optional initial commit
    - Optional remote configuration

    Args:
        base_path: Parent directory for the repository
        name: Name of the repository directory
        with_commit: If True, create an initial commit
        with_remote: If True, add a remote
        remote_url: URL for the remote (if with_remote is True)

    Returns:
        Path to the created repository.
    """
    repo_path = base_path / name
    repo_path.mkdir(parents=True, exist_ok=True)

    # Initialize repository
    run_git(["init", "-b", "main"], cwd=repo_path)

    # Set local config (belt and suspenders)
    run_git(["config", "user.email", "test@example.com"], cwd=repo_path)
    run_git(["config", "user.name", "Test User"], cwd=repo_path)
    run_git(["config", "commit.gpgsign", "false"], cwd=repo_path)
    run_git(["config", "tag.gpgsign", "false"], cwd=repo_path)
    run_git(["config", "core.hooksPath", "/dev/null"], cwd=repo_path)

    if with_commit:
        readme = repo_path / "README.md"
        readme.write_text("# Test Repository\n")
        run_git(["add", "README.md"], cwd=repo_path)
        run_git(["commit", "-m", "Initial commit"], cwd=repo_path)

    if with_remote:
        run_git(["remote", "add", "origin", remote_url], cwd=repo_path)

    return repo_path


# =============================================================================
# LAYER 5: Reusable fixtures for common test patterns
# =============================================================================


@pytest.fixture
def git_repo(tmp_path: Path, ensure_git_isolation: dict[str, Any]) -> Path:
    """Create an isolated git repository for testing.

    Returns the path to an initialized git repository with:
    - Proper user configuration
    - An initial commit
    - GPG signing disabled
    """
    return create_test_repo(tmp_path, name="test-repo", with_commit=True)


@pytest.fixture
def bare_git_repo(tmp_path: Path, ensure_git_isolation: dict[str, Any]) -> Path:
    """Create an isolated bare git repository for testing.

    Returns the path to an initialized bare git repository.
    """
    repo_path = tmp_path / "bare-repo.git"
    repo_path.mkdir()

    run_git(["init", "--bare"], cwd=repo_path)

    return repo_path


@pytest.fixture
def git_repo_with_remote(
    tmp_path: Path,
    ensure_git_isolation: dict[str, Any],
) -> tuple[Path, Path]:
    """Create a git repository with a remote pointing to a bare repo.

    Returns a tuple of (repo_path, remote_path).
    """
    # Create bare remote first
    bare_path = tmp_path / "remote.git"
    bare_path.mkdir()
    run_git(["init", "--bare"], cwd=bare_path)

    # Create working repo
    repo_path = create_test_repo(tmp_path, name="working-repo", with_commit=True)

    # Add remote and push
    run_git(["remote", "add", "origin", str(bare_path)], cwd=repo_path)
    run_git(["push", "-u", "origin", "main"], cwd=repo_path)

    return repo_path, bare_path
