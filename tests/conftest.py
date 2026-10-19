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
import ipaddress
import os
import shutil
import socket
import subprocess
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar, NoReturn

import pytest

from source_clone.discovery import GerritDiscoveryError

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
        # Git hook environment variables (set by git when running hooks,
        # e.g. pre-commit). These MUST be unset or they cause test git
        # operations to target the real repository instead of temp repos.
        "GIT_DIR",
        "GIT_INDEX_FILE",
        "GIT_WORK_TREE",
        "SSH_AUTH_SOCK",
        "SSH_AGENT_PID",
        "GPG_AGENT_INFO",
        "GNUPGHOME",
    ]

    # Variables that MUST be removed from the environment entirely.
    # When running under a git hook (e.g. pre-commit), git sets GIT_DIR,
    # GIT_INDEX_FILE, etc. pointing to the real repository. If these leak
    # into test subprocess calls, git commands in temp repos silently
    # operate against the real repo — causing commits to fail (pre-commit
    # hook fires in temp dir with no config) and git status to report the
    # real repo's state instead of the test repo's.
    git_hook_vars = [
        "GIT_DIR",
        "GIT_INDEX_FILE",
        "GIT_WORK_TREE",
    ]

    # Store original environment for potential restoration
    for var in git_env_vars:
        _GitIsolationState.original_git_env[var] = os.environ.get(var)

    # Remove git hook variables BEFORE any git operations occur.
    # This is critical when pytest is invoked via a pre-commit hook.
    for var in git_hook_vars:
        os.environ.pop(var, None)

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

        # Remove git hook variables that leak from pre-commit into tests.
        # When pytest runs via a pre-commit hook, git sets GIT_DIR etc.
        # pointing to the real repo. This causes test git operations in
        # temp directories to silently target the real repo instead.
        monkeypatch.delenv("GIT_DIR", raising=False)
        monkeypatch.delenv("GIT_INDEX_FILE", raising=False)
        monkeypatch.delenv("GIT_WORK_TREE", raising=False)

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
# LAYER 3b: No network for unit tests
# =============================================================================


class NetworkUseError(RuntimeError):
    """A unit test tried to reach the network."""


def _is_local_host(host: Any) -> bool:
    """Whether *host* names this machine: loopback, or a wildcard to bind.

    Checked as an address, never by prefix: ``127.0.0.1.example.org`` is a
    name anywhere on the network.
    """
    if isinstance(host, bytes):
        host = host.decode(errors="replace")
    if host in (None, "", "localhost"):
        return True
    try:
        # The whole string: ipaddress takes a scoped IPv6 address as is, and
        # anything it rejects -- 127.0.0.1%example.org -- is a name.
        address = ipaddress.ip_address(str(host))
    except ValueError:
        return False
    return address.is_loopback or address.is_unspecified


def _is_local(address: Any) -> bool:
    """Whether a socket *address* stays on this machine.

    A Unix socket path, or a host :func:`_is_local_host` accepts.
    """
    if isinstance(address, (str, bytes)):
        return True
    return _is_local_host(
        address[0] if isinstance(address, tuple) and address else None
    )


