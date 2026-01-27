# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2025 Matthew Watkins <mwatkins@linuxfoundation.org>

"""GitHub repository clone worker with support for gh CLI and git clone."""

from __future__ import annotations

import shutil
import subprocess
from datetime import UTC, datetime
from typing import TYPE_CHECKING
from urllib.parse import urlparse, urlunparse

from gerrit_clone.clone_utils import (
    analyze_git_clone_error,
    build_base_clone_command,
    should_cleanup_on_clone_error,
)
from gerrit_clone.git_utils import is_git_repository
from gerrit_clone.logging import get_logger
from gerrit_clone.models import CloneResult, CloneStatus, Config, Project
from gerrit_clone.pathing import AtomicClonePath

if TYPE_CHECKING:
    from pathlib import Path

logger = get_logger(__name__)


def clone_github_repository(
    project: Project,
    config: Config,
) -> CloneResult:
    """Clone a GitHub repository using gh CLI or git.

    Supports multiple authentication methods:
    - SSH (default): Uses SSH keys configured in the environment
    - HTTPS with token: Embeds GitHub token in URL for authentication
    - HTTPS with credential helper: Falls back to git credential helper
    - GitHub CLI: Uses gh CLI authentication

    For HTTPS cloning with a token:
    - Token is embedded in the clone URL: https://token@github.com/org/repo.git
    - Token is removed from .git/config after successful clone for security
    - GIT_TERMINAL_PROMPT=0 is set to prevent interactive credential prompts

    Args:
        project: Project to clone
        config: Configuration with optional github_token for HTTPS auth

    Returns:
        CloneResult with outcome
    """
    started_at = datetime.now(UTC)
    target_path = config.path / project.filesystem_path

    # Check if already exists (both regular and bare repositories)
    if target_path.exists():
        if is_git_repository(target_path):
            logger.debug(f"Repository already exists: {project.name}")
            completed_at = datetime.now(UTC)
            duration = (completed_at - started_at).total_seconds()
            return CloneResult(
                project=project,
                status=CloneStatus.ALREADY_EXISTS,
                path=target_path,
                started_at=started_at,
                completed_at=completed_at,
                duration_seconds=duration,
            )
        else:
            # Directory exists but not a git repo
            logger.warning(
                f"Directory exists but is not a git repository: {target_path}"
            )
            completed_at = datetime.now(UTC)
            duration = (completed_at - started_at).total_seconds()
            return CloneResult(
                project=project,
                status=CloneStatus.FAILED,
                path=target_path,
                error_message="Directory exists but is not a git repository",
                started_at=started_at,
                completed_at=completed_at,
                duration_seconds=duration,
            )

    # Ensure parent directory exists
    target_path.parent.mkdir(parents=True, exist_ok=True)

    # Determine clone method
    if config.use_gh_cli and _is_gh_cli_available():
        return _clone_with_gh_cli(project, config, target_path, started_at)
    else:
        return _clone_with_git(project, config, target_path, started_at)


def _is_gh_cli_available() -> bool:
    """Check if GitHub CLI (gh) is available.

    Returns:
        True if gh CLI is installed and accessible
    """
    return shutil.which("gh") is not None


