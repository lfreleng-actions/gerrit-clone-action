# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2025 Matthew Watkins <mwatkins@linuxfoundation.org>

"""Clone manager for coordinating bulk repository operations.

Entry point for a full run: discovery, gap analysis, refreshing the
repositories that already exist, cloning the ones that do not, retrying
failures and writing the manifest.

The individual stages live in sibling modules
(:mod:`~source_clone.clone_orchestrator`,
:mod:`~source_clone.clone_stages`,
:mod:`~source_clone.clone_refresh`,
:mod:`~source_clone.clone_results` and
:mod:`~source_clone.clone_reporting`).  The steps that resolve
``discover_projects``, ``get_current_commit_sha`` and ``RefreshWorker``
stay here so those collaborators remain patchable on this module.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

from rich.console import Console

from source_clone.clone_orchestrator import CloneManager
from source_clone.clone_refresh import (
    decide_refresh_from_shas,
    run_refresh_with_progress,
)
from source_clone.clone_reporting import (
    check_existing_manifest as _check_existing_manifest,
)
from source_clone.clone_reporting import (
    log_final_summary as _log_final_summary,
)
from source_clone.clone_reporting import (
    write_manifest as _write_manifest,
)
from source_clone.clone_results import (
    build_already_exists_result,
    build_verified_result,
)
from source_clone.clone_stages import (
    announce_clone_start,
    announce_source_connection,
    clone_missing_repos,
    partition_existing_repos,
    retry_failed_clones,
)
from source_clone.git_utils import get_current_commit_sha
from source_clone.logging import get_logger
from source_clone.models import BatchResult, RetryPolicy, SourceType
from source_clone.progress import create_progress_tracker
from source_clone.refresh_worker import RefreshWorker
from source_clone.rich_status import create_status_manager
from source_clone.unified_discovery import discover_projects

if TYPE_CHECKING:
    from pathlib import Path

    from source_clone.models import CloneResult, Config, Project
    from source_clone.progress import ProgressTracker

logger = get_logger(__name__)

__all__ = ["CloneManager", "clone_repositories"]


def _project_needs_refresh(project: Project, target_path: Path) -> bool:
    """Decide whether an existing checkout of *project* needs refreshing.

    Fails safe: if the local SHA cannot be determined for any reason the
    repository is refreshed rather than silently left stale.
    """
    # If no metadata is available for this project, refresh it by default
    if not getattr(project, "metadata", None):
        logger.debug(
            "↻ %s: needs refresh (no metadata available for SHA comparison)",
            project.name,
        )
        return True

    try:
        local_sha = get_current_commit_sha(target_path)
        # Metadata is guaranteed to exist by the check above
        metadata = getattr(project, "metadata", {}) or {}
        remote_sha = metadata.get("latest_commit_sha")
        return decide_refresh_from_shas(project.name, local_sha, remote_sha)
    except Exception as e:
        # If we can't determine, add to refresh list to be safe
        logger.debug(f"? {project.name}: couldn't check SHA ({e}), will refresh")
        return True


def _analyze_refresh_needs(
    config: Config, repos_to_refresh: list[Project]
) -> tuple[list[Project], list[Project]]:
    """Split existing repositories into those needing a refresh and those not.

    For GitHub sources with metadata available, SHA comparison avoids
    unnecessary pulls.  Otherwise every repository is refreshed.

    Returns:
        Tuple of ``(repos_needing_refresh, repos_up_to_date)``.
    """
    # Determine which projects actually have metadata available
    projects_with_metadata = [
        project for project in repos_to_refresh if getattr(project, "metadata", None)
    ]
    projects_missing_metadata = [
        project
        for project in repos_to_refresh
        if not getattr(project, "metadata", None)
    ]
    if projects_missing_metadata:
        logger.debug(
            "Metadata not available for %d repositories; "
            "they will be refreshed without SHA comparison",
            len(projects_missing_metadata),
        )

    repos_needing_refresh: list[Project] = []
    repos_up_to_date: list[Project] = []
    if (
        config.source_type == SourceType.GITHUB
        and repos_to_refresh
        and projects_with_metadata
    ):
        logger.debug("Checking which repositories need refresh using SHA comparison")
        for project in repos_to_refresh:
            target_path = config.path / project.filesystem_path
            if _project_needs_refresh(project, target_path):
                repos_needing_refresh.append(project)
            else:
                repos_up_to_date.append(project)
    else:
        # For non-GitHub or when metadata not available, refresh all
        repos_needing_refresh = repos_to_refresh

    logger.debug(
        f"Refresh analysis: {len(repos_needing_refresh)} need refresh, {len(repos_up_to_date)} up-to-date"
    )
    return repos_needing_refresh, repos_up_to_date


def _refresh_repositories(
    config: Config, repos_needing_refresh: list[Project]
) -> list[CloneResult]:
    """Refresh the repositories that SHA analysis flagged as stale."""
    logger.debug(
        f"Refreshing {len(repos_needing_refresh)} repositories (use --no-refresh to skip)"
    )

    # Show progress message
    if not config.quiet:
        console = Console(stderr=True)
        console.print(f"🔄 Refreshing {len(repos_needing_refresh)} repositories...")

    # Create a RefreshWorker instance
    # RefreshWorker handles both Gerrit and GitHub repositories:
    # - Gerrit repos: Uses 'origin' remote (standard Gerrit convention)
    # - GitHub repos: Uses 'origin' remote (standard GitHub convention)
    # - Both: Supports SSH and HTTPS authentication methods
    # - GitHub: Token auth via HTTPS, SSH keys, or gh CLI
    # - Authentication is handled transparently via git config and environment
    #
    # Key parameters for cross-platform refresh:
    # - filter_gerrit_only=False: Process ALL repos (Gerrit + GitHub)
    # - prune=True: Remove stale remote-tracking branches
    # - auto_stash: Controlled by force_refresh flag
    # - strategy="merge": Safe default for both platforms
    refresh_worker = RefreshWorker(
        config=config,
        retry_policy=RetryPolicy(),
        timeout=config.clone_timeout,
        fetch_only=config.fetch_only,
        prune=True,
        skip_conflicts=config.skip_conflicts,
        auto_stash=config.force_refresh,
        strategy="merge",
        filter_gerrit_only=False,  # Refresh all repos including GitHub
        force=config.force_refresh,
        content_filters=config.content_filters,
    )

    return run_refresh_with_progress(config, refresh_worker, repos_needing_refresh)


def _handle_existing_repos(
    config: Config, repos_to_refresh: list[Project], started_at: datetime
) -> list[CloneResult]:
    """Produce results for repositories that already exist locally.

    They are refreshed unless ``--no-refresh`` was given, in which case
    they are simply recorded as already present.
    """
    if not repos_to_refresh:
        return []

    if not config.auto_refresh:
        # --no-refresh: Just mark as already exists
        logger.debug(
            "Skipping refresh for %d existing repositories (--no-refresh enabled)",
            len(repos_to_refresh),
        )
        return [
            build_already_exists_result(config, project, started_at)
            for project in repos_to_refresh
        ]

    # Smart refresh: check which repos actually need updating
    repos_needing_refresh, repos_up_to_date = _analyze_refresh_needs(
        config, repos_to_refresh
    )

    # Create results for up-to-date repos (verified but not refreshed)
    results = [
        build_verified_result(config, project, started_at)
        for project in repos_up_to_date
    ]

    # Only refresh repos that actually need it
    if repos_needing_refresh:
        results.extend(_refresh_repositories(config, repos_needing_refresh))
    return results


def _run_clone_pipeline(
    config: Config,
    progress_tracker: ProgressTracker | None,
    started_at: datetime,
) -> BatchResult:
    """Run discovery, refresh, clone and retry for a single invocation."""
    announce_source_connection(config)

    projects, filter_stats = discover_projects(config)

    # Display warnings if present
    if filter_stats.get("warnings"):
        for warning in filter_stats["warnings"]:
            logger.warning(warning)

    if not projects:
        logger.warning("No projects found to clone")
        return BatchResult(
            config=config,
            results=[],
            started_at=started_at,
            completed_at=datetime.now(UTC),
        )

    # Ensure output directory exists before starting operations
    config.path.mkdir(parents=True, exist_ok=True)

    # Create clone manager (needed for gap analysis)
    manager = CloneManager(config, progress_tracker)

    # Perform gap analysis - check which repos actually need cloning vs refreshing
    repos_needing_clone, repos_to_refresh = partition_existing_repos(config, projects)

    # Only show clone messages and progress if there are repos to clone
    if repos_needing_clone:
        announce_clone_start(config, len(repos_needing_clone), filter_stats)

    # Handle already-existing repos - refresh them unless --no-refresh
    results = _handle_existing_repos(config, repos_to_refresh, started_at)

    # Only clone repos that need cloning
    results.extend(clone_missing_repos(manager, config, repos_needing_clone))

    # Retry failed clones (but not failed refreshes)
    results = retry_failed_clones(
        manager, config, progress_tracker, results, repos_to_refresh
    )

    batch_result = BatchResult(
        config=config,
        results=results,
        started_at=started_at,
        completed_at=datetime.now(UTC),
    )

    _write_manifest(batch_result, config)

    _log_final_summary(batch_result, config)

    return batch_result


def clone_repositories(config: Config) -> BatchResult:
    """Clone all repositories from configured source (Gerrit or GitHub).

    Args:
        config: Configuration for clone operations

    Returns:
        BatchResult with operation details and results
    """
    started_at = datetime.now(UTC)

    # Check for existing clones and warn about config changes
    # Pass None for console - will be created internally if needed before progress tracker
    _check_existing_manifest(config, console=None)

    progress_tracker = create_progress_tracker(config)

    # Use status manager context for Rich status integration

    with create_status_manager(progress_tracker):
        try:
            return _run_clone_pipeline(config, progress_tracker, started_at)
        except KeyboardInterrupt:
            logger.warning("Clone operation interrupted by user")
            raise
        except Exception as e:
            logger.error(f"Clone operation failed: {e}")
            raise