@pytest.fixture(autouse=True)
def no_network(
    request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch
) -> Generator[list[str], None, None]:
    """Keep unit tests off the network, and fail any that try to reach it.

    Building a Gerrit ``Config`` discovers its API base URL over HTTPS,
    trying several paths against the host.  Where DNS fails fast that
    costs nothing; on a runner that drops connections instead, every
    ``Config`` waits out a connect timeout per path, and the suite runs
    past the job's time limit.  Discovery therefore fails at once here,
    and ``Config`` falls back to ``https://<host>`` as it does whenever
    discovery fails.

    Anything else that resolves a name or connects off this machine
    raises, and is recorded so the test fails even if the code under
    test swallows the error.  Integration tests keep the network.
    """
    attempts: list[str] = []
    if _is_integration_test(request.node):
        yield attempts
        return

    def refuse(what: str) -> NoReturn:
        attempts.append(what)
        raise NetworkUseError(f"Unit test tried to reach the network: {what}")

    def offline_discovery(host: str, timeout: float = 30.0) -> str:
        raise GerritDiscoveryError(f"No API discovery in unit tests ({host})")

    def guarded_lookup(name: str, real: Any) -> Any:
        def lookup(host: Any, *args: Any, **kwargs: Any) -> Any:
            if not _is_local_host(host):
                refuse(f"{name} {host}")
            return real(host, *args, **kwargs)

        return lookup

    # Reverse lookups are answered here even for this machine: the system
    # resolver may ask DNS about a loopback address that no hosts entry
    # names, below Python's socket layer, where nothing would record it.

    def offline_gethostbyaddr(host: Any) -> tuple[str, list[str], list[str]]:
        if not _is_local_host(host):
            refuse(f"gethostbyaddr {host}")
        return "localhost", [], [str(host or "127.0.0.1")]

    def offline_getnameinfo(address: Any, flags: int) -> tuple[str, str]:
        # Numbers only from the real call, so it never consults a resolver;
        # a requested name is this machine's, and the port stays numeric.
        if not _is_local(address):
            refuse(f"getnameinfo {address}")
        numeric = socket.NI_NUMERICHOST | socket.NI_NUMERICSERV
        host, port = real_getnameinfo(address, numeric)
        if not flags & socket.NI_NUMERICHOST:
            host = "localhost"
        return host, port

    def guarded_sendto(sock: socket.socket, data: Any, *rest: Any) -> Any:
        # sendto(data, address) or sendto(data, flags, address): no connect,
        # and no lookup for an address given as numbers.
        if rest and not _is_local(rest[-1]):
            refuse(f"sendto {rest[-1]}")
        return real_sendto(sock, data, *rest)

    def guarded_sendmsg(sock: socket.socket, *args: Any) -> Any:
        # sendmsg(buffers, ancdata, flags, address): a destination only when
        # all four are given.
        if len(args) >= 4 and args[3] is not None and not _is_local(args[3]):
            refuse(f"sendmsg {args[3]}")
        assert real_sendmsg is not None  # Installed only where it exists.
        return real_sendmsg(sock, *args)

    def guarded_bind(sock: socket.socket, address: Any) -> Any:
        # A host name to bind to is resolved natively, past the lookups.
        if not _is_local(address):
            refuse(f"bind {address}")
        return real_bind(sock, address)

    def guarded_connect(sock: socket.socket, address: Any) -> Any:
        if not _is_local(address):
            refuse(f"connect {address}")
        return real_connect(sock, address)

    def guarded_connect_ex(sock: socket.socket, address: Any) -> Any:
        if not _is_local(address):
            refuse(f"connect {address}")
        return real_connect_ex(sock, address)

    real_getnameinfo = socket.getnameinfo
    real_sendto = socket.socket.sendto
    real_bind = socket.socket.bind
    # Not on every platform: Windows has no sendmsg.
    real_sendmsg = getattr(socket.socket, "sendmsg", None)
    real_connect = socket.socket.connect
    real_connect_ex = socket.socket.connect_ex
    monkeypatch.setattr(
        "source_clone.discovery.discover_gerrit_base_url", offline_discovery
    )
    # Each resolver entry point, not getaddrinfo alone: the others call the
    # system resolver themselves.  The reverse lookups are answered above.
    for name in ("getaddrinfo", "gethostbyname", "gethostbyname_ex"):
        monkeypatch.setattr(socket, name, guarded_lookup(name, getattr(socket, name)))
    monkeypatch.setattr(socket, "gethostbyaddr", offline_gethostbyaddr)
    monkeypatch.setattr(socket, "getnameinfo", offline_getnameinfo)
    monkeypatch.setattr(socket.socket, "bind", guarded_bind)
    monkeypatch.setattr(socket.socket, "connect", guarded_connect)
    monkeypatch.setattr(socket.socket, "sendto", guarded_sendto)
    if real_sendmsg is not None:
        monkeypatch.setattr(socket.socket, "sendmsg", guarded_sendmsg)
    monkeypatch.setattr(socket.socket, "connect_ex", guarded_connect_ex)
    yield attempts
    if attempts:
        pytest.fail(
            f"Unit test tried to reach the network: {', '.join(attempts)}",
            pytrace=False,
        )


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
    # Remove git hook variables that leak from pre-commit into tests.
    # When pytest runs via a pre-commit hook, git sets these pointing to
    # the real repo, causing test git operations to silently target it.
    env.pop("GIT_DIR", None)
    env.pop("GIT_INDEX_FILE", None)
    env.pop("GIT_WORK_TREE", None)
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