def _clone_with_gh_cli(
    project: Project,
    config: Config,
    target_path: Path,
    started_at: datetime,
) -> CloneResult:
    """Clone repository using GitHub CLI.

    Args:
        project: Project to clone
        config: Configuration
        target_path: Target clone path
        start_time: Clone start time

    Returns:
        CloneResult
    """
    logger.debug(f"Cloning {project.name} with gh CLI")

    # Build gh repo clone command
    cmd = ["gh", "repo", "clone"]

    # Add repository identifier (org/repo or full URL)
    # gh CLI can handle both "org/repo" format and full URLs
    if project.clone_url and project.clone_url.startswith("http"):
        repo_identifier = project.clone_url
    else:
        repo_identifier = project.name

    cmd.append(repo_identifier)
    cmd.append(str(target_path))

    # Use --mirror for complete repository metadata (all refs, tags, branches)
    # This creates a bare repository that is a complete copy of the remote
    if config.mirror:
        cmd.extend(["--", "--mirror"])
    else:
        # Non-mirror mode: optionally use shallow clone or specific branch
        # Add depth for shallow clone
        if config.depth:
            cmd.extend(["--", "--depth", str(config.depth)])

        # Add branch if specified
        if config.branch:
            if "--" not in cmd:
                cmd.append("--")
            cmd.extend(["--branch", config.branch])

    # Execute clone
    try:
        logger.debug(f"Executing: {' '.join(cmd)}")
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=config.clone_timeout,
            check=False,
        )

        if result.returncode == 0:
            logger.debug(f"✓ Cloned {project.name} with gh CLI")
            completed_at = datetime.now(UTC)
            duration = (completed_at - started_at).total_seconds()
            return CloneResult(
                project=project,
                status=CloneStatus.SUCCESS,
                path=target_path,
                started_at=started_at,
                completed_at=completed_at,
                duration_seconds=duration,
            )
        else:
            error_msg = result.stderr.strip() or result.stdout.strip()
            logger.error(f"✗ Failed to clone {project.name}: {error_msg}")

            # Clean up failed clone directory
            if target_path.exists():
                shutil.rmtree(target_path, ignore_errors=True)

            completed_at = datetime.now(UTC)
            duration = (completed_at - started_at).total_seconds()
            return CloneResult(
                project=project,
                status=CloneStatus.FAILED,
                path=target_path,
                error_message=error_msg,
                started_at=started_at,
                completed_at=completed_at,
                duration_seconds=duration,
            )

    except subprocess.TimeoutExpired:
        error_msg = f"Clone timeout after {config.clone_timeout}s"
        logger.error(f"✗ {project.name}: {error_msg}")

        # Clean up
        if target_path.exists():
            shutil.rmtree(target_path, ignore_errors=True)

        completed_at = datetime.now(UTC)
        duration = (completed_at - started_at).total_seconds()
        return CloneResult(
            project=project,
            status=CloneStatus.FAILED,
            path=target_path,
            error_message=error_msg,
            started_at=started_at,
            completed_at=completed_at,
            duration_seconds=duration,
        )
    except Exception as e:
        error_msg = f"Clone error: {e}"
        logger.error(f"✗ {project.name}: {error_msg}")

        # Clean up
        if target_path.exists():
            shutil.rmtree(target_path, ignore_errors=True)

        completed_at = datetime.now(UTC)
        duration = (completed_at - started_at).total_seconds()
        return CloneResult(
            project=project,
            status=CloneStatus.FAILED,
            path=target_path,
            error_message=error_msg,
            started_at=started_at,
            completed_at=completed_at,
            duration_seconds=duration,
        )


