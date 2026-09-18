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

from gerrit_clone.clone_reservations import (
    TargetOwnedError,
    claim_new_target,
    reserved_by_other,
)
from gerrit_clone.clone_utils import (
    analyze_git_clone_error,
    build_base_clone_command,
    should_cleanup_on_clone_error,
)
from gerrit_clone.git_utils import is_git_repository
from gerrit_clone.github_clone_env import build_git_env
from gerrit_clone.github_clone_results import build_clone_result
from gerrit_clone.github_clone_url import (
    UnsafeCloneUrlError,
    redact_clone_url,
    resolve_clone_url,
)
from gerrit_clone.github_gh_cli import clone_with_gh_cli
from gerrit_clone.github_token_hygiene import remove_token_from_remote_url
from gerrit_clone.logging import get_logger
from gerrit_clone.models import CloneResult, CloneStatus, Config, Project
from gerrit_clone.pathing import AtomicClonePath
from gerrit_clone.subprocess_tracking import batch_abandoned, run_tracked

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
    - HTTPS with token: Authenticates through the git process environment
    - HTTPS with credential helper: Falls back to git credential helper
    - GitHub CLI: Uses gh CLI authentication

    For HTTPS cloning with a token:
    - The configured token is not put in the clone URL, so it never
      reaches the process arguments; it is passed via ``GIT_CONFIG_*``
      instead.  A credential already present in an externally supplied
      ``project.clone_url`` is passed through as given, and is the
      subject of issue #277.
    - A clone whose remote URL still holds the configured token is
      destroyed rather than kept, as defence in depth
    - GIT_TERMINAL_PROMPT=0 is set to prevent interactive credential prompts

    Args:
        project: Project to clone
        config: Configuration with optional github_token for HTTPS auth

    Returns:
        CloneResult with outcome
    """
    started_at = datetime.now(UTC)
    target_path = config.path / project.filesystem_path

    # Asked before anything on disk is believed.  While another clone is
    # working here its directory is in flux -- git creates .git before
    # transferring anything -- so the shortcut below could report a
    # repository whose owner's cleanup is about to remove it.
    contested = _contested_result(project, target_path, started_at)
    if contested is not None:
        return contested

    # Check if already exists (both regular and bare repositories)
    existing = _existing_target_result(project, target_path, started_at)
    if existing is not None:
        return existing

    # Reserved before anything is created, the parent directories
    # included: mkdir(parents=True) can bring into being an ancestor
    # another batch has reserved, which would have the owner's clone
    # fail on a directory that appeared beneath it.  Taken before the
    # clone method is chosen, so that every GitHub clone takes part
    # regardless of which one runs: the gh CLI writes to the same
    # destination and its partial directory is cleaned up the same way.
    # Reached only once the already-exists checks above have passed, so
    # the destination is absent; a refusal means another batch is
    # already cloning there and this one must stand down.
    try:
        claim_new_target(target_path, project.name)
    except TargetOwnedError as exc:
        logger.error(f"✗ {project.name}: {exc}")
        return build_clone_result(
            project, target_path, started_at, CloneStatus.FAILED, str(exc)
        )

    # Ensure parent directory exists
    target_path.parent.mkdir(parents=True, exist_ok=True)

    # Determine clone method
    if config.use_gh_cli and _is_gh_cli_available():
        return clone_with_gh_cli(project, config, target_path, started_at)
    else:
        return _clone_with_git(project, config, target_path, started_at)


def _contested_result(
    project: Project, target_path: Path, started_at: datetime
) -> CloneResult | None:
    """Refuse *target_path* if another clone holds it.

    Args:
        project: Project being cloned
        target_path: Destination being considered
        started_at: Clone start time

    Returns:
        A failure result, or ``None`` if the destination is free.
    """
    contested = reserved_by_other(target_path, project.name)
    if contested is None:
        return None
    error_msg = f"{contested} is already being cloned"
    logger.error(f"✗ {project.name}: {error_msg}")
    return build_clone_result(
        project, target_path, started_at, CloneStatus.FAILED, error_msg
    )


def _existing_target_result(
    project: Project, target_path: Path, started_at: datetime
) -> CloneResult | None:
    """Account for a destination that is already on disk, if it is.

    Args:
        project: Project being cloned
        target_path: Destination being considered
        started_at: Clone start time

    Returns:
        The result for an existing destination, or ``None`` if the path
        is absent and the clone should go ahead.
    """
    if not target_path.exists():
        return None

    # Asked again now that something has been found.  A clone always
    # reserves before it creates anything, so a directory put there by a
    # rival has a reservation that was taken strictly earlier -- which
    # this second look finds and the first could not, it having run
    # while the path was still absent.
    contested = _contested_result(project, target_path, started_at)
    if contested is not None:
        return contested

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
    try:
        clone_url = resolve_clone_url(project, config)
        env = build_git_env(config, clone_url)
    except UnsafeCloneUrlError as exc:
        # Fail closed: a URL git would read as an option, or one that is
        # not on the configured host, must not be handed to git -- the
        # second would have the token sent to that host.
        error_msg = f"Refusing to clone {project.name}: {exc}"
        logger.error(error_msg)
        return build_clone_result(
            project, target_path, started_at, CloneStatus.FAILED, error_msg
        )

    # For logging, show URL without token
    log_url = redact_clone_url(clone_url, project, config.github_token)
    logger.debug(f"Cloning {project.name} with git from {log_url}")

    # Use atomic clone path for safety (automatic cleanup on failure)
    with AtomicClonePath(target_path) as atomic_path:
        cmd = build_base_clone_command(clone_url, atomic_path.temp_path, config)

        # GitHub-specific: add --single-branch when user explicitly requests a branch
        # Inserted before the ``--`` separator, so git reads it as an
        # option rather than as the repository argument.  Positioned
        # against the command's guaranteed ``["--", url, target]``
        # tail rather than by searching for the separator: an option
        # value earlier in the command could equal ``--`` and would be
        # found first, splitting ``--branch`` from its argument.
        if not config.mirror and config.branch:
            cmd.insert(-3, "--single-branch")

        try:
            logger.debug(f"Executing: {' '.join(cmd).replace(clone_url, log_url)}")
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
                if batch_abandoned():
                    # Terminating the child surfaces here as a negative
                    # return code, indistinguishable from git failing on
                    # its own.  The preservation policy below would then
                    # keep the temporary clone for inspection, and the
                    # timeout cleanup could not remove it: that knows
                    # the reserved destination, not this randomly named
                    # .partial sibling of it.
                    atomic_path.cleanup_temp()
                    return build_clone_result(
                        project,
                        target_path,
                        started_at,
                        CloneStatus.FAILED,
                        "Clone abandoned before it finished",
                    )

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

            # Defence in depth.  This tool no longer puts the token in
            # the URL, so this fires only for an externally supplied one
            # that already carried it -- and for that case there is no
            # clean replacement to write, the only candidate being the
            # very value the token came in on.  So it refuses and
            # destroys the clone rather than leaving the credential in
            # .git/config; issue #277 is what would turn that into a
            # sanitised clone instead.
            #
            # Keyed on the resolved URL rather than ``config.use_https``:
            # the SSH branch falls back to an HTTPS URL when a project
            # has no SSH URL, and that clone needs the check just as
            # much.
            if (
                config.github_token
                and clone_url.startswith("https://")
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
