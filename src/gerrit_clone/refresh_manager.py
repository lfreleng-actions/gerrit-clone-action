# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2025 Matthew Watkins <mwatkins@linuxfoundation.org>

"""Manager for bulk repository refresh operations.

This module holds the refresh policy: how a run is configured, whether it is a
dry run or a real one, and how the aggregated batch result is assembled. The
supporting mechanics live in two focused mixins:

* :mod:`gerrit_clone.refresh_discovery` — finding and filtering repositories
* :mod:`gerrit_clone.refresh_parallel` — thread-pool execution and progress

Their public names are re-exported here so ``gerrit_clone.refresh_manager``
remains the single import surface for bulk refresh behaviour.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from gerrit_clone.content_policy import PolicyReadError, recorded_policy
from gerrit_clone.logging import get_logger
from gerrit_clone.models import (
    Config,
    RefreshBatchResult,
    RefreshResult,
    RefreshStatus,
    RetryPolicy,
)
from gerrit_clone.refresh_discovery import RepositoryDiscoveryMixin
from gerrit_clone.refresh_parallel import ParallelRefreshMixin
from gerrit_clone.refresh_worker import RefreshWorker
from gerrit_clone.url_credentials import redact_url

if TYPE_CHECKING:
    from pathlib import Path

    from gerrit_clone.content_spec import ContentFilterSpec

logger = get_logger(__name__)

__all__ = ["RefreshManager", "RefreshWorker", "refresh_repositories"]


class RefreshManager(RepositoryDiscoveryMixin, ParallelRefreshMixin):
    """Manager for bulk repository refresh operations."""

    def __init__(
        self,
        config: Config | None = None,
        retry_policy: RetryPolicy | None = None,
        timeout: int = 300,
        fetch_only: bool = False,
        prune: bool = True,
        skip_conflicts: bool = True,
        auto_stash: bool = False,
        strategy: str = "merge",
        filter_gerrit_only: bool = True,
        threads: int | None = None,
        exit_on_error: bool = False,
        dry_run: bool = False,
        force: bool = False,
        force_hard: bool = False,
        recursive: bool = True,
        include_projects: list[str] | None = None,
        exclude_projects: list[str] | None = None,
        content_filters: ContentFilterSpec | None = None,
    ) -> None:
        """Initialize refresh manager.

        Args:
            config: Optional configuration for Git operations
            retry_policy: Retry policy for transient errors
            timeout: Timeout for each git operation in seconds
            fetch_only: Only fetch changes without merging
            prune: Prune deleted remote branches
            skip_conflicts: Skip repositories with uncommitted changes
            auto_stash: Automatically stash uncommitted changes
            strategy: Git pull strategy ('merge' or 'rebase')
            filter_gerrit_only: Only refresh repositories with Gerrit remotes
            threads: Number of concurrent threads (None = auto-detect)
            exit_on_error: Exit immediately on first error
            dry_run: Show what would be refreshed without executing
            force: Force refresh by fixing detached HEAD, upstream tracking, and stashing changes
            force_hard: Superset of force that also hard-resets each repository's
                default branch to upstream, discarding local commits/divergence
            recursive: Recursively discover repositories in subdirectories (default: True)
            include_projects: Optional list of project name patterns to include.
                Supports shell-style wildcards (*, ?, [seq]) and hierarchical
                matching.  Comma and space-separated values are accepted.
            exclude_projects: Optional list of project name patterns to exclude.
                Applied after include filters.  Same pattern syntax as
                include_projects.
            content_filters: The content filters the caller applies to
                what is refreshed, if any; see :class:`RefreshWorker`.
        """
        self.config = config
        self.retry_policy = retry_policy or RetryPolicy()
        self.timeout = timeout
        self.fetch_only = fetch_only
        self.prune = prune
        self.skip_conflicts = skip_conflicts
        self.auto_stash = auto_stash
        self.strategy = strategy
        self.filter_gerrit_only = filter_gerrit_only
        self.exit_on_error = exit_on_error
        self.dry_run = dry_run
        self.force = force
        self.force_hard = force_hard
        self.recursive = recursive
        self.include_projects = include_projects
        self.exclude_projects = exclude_projects
        self.content_filters = content_filters

        # Determine thread count
        if threads is not None:
            self.threads = threads
        elif config is not None:
            self.threads = config.effective_threads
        else:
            # Default to CPU count * 4, capped at 32, then halved (floor of 1)
            # to reduce concurrent SSH handshakes against Gerrit and avoid
            # transient "Could not read from remote repository" throttling.
            cpu_count = os.cpu_count() or 4
            self.threads = max(1, min(32, cpu_count * 4) // 2)

        logger.debug(f"RefreshManager initialized with {self.threads} threads")

    def refresh_repositories(
        self, base_path: Path, repo_paths: list[Path] | None = None
    ) -> RefreshBatchResult:
        """Refresh multiple repositories in parallel.

        Args:
            base_path: Base directory (for reporting)
            repo_paths: Optional list of specific repos to refresh
                       (if None, discovers all repos in base_path)

        Returns:
            RefreshBatchResult with aggregated results
        """
        started_at = datetime.now(UTC)

        # Discover repositories if not provided
        if repo_paths is None:
            repo_paths = self.discover_local_repositories(base_path)

        if not repo_paths:
            logger.warning("⚠️ No repositories found to refresh")
            return RefreshBatchResult(
                base_path=base_path,
                results=[],
                started_at=started_at,
                completed_at=datetime.now(UTC),
            )

        logger.debug(
            f"🔄 Refreshing {len(repo_paths)} repositories with {self.threads} threads"
        )

        if self.dry_run:
            logger.debug("🔍 DRY RUN MODE - no changes will be made")
            results = self._dry_run_refresh(repo_paths)
        else:
            results = self._execute_parallel_refresh(repo_paths)

        completed_at = datetime.now(UTC)

        batch_result = RefreshBatchResult(
            base_path=base_path,
            results=results,
            started_at=started_at,
            completed_at=completed_at,
        )

        return batch_result

    def _dry_run_refresh(self, repo_paths: list[Path]) -> list[RefreshResult]:
        """Perform dry run - just check repository status.

        Dry run mode ensures no repository modifications occur by:
        - Setting fetch_only=True (no merges/rebases)
        - Disabling auto_stash (no stash operations)
        - Disabling force mode (no HEAD fixes or upstream changes)

        Args:
            repo_paths: List of repository paths

        Returns:
            List of refresh results (status only, no actual refresh)
        """
        results: list[RefreshResult] = []

        # Explicit safeguards: ensure dry-run never modifies repository state
        worker = RefreshWorker(
            config=self.config,
            retry_policy=self.retry_policy,
            timeout=self.timeout,
            fetch_only=True,  # Dry run is fetch-only (no merges/rebases)
            prune=self.prune,
            skip_conflicts=self.skip_conflicts,
            auto_stash=False,  # Never stash in dry run
            strategy=self.strategy,
            filter_gerrit_only=self.filter_gerrit_only,
            force=False,  # Never force modifications in dry run
            force_hard=False,  # Never hard-reset in dry run
            content_filters=self.content_filters,
        )

        for repo_path in repo_paths:
            started_at = datetime.now(UTC)
            project_name = repo_path.name

            # Just check if it's a valid Git repo with Gerrit remote
            result = RefreshResult(
                path=repo_path,
                project_name=project_name,
                status=RefreshStatus.PENDING,
                started_at=started_at,
            )

            self._inspect_for_dry_run(worker, repo_path, result)

            result.completed_at = datetime.now(UTC)
            result.duration_seconds = (result.completed_at - started_at).total_seconds()
            results.append(result)

            status_emoji = self._get_status_emoji(result.status)
            logger.debug(f"{status_emoji} {project_name}: {result.status.value}")

        return results

    def _inspect_for_dry_run(
        self, worker: RefreshWorker, repo_path: Path, result: RefreshResult
    ) -> None:
        """Record what a refresh would do to a repository, without doing it.

        Args:
            worker: Dry-run-configured worker used for read-only probes
            repo_path: Repository path
            result: Result object to update
        """
        if not worker._is_git_repository(repo_path):
            result.status = RefreshStatus.NOT_GIT_REPO
            result.error_message = "Not a Git repository"
            return

        remote_url = worker._get_remote_url(repo_path)
        # Recorded in the manifest: never a credential.
        result.remote_url = redact_url(remote_url) if remote_url else None

        # Read first, as the refresh does: a config too broken to read the
        # policy hides the remotes too, and is a failure, not "not Gerrit".
        try:
            recorded = recorded_policy(repo_path)
        except PolicyReadError as exc:
            result.status = RefreshStatus.FAILED
            result.error_message = str(exc)
            return

        # Check if Gerrit
        if self.filter_gerrit_only and not worker._has_gerrit_remote(repo_path):
            result.status = RefreshStatus.NOT_GERRIT_REPO
            result.error_message = "Not a Gerrit repository"
            return

        bare = worker._is_bare_repository(repo_path)
        if worker._needs_staging(repo_path, recorded, bare=bare):
            refusal = worker._staged_refusal(repo_path, recorded, bare=bare)
            if refusal is None:
                result.status = RefreshStatus.SUCCESS
                result.error_message = "Would be refreshed and filtered again"
            else:
                result.status = RefreshStatus.SKIPPED
                result.error_message = refusal
            return

        if bare:
            # No working tree to be dirty or detached: a bare repository
            # is refreshable exactly when a fetch would update it.
            obstacle = worker._bare_refresh_obstacle(repo_path)
            if obstacle is None:
                result.status = RefreshStatus.SUCCESS
                result.error_message = "Would be refreshed"
            else:
                result.status = RefreshStatus.SKIPPED
                result.error_message = obstacle
            return

        state = worker._check_repository_state(repo_path)
        result.current_branch = state.get("branch")
        result.detached_head = state.get("detached_head", False)
        result.had_uncommitted_changes = state.get("has_uncommitted", False)

        if result.detached_head:
            result.status = RefreshStatus.DETACHED_HEAD
        elif result.had_uncommitted_changes:
            result.status = RefreshStatus.UNCOMMITTED_CHANGES
        else:
            result.status = RefreshStatus.SUCCESS
            result.error_message = "Would be refreshed"


def refresh_repositories(
    base_path: Path,
    config: Config | None = None,
    timeout: int = 300,
    fetch_only: bool = False,
    prune: bool = True,
    skip_conflicts: bool = True,
    auto_stash: bool = False,
    strategy: str = "merge",
    filter_gerrit_only: bool = True,
    threads: int | None = None,
    include_projects: list[str] | None = None,
    exclude_projects: list[str] | None = None,
    exit_on_error: bool = False,
    dry_run: bool = False,
    force: bool = False,
    force_hard: bool = False,
    recursive: bool = True,
    content_filters: ContentFilterSpec | None = None,
) -> RefreshBatchResult:
    """Refresh repositories in a directory.

    Convenience function for simple refresh operations.

    Args:
        base_path: Base directory to search for repositories
        config: Optional configuration for Git operations
        timeout: Timeout for each git operation in seconds
        fetch_only: Only fetch changes without merging
        prune: Prune deleted remote branches
        skip_conflicts: Skip repositories with uncommitted changes
        auto_stash: Automatically stash uncommitted changes
        strategy: Git pull strategy ('merge' or 'rebase')
        filter_gerrit_only: Only refresh repositories with Gerrit remotes
        threads: Number of concurrent threads (None = auto-detect)
        include_projects: Optional list of project name patterns to include
        exclude_projects: Optional list of project name patterns to exclude
        exit_on_error: Exit immediately on first error
        dry_run: Show what would be refreshed without executing
        force: Force refresh by fixing detached HEAD, upstream tracking, and stashing changes
        force_hard: Superset of force that also hard-resets each repository's
            default branch to upstream, discarding local commits/divergence
        recursive: Recursively discover repositories in subdirectories (default: True)
        content_filters: The content filters the caller applies to what
            is refreshed, if any; see :class:`RefreshWorker`.

    Returns:
        RefreshBatchResult with aggregated results
    """
    manager = RefreshManager(
        config=config,
        timeout=timeout,
        fetch_only=fetch_only,
        prune=prune,
        skip_conflicts=skip_conflicts,
        auto_stash=auto_stash,
        strategy=strategy,
        filter_gerrit_only=filter_gerrit_only,
        threads=threads,
        include_projects=include_projects,
        exclude_projects=exclude_projects,
        exit_on_error=exit_on_error,
        dry_run=dry_run,
        force=force,
        force_hard=force_hard,
        recursive=recursive,
        content_filters=content_filters,
    )

    return manager.refresh_repositories(base_path)