def _clone_with_git(  # noqa: PLR0915
    project: Project,
    config: Config,
    target_path: Path,
    started_at: datetime,
) -> CloneResult:
    """Clone repository using standard git clone.

    Args:
        project: Project to clone
        config: Configuration
        target_path: Target clone path
        started_at: Clone start time

    Returns:
        CloneResult
    """
    # Determine clone URL - prefer SSH, fall back to HTTPS
    if config.use_https:
        # Explicit HTTPS requested
        clone_url = project.clone_url or project.https_url(config.base_url)

        # Embed GitHub token in URL for authentication if provided
        if config.github_token and clone_url.startswith("https://"):
            # Insert token into URL: https://token@github.com/org/repo.git
            clone_url = clone_url.replace("https://", f"https://{config.github_token}@", 1)
            logger.debug(f"Cloning {project.name} with HTTPS using token authentication")
        else:
            logger.debug(f"Cloning {project.name} with HTTPS (no token, will use credential helper)")
    elif project.ssh_url_override:
        # SSH URL available from GitHub (preferred)
        clone_url = project.ssh_url_override
    else:
        # Fall back to HTTPS if no SSH URL available
        clone_url = project.clone_url or project.https_url(config.base_url)

    # For logging, show URL without token (parse and reconstruct to avoid issues with special characters)
    log_url = clone_url
    if config.github_token:
        try:
            parsed = urlparse(clone_url)
            # Check if token is in the netloc (e.g., token@github.com)
            if "@" in parsed.netloc and config.github_token in parsed.netloc:
                # Reconstruct netloc with redacted token
                netloc_parts = parsed.netloc.split("@", 1)
                redacted_netloc = f"***@{netloc_parts[1]}"
                log_url = urlunparse((
                    parsed.scheme,
                    redacted_netloc,
                    parsed.path,
                    parsed.params,
                    parsed.query,
                    parsed.fragment,
                ))
        except Exception:
            # SECURITY: If parsing fails, use safe placeholder to avoid credential leak
            log_url = f"https://***@github.com/{project.name}.git"
    logger.debug(f"Cloning {project.name} with git from {log_url}")

    # Setup environment for git
    env = _build_git_env(config)

    # Use atomic clone path for safety (automatic cleanup on failure)
    with AtomicClonePath(target_path) as atomic_path:
        # Build clone command using shared utility
        cmd = build_base_clone_command(clone_url, atomic_path.temp_path, config)

        # GitHub-specific: add --single-branch when user explicitly requests a branch
        # Must insert before the URL (second to last position)
        if not config.mirror and config.branch:
            cmd.insert(-2, "--single-branch")

        # Execute clone
        try:
            logger.debug(f"Executing: {' '.join(cmd)}")
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=config.clone_timeout,
                env=env,
                check=False,
            )

            if result.returncode == 0:
                logger.debug(f"✓ Cloned {project.name}")

                # Post-clone: remove token from remote URL for security
                if config.github_token and config.use_https and config.github_token in clone_url:
                    _remove_token_from_remote_url(atomic_path.temp_path, project, config)

                # Finalize atomic operation (move temp to target)
                atomic_path.finalize()

                completed_at = datetime.now(UTC)
                duration = (completed_at - started_at).total_seconds()
                return CloneResult(
                    project=project,
                    status=CloneStatus.SUCCESS,
                    path=target_path,
                    started_at=started_at,
                    completed_at=completed_at,
                    duration_seconds=duration,
                )
            else:
                error_output = result.stderr.strip() or result.stdout.strip()

                # Use shared error analysis for better diagnostics
                analyzed_error = analyze_git_clone_error(
                    error_output, project.name, config.host
                )
                logger.error(f"✗ Failed to clone {project.name}: {analyzed_error}")

                # Decide whether to cleanup or preserve directory for inspection
                if should_cleanup_on_clone_error(error_output):
                    # Normal error - cleanup temp directory
                    atomic_path.cleanup_temp()
                else:
                    # Special case (e.g., auth error) - preserve for inspection
                    logger.debug(f"Preserving directory for inspection: {atomic_path.temp_path}")

                completed_at = datetime.now(UTC)
                duration = (completed_at - started_at).total_seconds()
                return CloneResult(
                    project=project,
                    status=CloneStatus.FAILED,
                    path=target_path,
                    error_message=analyzed_error,
                    started_at=started_at,
                    completed_at=completed_at,
                    duration_seconds=duration,
                )

        except subprocess.TimeoutExpired:
            error_msg = f"Clone timeout after {config.clone_timeout}s"
            logger.error(f"✗ {project.name}: {error_msg}")
            # Explicitly cleanup temp directory since we're catching the exception
            atomic_path.cleanup_temp()

            completed_at = datetime.now(UTC)
            duration = (completed_at - started_at).total_seconds()
            return CloneResult(
                project=project,
                status=CloneStatus.FAILED,
                path=target_path,
                error_message=error_msg,
                started_at=started_at,
                completed_at=completed_at,
                duration_seconds=duration,
            )
        except Exception as e:
            error_msg = f"Clone error: {e}"
            logger.error(f"✗ {project.name}: {error_msg}")
            # Explicitly cleanup temp directory since we're catching the exception
            atomic_path.cleanup_temp()

            completed_at = datetime.now(UTC)
            duration = (completed_at - started_at).total_seconds()
            return CloneResult(
                project=project,
                status=CloneStatus.FAILED,
                path=target_path,
                error_message=error_msg,
                started_at=started_at,
                completed_at=completed_at,
                duration_seconds=duration,
            )


