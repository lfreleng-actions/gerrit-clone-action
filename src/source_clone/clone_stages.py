# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""Stages of a full clone run, in the order they execute.

Announcing the connection, splitting discovered projects into "needs
cloning" versus "already present", running the clone itself and retrying
whatever failed.  Sequenced by
:func:`source_clone.clone_manager.clone_repositories`.
"""

from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING, Any, cast

from rich.console import Console

from source_clone.git_utils import is_git_repository
from source_clone.logging import get_logger, suppress_console_logging
from source_clone.models import SourceType
from source_clone.rich_status import (
    connecting_to_server,
    print_status_message,
    starting_clone,
)

if TYPE_CHECKING:
    from source_clone.clone_orchestrator import CloneManager
    from source_clone.models import CloneResult, Config, Project
    from source_clone.progress import ProgressTracker

logger = get_logger(__name__)


def announce_source_connection(config: Config) -> None:
    """Log and display which server the run is about to talk to."""
    if config.source_type == SourceType.GITHUB:
        logger.debug("Connecting to GitHub: %s", config.host)
        console = Console(stderr=True)
        print_status_message(f"🌐 Connecting to GitHub: {config.host}", console)
    else:
        # Port is guaranteed to be set for Gerrit sources (defaults to 29418)
        # Validated in Config.__post_init__ - use cast for type narrowing
        port = cast(int, config.port)  # noqa: TC006
        logger.debug("Connecting to Gerrit server %s:%s", config.host, port)
        connecting_to_server(config.host, port)


def partition_existing_repos(
    config: Config, projects: list[Project]
) -> tuple[list[Project], list[Project]]:
    """Split *projects* into those needing a clone and those already present.

    Returns:
        Tuple of ``(repos_needing_clone, repos_to_refresh)``.
    """
    repos_needing_clone: list[Project] = []
    repos_to_refresh: list[Project] = []

    for project in projects:
        target_path = config.path / project.filesystem_path
        # Check if repository already exists (both regular and bare repos)
        if target_path.exists() and is_git_repository(target_path):
            repos_to_refresh.append(project)
        else:
            repos_needing_clone.append(project)

    return repos_needing_clone, repos_to_refresh


def announce_clone_start(
    config: Config, repos_to_clone: int, filter_stats: dict[str, Any]
) -> None:
    """Log and display how many repositories are about to be cloned."""
    item_name = (
        "repositories" if config.source_type == SourceType.GITHUB else "projects"
    )

    if filter_stats["skipped"] > 0:
        logger.debug(
            "Cloning %d active %s with %d workers (skipping %d archived)",
            repos_to_clone,
            item_name,
            config.effective_threads,
            filter_stats["skipped"],
        )
        starting_clone(
            repos_to_clone,
            config.effective_threads,
            filter_stats["skipped"],
            item_name=item_name,
        )
    else:
        logger.debug(
            "Cloning %d %s with %d workers",
            repos_to_clone,
            item_name,
            config.effective_threads,
        )
        starting_clone(repos_to_clone, config.effective_threads, item_name=item_name)


def clone_missing_repos(
    manager: CloneManager, config: Config, repos_needing_clone: list[Project]
) -> list[CloneResult]:
    """Clone only the repositories that do not already exist locally."""
    if not repos_needing_clone:
        logger.debug("All repositories already exist - nothing to clone")
        return []

    # Suppress console logging during clone to prevent interference with Rich
    # Live display unless in verbose mode (users want all logs for debugging).
    # Progress tracker cleanup is handled by the status manager context.
    with suppress_console_logging(verbose=config.verbose):
        return manager.clone_projects(repos_needing_clone)


def _log_retry_outcome(
    failed_count: int, retry_succeeded: int, retry_still_failed: int
) -> None:
    """Report how many previously failed clones recovered on retry."""
    if retry_succeeded == 0:
        logger.warning(f"All {failed_count} retry attempts failed")
        console = Console(stderr=True)
        console.print(f"[red]✗ All {failed_count} retry attempts failed[/red]")
        return

    logger.debug(
        f"Retry successful: {retry_succeeded}/{failed_count} "
        f"previously failed clone(s) now succeeded"
    )
    console = Console(stderr=True)
    if retry_still_failed == 0:
        console.print(
            f"[green]✓ {retry_succeeded} failed clone(s) succeeded on retry[/green]"
        )
    else:
        console.print(
            f"[yellow]⚠ Retry results: {retry_succeeded} succeeded, {retry_still_failed} still failed[/yellow]"
        )


def retry_failed_clones(
    manager: CloneManager,
    config: Config,
    progress_tracker: ProgressTracker | None,
    results: list[CloneResult],
    repos_to_refresh: list[Project],
) -> list[CloneResult]:
    """Retry failed clones single-threaded and merge the new outcomes.

    Refresh failures are deliberately excluded: they should not be
    retried as clone operations.

    Returns:
        The result list with retried projects replaced by their new
        outcome; unchanged when nothing was retried.
    """
    repos_that_were_refreshed = {p.name for p in repos_to_refresh}
    failed_results = [
        r
        for r in results
        if r.failed and r.project.name not in repos_that_were_refreshed
    ]
    if not failed_results:
        return results

    # Always use single thread for retry to avoid SSH agent contention
    retry_threads = 1
    logger.debug(
        f"Retrying {len(failed_results)} failed clone(s) with single thread to avoid SSH agent contention"
    )
    logger.debug(
        f"Retrying {len(failed_results)} failed clone(s) with single thread..."
    )
    retry_projects = [r.project for r in failed_results]

    if progress_tracker:
        progress_tracker.update_for_retry(retry_projects)
        progress_tracker.update_log_message(
            f"🔄 Retrying {len(failed_results)} failed clone(s)..."
        )

    # Create a modified config with fewer threads using dataclass replace
    retry_config = replace(config, threads=retry_threads)

    # Reuse the existing manager and progress tracker for retries
    manager.config = retry_config
    retry_results = manager._execute_dependency_aware_clone(retry_projects)

    failed_names = {r.project.name for r in failed_results}
    final_results = [
        r for r in results if r.project.name not in failed_names
    ] + retry_results

    retry_succeeded = sum(1 for r in retry_results if r.success)
    _log_retry_outcome(
        len(failed_results), retry_succeeded, len(retry_results) - retry_succeeded
    )

    return final_results
