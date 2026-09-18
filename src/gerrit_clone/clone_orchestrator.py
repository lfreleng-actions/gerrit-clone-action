# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""Concurrent execution engine for bulk clone operations.

Hosts :class:`CloneManager`, which owns the thread pool, the
dependency-safe batching loop and the per-future bookkeeping (progress
updates, ``--exit-on-error`` short-circuiting and overall timeouts).
"""

from __future__ import annotations

import threading
from typing import TYPE_CHECKING

from gerrit_clone.clone_batching import batch_should_stop, mark_batch_parents
from gerrit_clone.clone_cleanup import hold_back_running_claims
from gerrit_clone.clone_collection import consume_clone_futures
from gerrit_clone.clone_git_env import (
    isolated_git_config_scope,
    quarantine_isolated_git_configs,
)
from gerrit_clone.clone_ordering import (
    create_dependency_batches,
    get_filesystem_safe_thread_count,
    log_batch_preview,
    log_nested_summary,
    log_planning_summary,
    remove_duplicate_projects,
    topological_sort_projects,
)
from gerrit_clone.clone_reservations import release_claims
from gerrit_clone.clone_results import build_failure_result
from gerrit_clone.clone_timeout import record_timeout_results
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
        # Populated by clone_projects during planning.  Declared here
        # because retry_failed_clones drives the batching loop directly,
        # without that planning pass having run.
        self._project_name_index: set[str] = set()

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
            mark_batch_parents(
                batch,
                self._nested_candidates,
                self._nested_parent_usage,
                self._project_name_index,
            )

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

            if batch_should_stop(
                batch_results,
                batch_idx + 1,
                label,
                self.config.exit_on_error,
            ):
                break

            # No artificial sleep; proceed immediately to next batch
            # (Late ancestor detection logic in workers handles parent readiness)

        # Nested summary logging (after all batches complete)
        log_nested_summary(self._nested_candidates, self._nested_detected)
        return all_results

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
        future_to_project: dict[Future[CloneResult], Project] = {}
        # No worker does anything until every future has been recorded.
        # Submission is two steps -- submit() starts the task, the
        # mapping records it -- and a Ctrl+C in between leaves a worker
        # that may already hold a reservation but is invisible to the
        # hold-back, which would then release a path still being written
        # to.  Gating the start makes the pair atomic in the only sense
        # that matters: a worker either runs with its future recorded,
        # or does not run at all.  Kept local rather than on the
        # manager, which may be driving more than one batch.
        started = threading.Event()
        aborted = False

        def clone_when_started(project: Project) -> CloneResult:
            started.wait()
            if aborted:
                # Submission was interrupted, so this worker is not in
                # the batch's records.  Cloning would take a destination
                # nothing is left to account for or give back.
                return build_failure_result(
                    self.config,
                    project,
                    "Clone batch was interrupted during submission",
                )
            return self._clone_project_with_progress(project)

        try:
            with isolated_git_config_scope(), clone_pool as executor:
                generation = executor.generation
                # Submit all clone tasks
                logger.debug(f"Submitting {len(projects)} clone tasks to thread pool")
                try:
                    for project in projects:
                        future_to_project[
                            executor.submit(clone_when_started, project)
                        ] = project
                except BaseException:
                    # The gate is released either way, so nothing is
                    # left parked on it; the flag tells the woken
                    # workers to stand down rather than clone.
                    aborted = True
                    raise
                finally:
                    started.set()
                logger.debug(
                    f"All {len(future_to_project)} tasks submitted, waiting for completion"
                )

                # Add overall timeout to prevent hanging indefinitely
                # Use a generous timeout: individual timeout * 2 + buffer for all projects
                overall_timeout = (self.config.clone_timeout * 2) + 60
                logger.debug(f"Setting overall operation timeout to {overall_timeout}s")

                # Collect results as they complete with timeout
                try:
                    consume_clone_futures(
                        self.config,
                        self.progress_tracker,
                        self._shutdown_event,
                        future_to_project,
                        results,
                        overall_timeout,
                    )
                except TimeoutError:
                    # Cancelling futures only stops the queued ones; a clone
                    # already inside git keeps running, and leaving the
                    # block would wait for it. Abandon before reporting.
                    cancelled = executor.abandon()
                    # This timeout is caught, so the git config scope
                    # would otherwise see an ordinary exit and collect
                    # the directories that the workers surviving the
                    # settle wait are still using as HOME.
                    quarantine_isolated_git_configs()
                    logger.warning(
                        f"Abandoned {cancelled} queued clone(s) "
                        f"and stopped those already running"
                    )
                    self._handle_clone_timeout(
                        future_to_project, results, overall_timeout, generation
                    )
        finally:
            # Reservations belong to the batch that took them, and must
            # be given up even when the block is left by the Ctrl+C that
            # interruptible_executor re-raises.  That exit does not wait
            # for the workers, though, so any destination still being
            # written to is kept back here and released by the worker
            # holding it; otherwise a batch started by a caller that
            # caught the interrupt could take it mid-clone.
            hold_back_running_claims(self.config, future_to_project, generation)
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
