# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2025 Matthew Watkins <mwatkins@linuxfoundation.org>

"""Unit tests for clone_utils module."""

from __future__ import annotations

from pathlib import Path

from gerrit_clone.clone_utils import (
    analyze_git_clone_error,
    build_base_clone_command,
    is_retryable_git_error,
    should_cleanup_on_clone_error,
)
from gerrit_clone.models import Config


class TestBuildBaseCloneCommand:
    """Test build_base_clone_command function."""

    def test_basic_clone_with_mirror(self) -> None:
        """Test basic clone command with mirror mode (default)."""
        config = Config(host="gerrit.example.org")
        clone_url = "ssh://gerrit.example.org:29418/test-project"
        target_path = Path("/tmp/repos/test-project")

        cmd = build_base_clone_command(clone_url, target_path, config)

        assert cmd == [
            "git",
            "clone",
            "--no-hardlinks",
            "--quiet",
            "--mirror",
            clone_url,
            str(target_path),
        ]

    def test_clone_without_mirror(self) -> None:
        """Test clone command without mirror mode."""
        config = Config(host="gerrit.example.org", mirror=False)
        clone_url = "ssh://gerrit.example.org:29418/test-project"
        target_path = Path("/tmp/repos/test-project")

        cmd = build_base_clone_command(clone_url, target_path, config)

        assert cmd == [
            "git",
            "clone",
            "--no-hardlinks",
            "--quiet",
            clone_url,
            str(target_path),
        ]
        assert "--mirror" not in cmd

    def test_clone_with_depth(self) -> None:
        """Test clone command with shallow clone depth."""
        config = Config(host="gerrit.example.org", mirror=False, depth=1)
        clone_url = "https://github.com/org/repo.git"
        target_path = Path("/tmp/repos/repo")

        cmd = build_base_clone_command(clone_url, target_path, config)

        assert "--depth" in cmd
        assert "1" in cmd
        assert "--mirror" not in cmd

    def test_clone_with_branch(self) -> None:
        """Test clone command with specific branch."""
        config = Config(host="gerrit.example.org", mirror=False, branch="develop")
        clone_url = "https://github.com/org/repo.git"
        target_path = Path("/tmp/repos/repo")

        cmd = build_base_clone_command(clone_url, target_path, config)

        assert "--branch" in cmd
        assert "develop" in cmd
        assert "--mirror" not in cmd

    def test_clone_with_depth_and_branch(self) -> None:
        """Test clone command with both depth and branch."""
        config = Config(
            host="gerrit.example.org", mirror=False, depth=5, branch="feature/test"
        )
        clone_url = "https://github.com/org/repo.git"
        target_path = Path("/tmp/repos/repo")

        cmd = build_base_clone_command(clone_url, target_path, config)

        assert "--depth" in cmd
        assert "5" in cmd
        assert "--branch" in cmd
        assert "feature/test" in cmd

    def test_mirror_ignores_depth_and_branch(self) -> None:
        """Test that mirror mode ignores depth and branch settings."""
        config = Config(host="gerrit.example.org", mirror=True, depth=1, branch="main")
        clone_url = "ssh://gerrit.example.org:29418/project"
        target_path = Path("/tmp/repos/project")

        cmd = build_base_clone_command(clone_url, target_path, config)

        assert "--mirror" in cmd
        assert "--depth" not in cmd
        assert "--branch" not in cmd


class TestIsRetryableGitError:
    """Test is_retryable_git_error function."""

    def test_connection_timeout_retryable(self) -> None:
        """Test connection timeout is retryable."""
        assert is_retryable_git_error("Connection timeout") is True
        assert is_retryable_git_error("connection timed out") is True

    def test_connection_refused_retryable(self) -> None:
        """Test connection refused is retryable."""
        assert (
            is_retryable_git_error("ssh: connect to host: Connection refused") is True
        )
        assert is_retryable_git_error("Connection refused") is True

    def test_network_unreachable_retryable(self) -> None:
        """Test network unreachable is retryable."""
        assert is_retryable_git_error("Network is unreachable") is True

    def test_dns_failure_retryable(self) -> None:
        """Test DNS failures are retryable."""
        assert is_retryable_git_error("Temporary failure in name resolution") is True
        assert is_retryable_git_error("Could not resolve hostname") is True

    def test_git_protocol_errors_retryable(self) -> None:
        """Test git protocol errors are retryable."""
        assert is_retryable_git_error("early EOF") is True
        assert is_retryable_git_error("The remote end hung up unexpectedly") is True
        assert is_retryable_git_error("transfer closed") is True
        assert is_retryable_git_error("RPC failed") is True

    def test_server_errors_retryable(self) -> None:
        """Test HTTP server errors are retryable."""
        assert is_retryable_git_error("502 Bad Gateway") is True
        assert is_retryable_git_error("503 Service Unavailable") is True
        assert is_retryable_git_error("504 Gateway Timeout") is True

    def test_permission_denied_not_retryable(self) -> None:
        """Test permission denied is not retryable."""
        assert is_retryable_git_error("Permission denied (publickey)") is False
        assert is_retryable_git_error("Access denied") is False

    def test_authentication_failed_not_retryable(self) -> None:
        """Test authentication failures are not retryable."""
        assert is_retryable_git_error("Authentication failed") is False
        assert is_retryable_git_error("Invalid credentials") is False
        assert is_retryable_git_error("Bad credentials") is False

    def test_repository_not_found_not_retryable(self) -> None:
        """Test repository not found is not retryable."""
        assert is_retryable_git_error("Repository not found") is False
        assert (
            is_retryable_git_error("fatal: repository 'test' does not exist") is False
        )

    def test_host_key_verification_not_retryable(self) -> None:
        """Test host key verification is not retryable."""
        assert is_retryable_git_error("Host key verification failed") is False

    def test_empty_error_not_retryable(self) -> None:
        """Test empty error is not retryable."""
        assert is_retryable_git_error("") is False
        assert is_retryable_git_error(None) is False  # type: ignore

    def test_unknown_error_not_retryable(self) -> None:
        """Test unknown errors default to non-retryable."""
        assert is_retryable_git_error("Some unknown error occurred") is False


