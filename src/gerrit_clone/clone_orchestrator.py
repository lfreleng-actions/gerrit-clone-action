# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""Concurrent execution engine for bulk clone operations.

Hosts :class:`CloneManager`, which owns the thread pool, the
dependency-safe batching loop and the per-future bookkeeping (progress
updates, ``--exit-on-error`` short-circuiting and overall timeouts).
"""

from __future__ import annotations

import threading
from concurrent.futures import as_completed
from typing import TYPE_CHECKING

from gerrit_clone.clone_git_env import isolated_git_config_scope
from gerrit_clone.clone_ordering import (
    create_dependency_batches,
    get_filesystem_safe_thread_count,
    log_batch_preview,
    log_nested_summary,
    log_planning_summary,
    remove_duplicate_projects,
    topological_sort_projects,
)
from gerrit_clone.clone_reporting import log_project_result
from gerrit_clone.clone_results import build_failure_result
from gerrit_clone.clone_timeout import record_timeout_results, release_claims
from gerrit_clone.concurrent_utils import interruptible_executor
from gerrit_clone.github_worker import clone_github_repository
from gerrit_clone.logging import get_logger
from gerrit_clone.models import SourceType, filter_projects
from gerrit_clone.worker import CloneWorker

if TYPE_CHECKING:
    from concurrent.futures import Future

    from gerrit_clone.models import CloneResult, Config, Project
    from gerrit_clone.progress import ProgressTracker

logger = get_logger(__name__)


class CloneManager:
    """Manages bulk clone operations with progress tracking."""

    def __init__(
        self, config: Config, progress_tracker: ProgressTracker | None = None
    ) -> None:
        """Initialize clone manager.

        Args:
            config: Configuration for clone operations
            progress_tracker: Optional progress tracker for updates
        """
        self.config = config
        self.progress_tracker = progress_tracker
        self._shutdown_event = threading.Event()
        self._nested_candidates: set[str] = set()
        self._nested_detected: set[str] = set()
        self._nested_parent_usage: set[str] = set()

    def shutdown(self) -> None:
        """Signal shutdown to cancel ongoing operations."""
        self._shutdown_event.set()

    def _apply_project_filters(self, projects: list[Project]) -> list[Project]:
        """Apply include/exclude project filtering (supports wildcards)."""
        include_pats = getattr(self.config, "include_projects", None)
        exclude_pats = getattr(self.config, "exclude_projects", None)
        if not include_pats and not exclude_pats:
            return projects

        before_count = len(projects)
        filtered = filter_projects(
            projects,
            include_patterns=include_pats or None,
            exclude_patterns=exclude_pats or None,
        )
        after_count = len(filtered)
        filter_desc_parts: list[str] = []
        if include_pats:
            filter_desc_parts.append(f"include={sorted(include_pats)}")
        if exclude_pats:
            filter_desc_parts.append(f"exclude={sorted(exclude_pats)}")
        logger.debug(
            f"Project filter active: kept {after_count}/{before_count} projects "
            f"({', '.join(filter_desc_parts)})"
        )
        return filtered

    def clone_projects(self, projects: list[Project]) -> list[CloneResult]:
        """Clone multiple projects with progress tracking.

        Args:
            projects: Projects to clone

        Returns:
            List of clone results
        """
        if not projects:
            return []
        # Reset nested stats tracking for this clone operation
        self._nested_candidates.clear()
        self._nested_detected.clear()
        self._nested_parent_usage.clear()

        # Remove duplicates (fast operation)
        unique_projects = self._apply_project_filters(
            remove_duplicate_projects(projects)
        )

        self._project_name_index = {p.name for p in unique_projects}
        # Pre-compute depth for candidate nested tracking
        for p in unique_projects:
            if "/" in p.name:
                self._nested_candidates.add(p.name)

        log_planning_summary(unique_projects)

        has_filters = getattr(self.config, "include_projects", None) or getattr(
            self.config, "exclude_projects", None
        )
        logger.debug(
            f"Starting bulk clone of {len(unique_projects)} projects (project filter applied)"
            if has_filters
            else f"Starting bulk clone of {len(unique_projects)} projects"
        )

        if self.progress_tracker:
            self.progress_tracker.start(unique_projects)

        try:
            # Sort projects by dependencies - this handles all parent/child relationships
            dependency_ordered_projects = topological_sort_projects(unique_projects)

            # Use dependency-aware processing to prevent conflicts
            return self._execute_dependency_aware_clone(dependency_ordered_projects)
        finally:
            if self.progress_tracker:
                self.progress_tracker.stop()

    def _mark_batch_parents(self, batch: list[Project]) -> None:
        """Record which projects in *batch* are parents of nested repos."""
        # Mark parents in this batch (depth == 0 or any project with children)
        project_name_index: set[str] = getattr(self, "_project_name_index", set())
        batch_depth = batch[0].name.count("/") if batch else 0
        for pr in batch:
            prefix = pr.name + "/"
            if (
                any(cand.startswith(prefix) for cand in self._nested_candidates)
                and pr.name in project_name_index
            ):
                if pr.name not in self._nested_parent_usage and batch_depth == 0:
                    # First time we see this parent (top-level batch)
                    logger.debug(
                        f"👪 Parent ready for nesting: {pr.name} (children pending)"
                    )
                self._nested_parent_usage.add(pr.name)

        # Promote first few nested parents summary (only for top-level batch)
        if batch_depth == 0 and self._nested_parent_usage:
            sample_parents = sorted(self._nested_parent_usage)[:5]
            logger.debug(
                f"📂 Parent repositories prepared ({len(self._nested_parent_usage)}): {sample_parents}{' ...' if len(self._nested_parent_usage) > 5 else ''}"
            )

    def _batch_should_stop(
        self,
        batch_results: list[CloneResult],
        batch_number: int,
        label: str,
    ) -> bool:
        """Return ``True`` when ``--exit-on-error`` should halt batching.

        Args:
            batch_results: Results from the batch just completed.
            batch_number: 1-based index of that batch, named alone in the
                error so it reads as an ordinal rather than a fraction.
            label: ``"<number>/<total>"`` progress label for the debug line.
        """
        if not self.config.exit_on_error:
            return False
        failed_results = [r for r in batch_results if r.failed]
        if not failed_results:
            return False
        failed_project = failed_results[0]
        logger.error(
            f"🛑 Stopping after batch {batch_number}: {failed_project.project.name} failed with: {failed_project.error_message}"
        )
        logger.debug(f"📊 Processed {label} batches before stopping")
        return True

    def _execute_dependency_aware_clone(
        self, projects: list[Project]
    ) -> list[CloneResult]:
        """Execute clone operations with dependency-aware batching.

        This completely eliminates parent/child conflicts by processing
        projects in dependency-safe batches.

        Args:
            projects: Dependency-ordered projects

        Returns:
            List of clone results
        """
        if not projects:
            return []

        logger.debug("Starting dependency-aware clone execution")
        logger.debug(f"Total projects for batching: {len(projects)}")

        batches = create_dependency_batches(projects)
        all_results = []

        logger.debug(f"Created {len(batches)} dependency-safe batches")
        log_batch_preview(batches)

        for batch_idx, batch in enumerate(batches):
            label = f"{batch_idx + 1}/{len(batches)}"
            logger.debug(
                f"🔄 Processing batch {label} with {len(batch)} projects (sequential barrier before next batch)"
            )
            self._mark_batch_parents(batch)

            # Execute this batch (parallel inside batch)
            batch_results = self._execute_bulk_clone(batch)
            all_results.extend(batch_results)

            # Wait for batch to finish fully (already implied by synchronous call)
            # Add explicit barrier logging for clarity
            logger.debug(f"✅ Completed batch {label} ({len(batch_results)} results)")
            # Collect nested detections from results
            for r in batch_results:
                if getattr(r, "nested_under", None):
                    self._nested_detected.add(r.project.name)

            if self._batch_should_stop(batch_results, batch_idx + 1, label):
                break

            # No artificial sleep; proceed immediately to next batch
            # (Late ancestor detection logic in workers handles parent readiness)

        # Nested summary logging (after all batches complete)
        log_nested_summary(self._nested_candidates, self._nested_detected)
        return all_results

    def _record_future_result(
        self,
        future: Future[CloneResult],
        project: Project,
        results: list[CloneResult],
    ) -> bool:
        """Record one completed clone future.

        Returns:
            ``True`` when the caller should stop consuming further futures.
        """
        try:
            result = future.result()
            results.append(result)

            if self.progress_tracker:
                self.progress_tracker.update_project_result(result)

            log_project_result(result)

            if self.config.exit_on_error and result.failed:
                logger.error(
                    f"🛑 Exiting on error: {project.name} failed with: {result.error_message}"
                )
                return True

        except Exception as e:
            logger.error(f"Unexpected error cloning {project.name}: {e}")
            error_result = build_failure_result(self.config, project, str(e))
            results.append(error_result)

            if self.progress_tracker:
                self.progress_tracker.update_project_result(error_result)

            if self.config.exit_on_error:
                logger.error(
                    f"🛑 Exiting on error: {project.name} failed with exception: {e}"
                )
                return True

        return False

    def _consume_clone_futures(
        self,
        future_to_project: dict[Future[CloneResult], Project],
        results: list[CloneResult],
        overall_timeout: int,
    ) -> None:
        """Collect clone results as their futures complete."""
        logger.debug("Starting to wait for clone task completion...")
        for future in as_completed(future_to_project, timeout=overall_timeout):
            logger.debug("Clone task completed, processing result...")
            if self._shutdown_event.is_set():
                # Cancel remaining futures on shutdown
                for remaining_future in future_to_project:
                    remaining_future.cancel()
                break

            project = future_to_project[future]

            if self._record_future_result(future, project, results):
                # Cancel remaining futures
                for remaining_future in future_to_project:
                    if not remaining_future.done():
                        remaining_future.cancel()
                break

    def _handle_clone_timeout(
        self,
        future_to_project: dict[Future[CloneResult], Project],
        results: list[CloneResult],
        overall_timeout: int,
        generation: int | None = None,
    ) -> None:
        """Cancel outstanding clones and synthesise timeout results."""
        record_timeout_results(
            self.config,
            future_to_project,
            results,
            overall_timeout,
            generation,
        )

    def _execute_bulk_clone(self, projects: list[Project]) -> list[CloneResult]:
        """Execute bulk clone operation with proper thread management.

        Args:
            projects: Projects to clone

        Returns:
            List of clone results
        """
        if not projects:
            return []

        logger.debug("ENTERED _execute_bulk_clone method")

        results: list[CloneResult] = []

        # Ensure output directory exists before starting
        self.config.path.mkdir(parents=True, exist_ok=True)

        # Use filesystem-safe thread count
        max_threads = self.config.effective_threads
        thread_count = get_filesystem_safe_thread_count(projects, max_threads)

        logger.debug(f"Starting clone operations with {thread_count} threads")
        logger.debug(f"About to create ThreadPoolExecutor with {thread_count} workers")

        # Every clone pool is built here, including the ones
        # retry_failed_clones drives without going through
        # clone_projects, so this is where the workers' isolated git
        # config directories are scoped.  The scope is reference
        # counted, so a concurrent operation keeps its own.
        clone_pool = interruptible_executor(
            max_workers=thread_count, thread_name_prefix="clone"
        )
        generation: int | None = None
        try:
            with isolated_git_config_scope(), clone_pool as executor:
                generation = executor.generation
                # Submit all clone tasks
                logger.debug(f"Submitting {len(projects)} clone tasks to thread pool")
                future_to_project = {
                    executor.submit(self._clone_project_with_progress, project): project
                    for project in projects
                }
                logger.debug(
                    f"All {len(future_to_project)} tasks submitted, waiting for completion"
                )

                # Add overall timeout to prevent hanging indefinitely
                # Use a generous timeout: individual timeout * 2 + buffer for all projects
                overall_timeout = (self.config.clone_timeout * 2) + 60
                logger.debug(f"Setting overall operation timeout to {overall_timeout}s")

                # Collect results as they complete with timeout
                try:
                    self._consume_clone_futures(
                        future_to_project, results, overall_timeout
                    )
                except TimeoutError:
                    # Cancelling futures only stops the queued ones; a clone
                    # already inside git keeps running, and leaving the
                    # block would wait for it. Abandon before reporting.
                    cancelled = executor.abandon()
                    logger.warning(
                        f"Abandoned {cancelled} queued clone(s) "
                        f"and stopped those already running"
                    )
                    self._handle_clone_timeout(
                        future_to_project, results, overall_timeout, generation
                    )
        finally:
            # Reservations belong to the batch that took them, and
            # must be given up even when the block is left by the
            # Ctrl+C that interruptible_executor re-raises.
            release_claims(generation)

        return results

    def _clone_project_with_progress(self, project: Project) -> CloneResult:
        """Clone a project with progress updates.

        Args:
            project: Project to clone

        Returns:
            Clone result
        """
        logger.debug(f"Starting clone task for project: {project.name}")
        logger.debug(f"Calling worker.clone_project for: {project.name}")

        if self.progress_tracker:
            self.progress_tracker.update_log_message(f"Cloning {project.name}...")

        # Use appropriate clone method based on source type
        if self.config.source_type == SourceType.GITHUB:
            result = clone_github_repository(project, self.config)
        else:
            # Create a new worker instance for this task (thread safety)
            # Pass project index to worker for accurate ancestor detection
            worker = CloneWorker(self.config, project_index=self._project_name_index)
            result = worker.clone_project(project)

        logger.debug(
            f"Worker completed for {project.name} with status: {result.status}"
        )
        return result
