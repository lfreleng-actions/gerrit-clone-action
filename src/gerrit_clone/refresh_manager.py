# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2025 Matthew Watkins <mwatkins@linuxfoundation.org>

"""Manager for bulk repository refresh operations."""

from __future__ import annotations

import os
from concurrent.futures import as_completed
from datetime import UTC, datetime
from pathlib import Path

from rich.text import Text
from rich.progress import (
    BarColumn,
    MofNCompleteColumn,
    Progress,
    SpinnerColumn,
    TaskID,
    TextColumn,
    TimeElapsedColumn,
)

from gerrit_clone.concurrent_utils import interruptible_executor
from gerrit_clone.logging import get_logger
from gerrit_clone.models import Config, RefreshBatchResult, RefreshResult, RefreshStatus, RetryPolicy
from gerrit_clone.refresh_worker import RefreshWorker

logger = get_logger(__name__)


class RefreshManager:
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
        recursive: bool = True,
        include_projects: list[str] | None = None,
        exclude_projects: list[str] | None = None,
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
            recursive: Recursively discover repositories in subdirectories (default: True)
            include_projects: Optional list of project name patterns to include.
                Supports shell-style wildcards (*, ?, [seq]) and hierarchical
                matching.  Comma and space-separated values are accepted.
            exclude_projects: Optional list of project name patterns to exclude.
                Applied after include filters.  Same pattern syntax as
                include_projects.
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
        self.recursive = recursive
        self.include_projects = include_projects
        self.exclude_projects = exclude_projects

        # Determine thread count
        if threads is not None:
            self.threads = threads
        elif config is not None:
            self.threads = config.effective_threads
        else:
            # Default to CPU count * 4, capped at 32
            cpu_count = os.cpu_count() or 4
            self.threads = min(32, cpu_count * 4)

        logger.debug(f"RefreshManager initialized with {self.threads} threads")

    def discover_local_repositories(self, base_path: Path) -> list[Path]:
        """Discover all Git repositories under base_path.

        Args:
            base_path: Base directory to search

        Returns:
            Sorted list of repository root paths (sorted alphabetically for
            deterministic processing order and consistent progress display)
        """
        if not base_path.exists():
            raise ValueError(f"Base path does not exist: {base_path}")

        if not base_path.is_dir():
            raise ValueError(f"Base path is not a directory: {base_path}")

        logger.debug(f"🔍 Discovering Git repositories in {base_path}")

        repositories: list[Path] = []
        visited_repos: set[Path] = set()

        # Walk directory tree
        for root, dirs, files in os.walk(base_path):
            root_path = Path(root)

            # Check if current directory is a Git repository
            if ".git" in dirs:
                git_dir = root_path / ".git"

                # Verify it's a directory (not a file for submodules)
                if git_dir.is_dir():
                    # Normalize path
                    repo_path = root_path.resolve()

                    # Skip if we've already visited this repo
                    if repo_path in visited_repos:
                        continue

                    repositories.append(repo_path)
                    visited_repos.add(repo_path)

                    logger.debug(f"Found repository: {repo_path.name}")

                    if self.recursive:
                        # Continue searching subdirectories for Gerrit hierarchical projects
                        # In Gerrit, projects like ccsdk/apps, ccsdk/features are separate
                        # independent repos, not nested submodules within ccsdk
                        # We only skip .git directory itself
                        dirs[:] = [d for d in dirs if d != ".git"]
                    else:
                        # Non-recursive mode: don't descend into subdirectories
                        dirs[:] = []
                    continue

            # Skip hidden directories (except .git which we already handled)
            dirs[:] = [d for d in dirs if not d.startswith(".")]

        logger.debug(f"📂 Discovered {len(repositories)} Git repositories")

        # Sort repositories alphabetically for:
        # 1. Deterministic processing order across runs
        # 2. Better progress tracking (alphabetical display)
        # 3. Easier debugging and log analysis
        sorted_repos = sorted(repositories)

        # Apply include/exclude project filtering using relative paths
        # from base_path as project names (matching Gerrit's hierarchical
        # naming convention, e.g. "testsuite/pythonsdk-tests").
        if self.include_projects or self.exclude_projects:
            from gerrit_clone.models import match_project_pattern, normalize_project_list

            include_pats = normalize_project_list(self.include_projects) if self.include_projects else []
            exclude_pats = normalize_project_list(self.exclude_projects) if self.exclude_projects else []

            before_count = len(sorted_repos)
            filtered: list[Path] = []
            base_resolved = base_path.resolve()
            for repo_path in sorted_repos:
                # Derive a project name from the path relative to base_path
                # Use as_posix() for consistent forward-slash separators
                # matching Gerrit's hierarchical naming convention.
                try:
                    rel = repo_path.relative_to(base_resolved)
                    if rel == Path("."):
                        # repo is exactly at base_path; use directory
                        # name so filters can match it.
                        project_name = repo_path.name
                    else:
                        project_name = rel.as_posix()
                except ValueError:
                    # Fallback: use just the directory name
                    project_name = repo_path.name

                # Apply include filter (if specified, only keep matches)
                if include_pats:
                    if not any(match_project_pattern(project_name, p) for p in include_pats):
                        continue

                # Apply exclude filter
                if exclude_pats:
                    if any(match_project_pattern(project_name, p) for p in exclude_pats):
                        continue

                filtered.append(repo_path)

            after_count = len(filtered)
            filter_desc: list[str] = []
            if include_pats:
                filter_desc.append(f"include={sorted(include_pats)}")
            if exclude_pats:
                filter_desc.append(f"exclude={sorted(exclude_pats)}")
            logger.debug(
                f"Project filter: kept {after_count}/{before_count} repositories "
                f"({', '.join(filter_desc)})"
            )
            return filtered

        return sorted_repos

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

        logger.debug(f"🔄 Refreshing {len(repo_paths)} repositories with {self.threads} threads")

        if self.dry_run:
            logger.debug("🔍 DRY RUN MODE - no changes will be made")
            results = self._dry_run_refresh(repo_paths)
        else:
            # Execute parallel refresh
            results = self._execute_parallel_refresh(repo_paths)

        completed_at = datetime.now(UTC)

        batch_result = RefreshBatchResult(
            base_path=base_path,
            results=results,
            started_at=started_at,
            completed_at=completed_at,
        )

        return batch_result

    def _execute_parallel_refresh(self, repo_paths: list[Path]) -> list[RefreshResult]:
        """Execute refresh operations in parallel with progress tracking.

        Args:
            repo_paths: List of repository paths to refresh

        Returns:
            List of refresh results
        """
        results: list[RefreshResult] = []
        total = len(repo_paths)

        # Create worker
        worker = RefreshWorker(
            config=self.config,
            retry_policy=self.retry_policy,
            timeout=self.timeout,
            fetch_only=self.fetch_only,
            prune=self.prune,
            skip_conflicts=self.skip_conflicts,
            auto_stash=self.auto_stash,
            strategy=self.strategy,
            filter_gerrit_only=self.filter_gerrit_only,
            force=self.force,
        )

        # Create progress display with two-line layout
        # Line 1: Current repository being processed
        # Line 2: Progress bar + count + time
        from rich.console import Console, Group
        from rich.live import Live

        current_repo = Text("", style="bold blue")

        progress_bar = Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            BarColumn(bar_width=40),
            MofNCompleteColumn(),
            TextColumn("•"),
            TimeElapsedColumn(),
            transient=False,
        )
        task = progress_bar.add_task("Refreshing repositories", total=total)

        # Combine current repo and progress bar into a two-line display
        display_group = Group(current_repo, progress_bar)

        with Live(display_group, console=Console(), refresh_per_second=4, transient=False):
            # Create thread pool with graceful interrupt handling
            with interruptible_executor(
                max_workers=self.threads,
                thread_name_prefix="refresh",
            ) as executor:
                # Submit all tasks
                future_to_repo = {
                    executor.submit(worker.refresh_repository, repo): repo
                    for repo in repo_paths
                }

                # Process results as they complete
                for future in as_completed(future_to_repo):
                    repo = future_to_repo[future]

                    try:
                        result = future.result()
                        results.append(result)

                        # Update progress with status
                        self._update_progress(progress_bar, task, result, current_repo)

                        # Check for exit-on-error
                        if self.exit_on_error and result.failed:
                            logger.error(
                                f"❌ Exiting due to error in {result.project_name}"
                            )
                            # Cancel remaining tasks (only those not yet completed)
                            for f in future_to_repo.keys():
                                if not f.done():
                                    f.cancel()
                            break

                    except Exception as e:
                        # This shouldn't happen as worker catches all exceptions
                        # But just in case...
                        logger.error(f"❌ Unexpected error processing {repo.name}: {e}")
                        # Create failure result
                        failure_result = RefreshResult(
                            path=repo,
                            project_name=repo.name,
                            status=RefreshStatus.FAILED,
                            error_message=f"Unexpected error: {e}",
                            started_at=datetime.now(UTC),
                            completed_at=datetime.now(UTC),
                        )
                        results.append(failure_result)
                        progress_bar.update(task, advance=1)

                        if self.exit_on_error:
                            logger.error("❌ Exiting due to unexpected error")
                            # Cancel remaining tasks (only those not yet completed)
                            for f in future_to_repo.keys():
                                if not f.done():
                                    f.cancel()
                            break

        return results

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

            # Check Git repository
            if not worker._is_git_repository(repo_path):
                result.status = RefreshStatus.NOT_GIT_REPO
                result.error_message = "Not a Git repository"
            else:
                # Get remote URL
                remote_url = worker._get_remote_url(repo_path)
                result.remote_url = remote_url

                # Check if Gerrit
                if self.filter_gerrit_only and not worker._is_gerrit_repository(remote_url):
                    result.status = RefreshStatus.NOT_GERRIT_REPO
                    result.error_message = f"Not a Gerrit repository"
                else:
                    # Get repository state
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

            result.completed_at = datetime.now(UTC)
            result.duration_seconds = (result.completed_at - started_at).total_seconds()
            results.append(result)

            # Log dry run result
            status_emoji = self._get_status_emoji(result.status)
            logger.debug(f"{status_emoji} {project_name}: {result.status.value}")

        return results

    def _update_progress(
        self, progress: Progress, task: TaskID, result: RefreshResult, current_repo: Text
    ) -> None:
        """Update progress display based on result.

        Args:
            progress: Progress instance
            task: Task ID
            result: Refresh result
            current_repo: Text object for current repo display
        """
        # Get emoji for status
        status_emoji = self._get_status_emoji(result.status)

        # Update current repo text
        current_repo.plain = f"{status_emoji} {result.project_name}"

        # Update progress bar
        progress.update(task, advance=1)

    def _get_status_emoji(self, status: RefreshStatus) -> str:
        """Get emoji for refresh status.

        Args:
            status: Refresh status

        Returns:
            Emoji string
        """
        emoji_map = {
            RefreshStatus.SUCCESS: "✅",
            RefreshStatus.UP_TO_DATE: "✓",
            RefreshStatus.FAILED: "❌",
            RefreshStatus.SKIPPED: "⊘",
            RefreshStatus.CONFLICTS: "⚠️",
            RefreshStatus.NOT_GIT_REPO: "⊘",
            RefreshStatus.NOT_GERRIT_REPO: "⊘",
            RefreshStatus.UNCOMMITTED_CHANGES: "⚠️",
            RefreshStatus.DETACHED_HEAD: "⚠️",
            RefreshStatus.PENDING: "⏳",
            RefreshStatus.REFRESHING: "🔄",
        }

        return emoji_map.get(status, "•")


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
    recursive: bool = True,
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
        recursive: Recursively discover repositories in subdirectories (default: True)

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
        recursive=recursive,
    )

    return manager.refresh_repositories(base_path)
