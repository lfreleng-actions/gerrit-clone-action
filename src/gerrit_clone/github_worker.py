# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2025 Matthew Watkins <mwatkins@linuxfoundation.org>

"""GitHub repository clone worker with support for gh CLI and git clone.

Entry point for cloning a single GitHub repository.  URL selection,
process environment, token hygiene and the ``gh`` CLI path each live in
their own module; this one owns the git clone itself.
"""

from __future__ import annotations

import shutil
import subprocess
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from gerrit_clone.clone_timeout import TargetOwnedError, claim_new_target
from gerrit_clone.clone_utils import (
    analyze_git_clone_error,
    build_base_clone_command,
    should_cleanup_on_clone_error,
)
from gerrit_clone.git_utils import is_git_repository
from gerrit_clone.github_clone_env import build_git_env
from gerrit_clone.github_clone_results import build_clone_result
from gerrit_clone.github_clone_url import redact_clone_url, resolve_clone_url
from gerrit_clone.github_gh_cli import clone_with_gh_cli
from gerrit_clone.github_token_hygiene import remove_token_from_remote_url
from gerrit_clone.logging import get_logger
from gerrit_clone.models import CloneResult, CloneStatus, Config, Project
from gerrit_clone.pathing import AtomicClonePath
from gerrit_clone.subprocess_tracking import run_tracked

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
            return build_clone_result(
                project, target_path, started_at, CloneStatus.ALREADY_EXISTS
            )

        # Directory exists but not a git repo
        logger.warning(f"Directory exists but is not a git repository: {target_path}")
        return build_clone_result(
            project,
            target_path,
            started_at,
            CloneStatus.FAILED,
            "Directory exists but is not a git repository",
        )

    # Ensure parent directory exists
    target_path.parent.mkdir(parents=True, exist_ok=True)

    # Determine clone method
    if config.use_gh_cli and _is_gh_cli_available():
        return clone_with_gh_cli(project, config, target_path, started_at)
    else:
        return _clone_with_git(project, config, target_path, started_at)


def _is_gh_cli_available() -> bool:
    """Check if GitHub CLI (gh) is available.

    Returns:
        True if gh CLI is installed and accessible
    """
    return shutil.which("gh") is not None


def _handle_git_clone_failure(
    atomic_path: AtomicClonePath,
    error_output: str,
    project: Project,
    config: Config,
) -> str:
    """Diagnose a failed git clone and dispose of the temp directory.

    Args:
        atomic_path: In-progress atomic clone path
        error_output: Combined git stderr/stdout
        project: Project being cloned
        config: Configuration providing the source host

    Returns:
        The analysed, human-readable error message.
    """
    # Use shared error analysis for better diagnostics
    analyzed_error = analyze_git_clone_error(error_output, project.name, config.host)
    logger.error(f"✗ Failed to clone {project.name}: {analyzed_error}")

    # Decide whether to cleanup or preserve directory for inspection
    if should_cleanup_on_clone_error(error_output):
        # Normal error - cleanup temp directory
        atomic_path.cleanup_temp()
    else:
        # Special case (e.g., auth error) - preserve for inspection
        logger.debug(f"Preserving directory for inspection: {atomic_path.temp_path}")

    return analyzed_error


def _clone_with_git(
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
    clone_url = resolve_clone_url(project, config)

    # For logging, show URL without token
    log_url = redact_clone_url(clone_url, project, config.github_token)
    logger.debug(f"Cloning {project.name} with git from {log_url}")

    env = build_git_env(config)

    # Reserved as it is taken, so a timeout may discard what this clone
    # leaves behind.  Reached only once the already-exists checks above
    # have passed, so the destination is absent; a refusal means another
    # batch is already cloning there and this one must stand down rather
    # than write to a path it does not own.
    try:
        claim_new_target(target_path, project.name)
    except TargetOwnedError as exc:
        logger.error(f"✗ {project.name}: {exc}")
        return build_clone_result(
            project, target_path, started_at, CloneStatus.FAILED, str(exc)
        )

    # Use atomic clone path for safety (automatic cleanup on failure)
    with AtomicClonePath(target_path) as atomic_path:
        cmd = build_base_clone_command(clone_url, atomic_path.temp_path, config)

        # GitHub-specific: add --single-branch when user explicitly requests a branch
        # Must insert before the URL (second to last position)
        if not config.mirror and config.branch:
            cmd.insert(-2, "--single-branch")

        try:
            logger.debug(f"Executing: {' '.join(cmd)}")
            # Tracked so a batch that gives up can terminate the child
            # rather than wait for it; see
            # gerrit_clone.subprocess_tracking.
            result = run_tracked(
                cmd,
                timeout=config.clone_timeout,
                env=env,
            )

            if result.returncode != 0:
                error_output = result.stderr.strip() or result.stdout.strip()
                analyzed_error = _handle_git_clone_failure(
                    atomic_path, error_output, project, config
                )
                return build_clone_result(
                    project,
                    target_path,
                    started_at,
                    CloneStatus.FAILED,
                    analyzed_error,
                )

            logger.debug(f"✓ Cloned {project.name}")

            # Post-clone: remove token from remote URL for security
            if (
                config.github_token
                and config.use_https
                and config.github_token in clone_url
            ):
                remove_token_from_remote_url(atomic_path.temp_path, project, config)

            # Finalize atomic operation (move temp to target)
            atomic_path.finalize()

            return build_clone_result(
                project, target_path, started_at, CloneStatus.SUCCESS
            )

        except subprocess.TimeoutExpired:
            error_msg = f"Clone timeout after {config.clone_timeout}s"
            logger.error(f"✗ {project.name}: {error_msg}")
            # Explicitly cleanup temp directory since we're catching the exception
            atomic_path.cleanup_temp()
            return build_clone_result(
                project, target_path, started_at, CloneStatus.FAILED, error_msg
            )
        except Exception as e:
            error_msg = f"Clone error: {e}"
            logger.error(f"✗ {project.name}: {error_msg}")
            # Explicitly cleanup temp directory since we're catching the exception
            atomic_path.cleanup_temp()
            return build_clone_result(
                project, target_path, started_at, CloneStatus.FAILED, error_msg
            )
