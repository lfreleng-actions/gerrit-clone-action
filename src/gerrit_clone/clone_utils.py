# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2025 Matthew Watkins <mwatkins@linuxfoundation.org>

"""Shared utilities for git clone operations across different workers.

This module consolidates common clone logic used by both Gerrit and GitHub workers,
reducing duplication and ensuring consistent behavior.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path

    from gerrit_clone.models import Config


def build_base_clone_command(
    clone_url: str,
    target_path: Path,
    config: Config,
) -> list[str]:
    """Build base git clone command with common options.

    This function consolidates the common clone command building logic used by
    both Gerrit (CloneWorker) and GitHub (github_worker) clone operations.

    Args:
        clone_url: Git URL to clone from
        target_path: Target path for the clone
        config: Configuration object with mirror, depth, branch settings

    Returns:
        Git clone command as list of strings
    """
    cmd = ["git", "clone"]

    # Add options to reduce filesystem contention and I/O (compatible with all modes)
    cmd.extend(
        [
            "--no-hardlinks",  # Prevent hardlink creation that can cause locks
            "--quiet",  # Reduce output and potential I/O contention
        ]
    )

    # Use --mirror for complete repository metadata (all refs, tags, branches)
    # This creates a bare repository that is a complete copy of the remote
    if config.mirror:
        cmd.append("--mirror")
    else:
        # Non-mirror mode: optionally use shallow clone or specific branch
        # Add depth option for shallow clone
        if config.depth is not None:
            cmd.extend(["--depth", str(config.depth)])

        # Add branch option (only in non-mirror mode)
        if config.branch is not None:
            cmd.extend(["--branch", config.branch])

    # Add clone URL and target path
    cmd.append(clone_url)
    cmd.append(str(target_path))

    return cmd


def is_retryable_git_error(error_output: str) -> bool:
    """Determine if a git clone error is retryable.

    This consolidates retry logic for transient errors that may succeed on retry,
    such as network issues, timeouts, or temporary service unavailability.

    Args:
        error_output: Error output from git command (stderr or stdout)

    Returns:
        True if the error is likely retryable, False otherwise
    """
    if not error_output:
        return False

    error_lower = error_output.lower()

    # Network and connectivity errors (retryable)
    retryable_patterns = [
        # Connection issues
        "connection timeout",
        "connection timed out",
        "connect to host",
        "connection refused",
        "connection reset",
        "broken pipe",
        "network is unreachable",
        # DNS issues
        "temporary failure in name resolution",
        "could not resolve hostname",
        "name or service not known",
        # Git protocol errors
        "early eof",
        "the remote end hung up unexpectedly",
        "transfer closed",
        "rpc failed",
        "fetch-pack: unable to spawn",
        # Server-side transient errors
        "service temporarily unavailable",
        "502 bad gateway",
        "503 service unavailable",
        "504 gateway timeout",
        # SSH specific
        "ssh: connect to host",
        "kex_exchange_identification",
        # Git pack/object errors (can be transient)
        "pack-objects died",
        "index-pack failed",
        "fatal: protocol error: bad pack header",
    ]

    for pattern in retryable_patterns:
        if pattern in error_lower:
            return True

    # Non-retryable errors (authentication, permissions, repo doesn't exist)
    non_retryable_patterns = [
        "permission denied",
        "authentication failed",
        "access denied",
        "repository not found",
        "does not exist",
        "host key verification failed",
        "could not read from remote repository",
        "fatal: repository",
        "invalid credentials",
        "bad credentials",
    ]

    for pattern in non_retryable_patterns:
        if pattern in error_lower:
            return False

    # Default to non-retryable for unknown errors
    return False


def analyze_git_clone_error(error_output: str, project_name: str, host: str | None = None) -> str:  # noqa: PLR0911, PLR0915
    """Analyze git clone error and provide helpful diagnostic message.

    Args:
        error_output: Error output from git command
        project_name: Name of the project being cloned
        host: Optional host name for SSH-specific diagnostics

    Returns:
        User-friendly error message with diagnostics
    """
    if not error_output:
        return "Clone failed with no error output"

    error_lower = error_output.lower()

    # SSH authentication issues
    if "permission denied" in error_lower and "publickey" in error_lower:
        msg = f"SSH authentication failed for {project_name}\n"
        msg += "Possible causes:\n"
        msg += "  • SSH key not added to ssh-agent (run: ssh-add <key-path>)\n"
        msg += "  • SSH key not authorized on the server\n"
        msg += "  • Wrong SSH user (try setting ssh_user in config)\n"
        if host:
            msg += f"\nTest SSH access: ssh -T {host}"
        return msg

    # Host key verification
    if "host key verification failed" in error_lower:
        msg = f"SSH host key verification failed for {project_name}\n"
        msg += "Possible causes:\n"
        msg += "  • Host not in known_hosts file\n"
        msg += "  • Host key has changed (security risk!)\n"
        if host:
            msg += f"\nAdd host key: ssh-keyscan {host} >> ~/.ssh/known_hosts\n"
            msg += f"Or disable strict checking (less secure): ssh -o StrictHostKeyChecking=no {host}"
        return msg

    # Connection refused
    if "connection refused" in error_lower:
        port_match = None
        if "port" in error_lower:
            # Try to extract port number
            import re
            match = re.search(r"port (\d+)", error_lower)
            if match:
                port_match = match.group(1)

        msg = f"Connection refused for {project_name}\n"
        msg += "Possible causes:\n"
        msg += "  • SSH service is not running on the server\n"
        if port_match:
            msg += f"  • Wrong port (currently using {port_match})\n"
        if host:
            msg += f"  • Firewall blocking access to {host}\n"
            msg += f"\nVerify SSH port: nmap -p 22,29418 {host}"
        return msg

    # DNS resolution failures
    if "could not resolve hostname" in error_lower or "name or service not known" in error_lower:
        msg = f"DNS resolution failed for {project_name}\n"
        msg += "Possible causes:\n"
        msg += "  • Hostname is incorrect\n"
        msg += "  • DNS server is unavailable\n"
        msg += "  • Network connectivity issue\n"
        if host:
            msg += f"\nTest DNS: nslookup {host}"
        return msg

    # Repository not found
    if "repository not found" in error_lower or "does not exist" in error_lower:
        msg = f"Repository not found: {project_name}\n"
        msg += "Possible causes:\n"
        msg += "  • Repository name is incorrect\n"
        msg += "  • Repository has been deleted or moved\n"
        msg += "  • You don't have permission to access this repository"
        return msg

    # Timeout errors
    if "timeout" in error_lower or "timed out" in error_lower:
        msg = f"Connection timeout for {project_name}\n"
        msg += "Possible causes:\n"
        msg += "  • Network is slow or unstable\n"
        msg += "  • Server is overloaded\n"
        msg += "  • Repository is very large\n"
        msg += "\nConsider increasing clone_timeout in config"
        return msg

    # Generic network errors
    if any(pattern in error_lower for pattern in ["network", "connection", "eof", "hung up"]):
        return f"Network error cloning {project_name}: {error_output.strip()}"

    # Default: return original error
    return error_output.strip()


def should_cleanup_on_clone_error(error_output: str) -> bool:
    """Determine if failed clone directory should be cleaned up.

    Some errors leave the directory in a state where it should be removed,
    while others (like permission errors) should leave it for inspection.

    Args:
        error_output: Error output from git command

    Returns:
        True if directory should be cleaned up, False to leave it
    """
    if not error_output:
        return True  # No error output - clean up

    error_lower = error_output.lower()

    # Leave directory for inspection on authentication/permission issues
    # (user needs to debug credentials)
    inspection_patterns = [
        "permission denied",
        "authentication failed",
        "access denied",
        "host key verification failed",
    ]

    # Return False if any inspection pattern matches (leave for inspection)
    # Return True otherwise (clean up for network, timeouts, transient issues)
    return all(pattern not in error_lower for pattern in inspection_patterns)
