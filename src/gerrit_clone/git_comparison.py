# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2025 Matthew Watkins <mwatkins@linuxfoundation.org>

"""Git comparison utilities for local vs remote repository synchronization."""

from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path

from gerrit_clone.logging import get_logger
from gerrit_clone.reset_models import (
    GitHubRepoStatus,
    LocalRepoStatus,
    SyncComparison,
)

logger = get_logger(__name__)


def scan_local_gerrit_clone(base_path: Path) -> dict[str, LocalRepoStatus]:
    """
    Scan local Gerrit clone directory hierarchy for Git repositories.

    Walks the directory tree looking for .git directories and extracts
    status information from each valid Git repository found.

    Note: This function intentionally scans the ENTIRE directory tree without
    depth limits to ensure complete coverage of all repositories in the Gerrit
    clone structure, which may be organized in arbitrarily deep hierarchies.
    However, it efficiently skips the contents of .git directories themselves
    to avoid unnecessary traversal of potentially thousands of internal Git
    objects and refs. Completeness is prioritized over speed for this use case.

    Args:
        base_path: Root directory to scan for Git repositories

    Returns:
        Dictionary mapping repository name to LocalRepoStatus
    """
    repos: dict[str, LocalRepoStatus] = {}

    if not base_path.exists():
        logger.warning(f"Base path does not exist: {base_path}")
        return repos

    if not base_path.is_dir():
        logger.warning(f"Base path is not a directory: {base_path}")
        return repos

    logger.info(f"Scanning for Git repositories in: {base_path}")

    # Walk directory tree looking for .git directories
    # Use os.walk with topdown=True to allow directory pruning
    try:
        for root, dirs, _files in os.walk(base_path, topdown=True):
            # Check if current directory contains a .git subdirectory
            if ".git" in dirs:
                repo_path = Path(root)
                repo_name = repo_path.name

                logger.debug(f"Found Git repository: {repo_name} at {repo_path}")

                status = _get_local_repo_status(repo_path)
                repos[repo_name] = status

            # Remove .git from dirs to skip descending into it
            # This avoids scanning thousands of internal Git objects and refs
            if ".git" in dirs:
                dirs.remove(".git")

    except Exception as e:
        logger.error(f"Error scanning directory tree: {e}")

    logger.info(f"Found {len(repos)} Git repositories")
    return repos


def _run_git_command_with_retry(
    cmd: list[str],
    cwd: Path,
    max_attempts: int = 3,
    timeout: int = 12,
    base_delay: float = 0.5,
    max_delay: float = 2.0,
) -> subprocess.CompletedProcess[str]:
    """
    Run a git command with retry logic to handle transient failures.

    Uses exponential backoff for retries to handle transient issues more
    effectively while avoiding excessive delays for quickly-resolving problems.

    Default parameters are tuned for local Git read operations (rev-parse,
    rev-list, etc.) which may encounter transient lock contention or filesystem
    I/O issues. With defaults, delays are: 0.5s, 1.0s, capped at 2.0s.

    Args:
        cmd: Git command and arguments as list
        cwd: Working directory for the command
        max_attempts: Maximum number of attempts (default: 3)
        timeout: Timeout in seconds for each attempt (default: 12)
        base_delay: Base delay for exponential backoff in seconds (default: 0.5)
        max_delay: Maximum delay between retries in seconds (default: 2.0)

    Returns:
        CompletedProcess result from successful attempt or last attempt
    """
    last_result: subprocess.CompletedProcess[str] | None = None
    for attempt in range(1, max_attempts + 1):
        try:
            result = subprocess.run(
                cmd,
                cwd=cwd,
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
            )

            # If successful, return immediately
            if result.returncode == 0:
                return result

            # Store result for potential return
            last_result = result

            # If not the last attempt and command failed, log and retry
            if attempt < max_attempts:
                logger.debug(
                    f"Git command failed (attempt {attempt}/{max_attempts}): "
                    f"{' '.join(cmd)} in {cwd}"
                )
                # Exponential backoff: base_delay * (2 ^ (attempt - 1)), capped at max_delay
                delay = min(base_delay * (2 ** (attempt - 1)), max_delay)
                time.sleep(delay)

        except subprocess.TimeoutExpired as e:
            logger.warning(
                f"Git command timeout (attempt {attempt}/{max_attempts}): "
                f"{' '.join(cmd)} in {cwd}"
            )
            if attempt < max_attempts:
                # Exponential backoff: base_delay * (2 ^ (attempt - 1)), capped at max_delay
                delay = min(base_delay * (2 ** (attempt - 1)), max_delay)
                time.sleep(delay)
            else:
                last_result = subprocess.CompletedProcess[str](
                    args=cmd,
                    returncode=1,
                    stdout="",
                    stderr=str(e),
                )
        except Exception as e:
            logger.error(f"Unexpected error running git command: {e}")
            last_result = subprocess.CompletedProcess[str](
                args=cmd,
                returncode=1,
                stdout="",
                stderr=str(e),
            )
            break

    # This should never be None since we always set it in the loop
    assert last_result is not None, "last_result should not be None"
    return last_result