def _build_git_env(config: Config) -> dict[str, str]:
    """Build environment variables for git commands.

    Sets up the environment for secure, non-interactive git operations:
    - SSH configuration if ssh_identity_file is provided
    - GIT_TERMINAL_PROMPT=0 for HTTPS to prevent interactive prompts
    - Credential helper disabled when using HTTPS with token

    When using HTTPS with a token, the credential helper is explicitly
    disabled to ensure fully automated, non-interactive operation. This
    prevents git from:
    - Prompting for credentials interactively
    - Storing credentials in the system keychain
    - Falling back to other credential helpers

    This is intentional for CI/CD and automation scenarios where the token
    is embedded in the clone URL. If the token is invalid or missing, the
    operation will fail immediately rather than prompting or using cached
    credentials, ensuring predictable behavior.

    Args:
        config: Configuration with optional SSH and HTTPS settings

    Returns:
        Environment dictionary with git-specific variables
    """
    import os

    env = os.environ.copy()

    # Add SSH key if provided
    if config.ssh_identity_file:
        ssh_cmd = f"ssh -i {config.ssh_identity_file}"
        if not config.strict_host_checking:
            ssh_cmd += " -o StrictHostKeyChecking=no"
        env["GIT_SSH_COMMAND"] = ssh_cmd
    elif not config.strict_host_checking:
        env["GIT_SSH_COMMAND"] = "ssh -o StrictHostKeyChecking=no"

    # Prevent interactive credential prompts when using HTTPS
    # Token is embedded in URL, so we don't want git asking for credentials
    if config.use_https:
        # Disable interactive prompts (fail fast if auth fails)
        env["GIT_TERMINAL_PROMPT"] = "0"
        # Disable credential helper to prevent:
        # 1. Interactive credential prompts in CI/CD
        # 2. Credential storage in system keychain
        # 3. Fallback to cached/stored credentials
        # This ensures the embedded token is used exclusively and operations
        # fail predictably if the token is invalid/missing
        env["GIT_CONFIG_COUNT"] = "1"
        env["GIT_CONFIG_KEY_0"] = "credential.helper"
        env["GIT_CONFIG_VALUE_0"] = ""

    return env


def _remove_token_from_remote_url(
    repo_path: Path,
    project: Project,
    config: Config,
) -> None:
    """Remove authentication token from git remote URL after cloning.

    Security measure to prevent token leakage:
    - Tokens are embedded in URLs for clone authentication
    - After successful clone, the remote URL is updated to remove the token
    - This prevents the token from being stored in .git/config
    - Subsequent git operations will use credential helper or SSH

    This operation is CRITICAL for security - if token removal fails, the clone
    operation will fail to prevent credential leakage in .git/config.

    Args:
        repo_path: Path to cloned repository
        project: Project that was cloned
        config: Configuration with github_token

    Raises:
        RuntimeError: If token removal fails (security-critical operation)
    """
    try:
        # Get the clean HTTPS URL without token
        clean_url = project.clone_url or project.https_url(config.base_url)

        # Update the remote URL to remove token
        subprocess.run(
            ["git", "remote", "set-url", "origin", clean_url],
            cwd=repo_path,
            capture_output=True,
            text=True,
            check=True,
        )
        logger.debug(f"Removed token from remote URL for {project.name}")
    except subprocess.CalledProcessError as e:
        # CRITICAL: Token removal failed - this is a security issue
        # Delete the repository to prevent credential leakage
        import shutil
        try:
            shutil.rmtree(repo_path)
            logger.warning(f"Deleted repository {repo_path} due to token removal failure")
        except Exception as cleanup_error:
            logger.error(f"Failed to cleanup repository after token removal failure: {cleanup_error}")

        error_msg = (
            f"SECURITY: Failed to remove token from remote URL for {project.name}. "
            f"Repository deleted to prevent credential leakage. Error: {e.stderr.strip()}"
        )
        logger.error(error_msg)
        raise RuntimeError(error_msg) from e
    except Exception as e:
        # CRITICAL: Unexpected error during token removal
        # Delete the repository to prevent credential leakage
        import shutil
        try:
            shutil.rmtree(repo_path)
            logger.warning(f"Deleted repository {repo_path} due to token removal failure")
        except Exception as cleanup_error:
            logger.error(f"Failed to cleanup repository after token removal failure: {cleanup_error}")

        error_msg = (
            f"SECURITY: Failed to update remote URL for {project.name}. "
            f"Repository deleted to prevent credential leakage. Error: {e}"
        )
        logger.error(error_msg)
        raise RuntimeError(error_msg) from e