class TestAnalyzeGitCloneError:
    """Test analyze_git_clone_error function."""

    def test_ssh_permission_denied(self) -> None:
        """Test SSH permission denied analysis."""
        error = "Permission denied (publickey)."
        result = analyze_git_clone_error(error, "test-project", "gerrit.example.org")

        assert "SSH authentication failed" in result
        assert "test-project" in result
        assert "ssh-add" in result
        assert "ssh -T gerrit.example.org" in result

    def test_host_key_verification_failed(self) -> None:
        """Test host key verification analysis."""
        error = "Host key verification failed."
        result = analyze_git_clone_error(error, "test-project", "gerrit.example.org")

        assert "host key verification failed" in result.lower()
        assert "known_hosts" in result
        assert "ssh-keyscan gerrit.example.org" in result

    def test_connection_refused(self) -> None:
        """Test connection refused analysis."""
        error = "ssh: connect to host gerrit.example.org port 29418: Connection refused"
        result = analyze_git_clone_error(error, "test-project", "gerrit.example.org")

        assert "Connection refused" in result
        assert "SSH service" in result
        assert "29418" in result
        assert "nmap" in result

    def test_dns_resolution_failure(self) -> None:
        """Test DNS resolution failure analysis."""
        error = "ssh: Could not resolve hostname invalid.example.org"
        result = analyze_git_clone_error(error, "test-project", "invalid.example.org")

        assert "DNS resolution failed" in result
        assert "nslookup invalid.example.org" in result

    def test_repository_not_found(self) -> None:
        """Test repository not found analysis."""
        error = "fatal: repository 'nonexistent' does not exist"
        result = analyze_git_clone_error(error, "nonexistent")

        assert "Repository not found" in result
        assert "nonexistent" in result
        assert "incorrect" in result or "deleted" in result or "permission" in result

    def test_timeout_error(self) -> None:
        """Test timeout error analysis."""
        error = "fatal: The remote end hung up unexpectedly (timeout)"
        result = analyze_git_clone_error(error, "large-project")

        assert "timeout" in result.lower()
        assert "clone_timeout" in result

    def test_generic_network_error(self) -> None:
        """Test generic network error analysis."""
        error = (
            "fatal: unable to access 'https://github.com/org/repo.git/': Network error"
        )
        result = analyze_git_clone_error(error, "test-project")

        assert "Network error" in result
        assert "test-project" in result

    def test_empty_error(self) -> None:
        """Test empty error handling."""
        result = analyze_git_clone_error("", "test-project")
        assert "no error output" in result.lower()

    def test_unknown_error_returns_original(self) -> None:
        """Test unknown errors return original message."""
        error = "Some completely unknown error message"
        result = analyze_git_clone_error(error, "test-project")
        assert error in result


class TestShouldCleanupOnCloneError:
    """Test should_cleanup_on_clone_error function."""

    def test_cleanup_on_network_errors(self) -> None:
        """Test cleanup on network errors."""
        assert should_cleanup_on_clone_error("Connection timeout") is True
        assert should_cleanup_on_clone_error("Network is unreachable") is True
        assert should_cleanup_on_clone_error("early EOF") is True

    def test_cleanup_on_timeout(self) -> None:
        """Test cleanup on timeout errors."""
        assert should_cleanup_on_clone_error("Clone timed out after 600s") is True

    def test_no_cleanup_on_permission_denied(self) -> None:
        """Test no cleanup on permission denied (leave for inspection)."""
        assert should_cleanup_on_clone_error("Permission denied (publickey)") is False

    def test_no_cleanup_on_authentication_failed(self) -> None:
        """Test no cleanup on authentication failures."""
        assert should_cleanup_on_clone_error("Authentication failed") is False
        assert should_cleanup_on_clone_error("Access denied") is False

    def test_no_cleanup_on_host_key_verification(self) -> None:
        """Test no cleanup on host key verification failure."""
        assert should_cleanup_on_clone_error("Host key verification failed") is False

    def test_cleanup_on_empty_error(self) -> None:
        """Test cleanup on empty error."""
        assert should_cleanup_on_clone_error("") is True

    def test_cleanup_on_unknown_error(self) -> None:
        """Test cleanup on unknown errors."""
        assert should_cleanup_on_clone_error("Some random error") is True

    def test_cleanup_on_repository_not_found(self) -> None:
        """Test cleanup on repository not found."""
        assert should_cleanup_on_clone_error("Repository not found") is True