def _get_local_repo_status(repo_path: Path) -> LocalRepoStatus:
    """
    Get status information for a local Git repository.

    Args:
        repo_path: Path to the Git repository

    Returns:
        LocalRepoStatus with repository information
    """
    branch_result = _run_git_command_with_retry(
        ["git", "rev-parse", "--abbrev-ref", "HEAD"],
        cwd=repo_path,
    )
    current_branch = (
        branch_result.stdout.strip() if branch_result.returncode == 0 else None
    )

    sha_result = _run_git_command_with_retry(
        ["git", "rev-parse", "HEAD"],
        cwd=repo_path,
    )
    last_sha = sha_result.stdout.strip() if sha_result.returncode == 0 else None

    count_result = _run_git_command_with_retry(
        ["git", "rev-list", "--count", "HEAD"],
        cwd=repo_path,
    )
    commit_count = (
        int(count_result.stdout.strip()) if count_result.returncode == 0 else None
    )

    is_valid = sha_result.returncode == 0

    if not is_valid:
        logger.warning(
            f"Repository at {repo_path} is not a valid Git repository or HEAD is invalid"
        )

    return LocalRepoStatus(
        name=repo_path.name,
        path=repo_path,
        last_commit_sha=last_sha,
        commit_count=commit_count,
        current_branch=current_branch,
        is_valid_git_repo=is_valid,
    )


def compare_local_with_remote(
    local_repos: dict[str, LocalRepoStatus],
    remote_repos: dict[str, GitHubRepoStatus],
) -> list[SyncComparison]:
    """
    Compare local and remote repositories to detect synchronization differences.

    Args:
        local_repos: Dictionary of local repository statuses
        remote_repos: Dictionary of remote GitHub repository statuses

    Returns:
        List of SyncComparison objects, sorted with unsynchronized repos first
    """
    comparisons: list[SyncComparison] = []

    for remote_name, remote_status in remote_repos.items():
        local_status = local_repos.get(remote_name)

        is_synced, description = _determine_sync_status(local_status, remote_status)

        comparisons.append(
            SyncComparison(
                repo_name=remote_name,
                local_status=local_status,
                remote_status=remote_status,
                is_synchronized=is_synced,
                difference_description=description,
            )
        )

    # Sort: unsynchronized first, then alphabetically by name
    comparisons.sort(key=lambda c: (c.is_synchronized, c.repo_name))

    unsynchronized_count = sum(1 for c in comparisons if not c.is_synchronized)
    logger.info(
        f"Comparison complete: {unsynchronized_count}/{len(comparisons)} unsynchronized"
    )

    return comparisons


def _determine_sync_status(  # noqa: PLR0911
    local: LocalRepoStatus | None,
    remote: GitHubRepoStatus,
) -> tuple[bool, str]:
    """
    Determine if local and remote repositories are synchronized.

    Args:
        local: Local repository status (None if not found locally)
        remote: Remote GitHub repository status

    Returns:
        Tuple of (is_synchronized, description)
    """
    if not local:
        return True, "No local copy"

    if not local.is_valid_git_repo:
        return True, "Local repo invalid"

    if not local.last_commit_sha:
        return False, "Unable to read local commit SHA"

    if not remote.last_commit_sha:
        return False, "Remote commit SHA unavailable"

    # Exact SHA match
    if local.last_commit_sha == remote.last_commit_sha:
        return True, "Synchronized"

    # Check for short SHA match (first 7+ characters)
    # This handles cases where remote might return short SHA
    local_short = local.last_commit_sha[:8]
    remote_short = remote.last_commit_sha[:8]

    if local_short == remote_short:
        return True, "Synchronized (SHA prefix match)"

    # SHAs differ - repositories are out of sync
    return False, f"Different commits (local: {local_short}, remote: {remote_short})"


def transform_github_name_to_gerrit(github_name: str) -> str:
    """
    Transform GitHub repository name back to potential Gerrit project name.

    GitHub names like 'ccsdk-apps' might correspond to Gerrit 'ccsdk/apps'.
    This is the inverse of the transformation used in mirroring.

    Args:
        github_name: GitHub repository name

    Returns:
        Potential Gerrit project name
    """
    # Simple heuristic: replace first dash with slash
    # This may not be perfect but gives a reasonable guess
    if "-" in github_name:
        return github_name.replace("-", "/", 1)
    return github_name
