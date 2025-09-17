# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2025 Matthew Watkins <mwatkins@linuxfoundation.org>

"""Clone manager for coordinating bulk repository operations."""

from __future__ import annotations

import json
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import UTC, datetime

from gerrit_clone.gerrit_api import fetch_gerrit_projects
from gerrit_clone.logging import get_logger, set_progress_tracker, clear_progress_tracker
from gerrit_clone.models import BatchResult, CloneResult, CloneStatus, Config, Project
from gerrit_clone.progress import ProgressTracker, create_progress_tracker
from gerrit_clone.worker import CloneWorker

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

    def shutdown(self) -> None:
        """Signal shutdown to cancel ongoing operations."""
        self._shutdown_event.set()

    def clone_projects(self, projects: list[Project]) -> list[CloneResult]:
        """Clone multiple projects with progress tracking.

        Args:
            projects: Projects to clone

        Returns:
            List of clone results
        """
        if not projects:
            logger.info("No projects to clone")
            return []

        # Remove duplicates and sort by hierarchy to prevent conflicts
        unique_projects = self._remove_duplicates_and_order_hierarchically(projects)

        logger.info(f"Starting bulk clone of {len(unique_projects)} projects")

        if self.progress_tracker:
            self.progress_tracker.start(unique_projects)

        try:
            # Check if we should use chunked processing
            if self._should_use_chunked_processing(unique_projects):
                logger.info(
                    "Using chunked processing for better filesystem error adaptation"
                )
                return self._execute_chunked_clone(unique_projects)
            else:
                return self._execute_bulk_clone(unique_projects)
        finally:
            if self.progress_tracker:
                self.progress_tracker.stop()

    def _remove_duplicates_and_order_hierarchically(
        self, projects: list[Project]
    ) -> list[Project]:
        """Remove duplicate projects and order hierarchically to prevent conflicts.

        Args:
            projects: Input projects

        Returns:
            Unique projects ordered to avoid hierarchical conflicts
        """
        # Remove duplicates by project name
        seen = set()
        unique_projects = []
        for project in projects:
            if project.name not in seen:
                unique_projects.append(project)
                seen.add(project.name)

        if len(unique_projects) != len(projects):
            logger.info(
                f"Removed {len(projects) - len(unique_projects)} duplicate projects"
            )

        # Filter out parent directory projects that have child projects
        project_names = {p.name for p in unique_projects}
        filtered_projects = []
        removed_parents = []

        logger.debug(f"Starting hierarchical filtering with {len(unique_projects)} projects")
        logger.debug(f"Project names: {sorted(list(project_names))[:10]}...")  # Show first 10

        for project in unique_projects:
            # Check if this project has any children
            children = [
                child_name for child_name in project_names
                if child_name != project.name and child_name.startswith(project.name + "/")
            ]

            if children:
                removed_parents.append(project.name)
                logger.debug(f"Filtering out parent project: {project.name} (has {len(children)} children: {children[:3]}{'...' if len(children) > 3 else ''})")
            else:
                filtered_projects.append(project)
                if "/" in project.name:
                    logger.debug(f"Keeping nested project: {project.name}")
                else:
                    logger.debug(f"Keeping top-level project: {project.name}")

        if removed_parents:
            logger.info(f"Filtered out {len(removed_parents)} parent directory projects: {removed_parents[:5]}{'...' if len(removed_parents) > 5 else ''}")
        else:
            logger.debug("No parent directory projects found to filter")

        unique_projects = filtered_projects

        # Light hierarchical ordering: only separate top-level from nested projects
        def hierarchy_key(project: Project) -> tuple[bool, str]:
            path_parts = project.name.split("/")
            # Sort by: depth first (1-level vs multi-level), then name
            is_nested = len(path_parts) > 1
            return (is_nested, project.name)

        ordered_projects = sorted(unique_projects, key=hierarchy_key)

        if ordered_projects != unique_projects:
            logger.debug("Applied light hierarchical ordering")

        return ordered_projects

    def _should_use_chunked_processing(self, projects: list[Project]) -> bool:
        """Determine if chunked processing should be used.

        Args:
            projects: Projects to process

        Returns:
            True if chunked processing should be used
        """
        project_count = len(projects)

        # Only use chunked processing for very large batches or constrained CI
        if project_count > 200:
            return True
        if os.environ.get("GITHUB_ACTIONS") == "true" and project_count > 50:
            return True
        return os.environ.get("CI") == "true" and project_count > 100

    def _execute_chunked_clone(self, projects: list[Project]) -> list[CloneResult]:
        """Execute clone in chunks to manage filesystem stress.

        Args:
            projects: Projects to clone

        Returns:
            Combined results from all chunks
        """
        chunk_size = 25
        thread_count = self.config.effective_threads
        all_results = []

        # Process in chunks
        total_chunks = (len(projects) + chunk_size - 1) // chunk_size
        logger.info(f"💾 Disk space: {self._get_disk_space_info()}")
        logger.info(
            f"Processing {len(projects)} projects in chunks of {chunk_size} with initial {thread_count} threads"
        )

        for chunk_idx in range(total_chunks):
            start_idx = chunk_idx * chunk_size
            end_idx = min(start_idx + chunk_size, len(projects))
            chunk_projects = projects[start_idx:end_idx]

            logger.info(
                f"Processing chunk {chunk_idx + 1}/{total_chunks} with {len(chunk_projects)} projects"
            )

            # Execute chunk with current thread count
            chunk_results = self._execute_bulk_clone(chunk_projects)
            all_results.extend(chunk_results)

            # Check if we should adapt thread count based on errors
            error_rate = sum(
                1 for r in chunk_results if r.status == CloneStatus.FAILED
            ) / len(chunk_results)
            if error_rate > 0.1 and thread_count > 1:  # More than 10% failures
                new_thread_count = max(1, thread_count - 1)
                logger.warning(
                    f"High error rate ({error_rate:.1%}), reducing threads from {thread_count} to {new_thread_count}"
                )
                # Update config for next chunk
                thread_count = new_thread_count

            # Brief pause between chunks to let filesystem settle
            if chunk_idx < total_chunks - 1:
                time.sleep(1.0)

        return all_results

    def _get_disk_space_info(self) -> str:
        """Get disk space information for logging."""
        try:
            stat = os.statvfs(self.config.path_prefix)
            free_bytes = stat.f_frsize * stat.f_bavail
            total_bytes = stat.f_frsize * stat.f_blocks
            free_gb = free_bytes / (1024**3)
            used_percent = ((total_bytes - free_bytes) / total_bytes) * 100
            return f"{free_gb:.1f}GB free ({used_percent:.1f}% used)"
        except (OSError, AttributeError):
            return "unknown"

    def _get_filesystem_safe_thread_count(
        self, projects: list[Project], max_threads: int
    ) -> int:
        """Get filesystem-safe thread count based on conditions.

        Args:
            projects: Projects being processed
            max_threads: Maximum threads from config

        Returns:
            Safe thread count for filesystem operations
        """
        project_count = len(projects)

        # Use full thread count for most cases - hierarchical ordering prevents conflicts
        safe_count = max_threads

        # Only reduce threads in very constrained CI environments
        if os.environ.get("GITHUB_ACTIONS") == "true":
            # Use half threads in CI to balance speed vs filesystem conflicts
            safe_count = max(2, max_threads // 2)
        elif os.environ.get("CI") == "true":
            safe_count = max(3, (max_threads * 2) // 3)

        logger.debug(
            f"Selected {safe_count} threads for {project_count} projects (max: {max_threads})"
        )
        return safe_count

    def _execute_bulk_clone(self, projects: list[Project]) -> list[CloneResult]:
        """Execute bulk clone operation with proper thread management.

        Args:
            projects: Projects to clone

        Returns:
            List of clone results
        """
        if not projects:
            return []

        logger.info("ENTERED _execute_bulk_clone method")

        results = []

        # Ensure output directory exists before starting
        self.config.path_prefix.mkdir(parents=True, exist_ok=True)

        # Use filesystem-safe thread count
        max_threads = self.config.effective_threads
        thread_count = self._get_filesystem_safe_thread_count(projects, max_threads)

        logger.info(f"Starting clone operations with {thread_count} threads")
        logger.info(f"About to create ThreadPoolExecutor with {thread_count} workers")

        with ThreadPoolExecutor(
            max_workers=thread_count, thread_name_prefix="clone"
        ) as executor:
            # Submit all clone tasks
            logger.info(f"Submitting {len(projects)} clone tasks to thread pool")
            future_to_project = {
                executor.submit(self._clone_project_with_progress, project): project
                for project in projects
            }
            logger.info(
                f"All {len(future_to_project)} tasks submitted, waiting for completion"
            )

            # Add overall timeout to prevent hanging indefinitely
            # Use a generous timeout: individual timeout * 2 + buffer for all projects
            overall_timeout = (self.config.clone_timeout * 2) + 60
            logger.info(f"Setting overall operation timeout to {overall_timeout}s")

            # Collect results as they complete with timeout
            try:
                logger.info("Starting to wait for clone task completion...")
                for future in as_completed(future_to_project, timeout=overall_timeout):
                    logger.info("Clone task completed, processing result...")
                    if self._shutdown_event.is_set():
                        # Cancel remaining futures on shutdown
                        for remaining_future in future_to_project:
                            remaining_future.cancel()
                        break

                    project = future_to_project[future]

                    try:
                        result = future.result()
                        results.append(result)

                        # Update progress tracker
                        if self.progress_tracker:
                            self.progress_tracker.update_project_result(result)

                        # Log individual result
                        self._log_project_result(result)

                    except Exception as e:
                        logger.error(f"Unexpected error cloning {project.name}: {e}")
                        # Create failed result for exception
                        error_result = CloneResult(
                            project=project,
                            status=CloneStatus.FAILED,
                            path=self.config.path_prefix / project.name,
                            attempts=0,
                            error_message=str(e),
                            started_at=datetime.now(UTC),
                            completed_at=datetime.now(UTC),
                        )
                        results.append(error_result)

                        if self.progress_tracker:
                            self.progress_tracker.update_project_result(error_result)

            except TimeoutError:
                logger.error(f"Clone operations timed out after {overall_timeout}s")
                # Cancel all remaining futures
                for future in future_to_project:
                    if not future.done():
                        future.cancel()
                        logger.warning(
                            f"Cancelled clone for {future_to_project[future].name}"
                        )

                # Create failed results for any incomplete projects
                for future, project in future_to_project.items():
                    if not future.done():
                        error_result = CloneResult(
                            project=project,
                            status=CloneStatus.FAILED,
                            path=self.config.path_prefix / project.name,
                            attempts=0,
                            error_message=f"Operation timed out after {overall_timeout}s",
                            started_at=datetime.now(UTC),
                            completed_at=datetime.now(UTC),
                        )
                        results.append(error_result)

                # Don't raise exception, return partial results
                logger.warning(
                    f"Returning {len(results)} partial results due to timeout"
                )

        return results

    def _clone_project_with_progress(self, project: Project) -> CloneResult:
        """Clone a project with progress updates.

        Args:
            project: Project to clone

        Returns:
            Clone result
        """
        logger.info(f"Starting clone task for project: {project.name}")
        logger.info(f"Calling worker.clone_project for: {project.name}")

        # Create a new worker instance for this task (thread safety)
        worker = CloneWorker(self.config)
        result = worker.clone_project(project)

        logger.info(f"Worker completed for {project.name} with status: {result.status}")
        return result

    def _log_project_result(self, result: CloneResult) -> None:
        """Log the result of a project clone operation.

        Args:
            result: Clone result to log
        """
        if result.status == CloneStatus.SUCCESS:
            logger.info(f"✓ Successfully cloned {result.project.name}")
        elif result.status == CloneStatus.ALREADY_EXISTS:
            logger.info(f"≈ Already exists {result.project.name}")
        elif result.status == CloneStatus.FAILED:
            error_summary = (
                result.error_message[:100] + "..."
                if result.error_message and len(result.error_message) > 100
                else result.error_message
            )
            logger.error(
                f"✗ Failed to clone {result.project.name} after {result.attempts} attempts: {error_summary}"
            )
        elif result.status == CloneStatus.SKIPPED:
            logger.info(f"↷ Skipped {result.project.name}")


def clone_repositories(config: Config) -> BatchResult:
    """Clone all repositories from Gerrit with hierarchical ordering.

    Args:
        config: Configuration for clone operations

    Returns:
        BatchResult with operation details and results
    """
    started_at = datetime.now(UTC)

    try:
        # Fetch projects from Gerrit
        logger.info(f"🌐 Connecting to Gerrit server {config.host}:{config.port}")
        projects = fetch_gerrit_projects(config)

        if not projects:
            logger.warning("No projects found to clone")
            return BatchResult(
                config=config,
                results=[],
                started_at=started_at,
                completed_at=datetime.now(UTC),
            )

        # Ensure output directory exists before starting operations
        config.path_prefix.mkdir(parents=True, exist_ok=True)

        # Log summary
        logger.info(
            f"📦 Found {len(projects)} projects - starting {config.effective_threads} workers"
        )

        # Initialize progress tracker with automatic environment detection
        logger.info("🚀 Initializing clone operations...")
        progress_tracker = create_progress_tracker(config)

        # Set up progress-aware logging integration
        set_progress_tracker(progress_tracker)

        # Create clone manager and execute
        manager = CloneManager(config, progress_tracker)
        try:
            results = manager.clone_projects(projects)
        finally:
            # Always clear progress tracker when done
            clear_progress_tracker()

        # Create batch result
        batch_result = BatchResult(
            config=config,
            results=results,
            started_at=started_at,
            completed_at=datetime.now(UTC),
        )

        # Write manifest file
        _write_manifest(batch_result, config)

        # Log final summary
        _log_final_summary(batch_result, config)

        return batch_result

    except KeyboardInterrupt:
        logger.warning("Clone operation interrupted by user")
        raise
    except Exception as e:
        logger.error(f"Clone operation failed: {e}")
        raise


def _write_manifest(batch_result: BatchResult, config: Config) -> None:
    """Write clone manifest to file.

    Args:
        batch_result: Batch result to write
        config: Configuration with manifest filename
    """
    manifest_path = config.path_prefix / config.manifest_filename

    try:
        manifest_data = batch_result.to_dict()

        with manifest_path.open("w", encoding="utf-8") as f:
            json.dump(manifest_data, f, indent=2, sort_keys=True)

        logger.info(f"Wrote clone manifest to [path]{manifest_path}[/path]")

    except Exception as e:
        logger.error(f"Failed to write manifest file: {e}")


def _log_final_summary(batch_result: BatchResult, config: Config) -> None:
    """Log final summary of clone operations.

    Args:
        batch_result: Batch result to summarize
        config: Configuration for quiet flag
    """
    duration_str = f"{batch_result.duration_seconds:.1f}s"

    if batch_result.failed_count == 0:
        # All successful
        logger.info(
            f"🎉 Clone completed successfully! "
            f"[green]{batch_result.success_count}[/green] repositories cloned "
            f"in {duration_str}"
        )
    else:
        # Some failures
        logger.warning(
            f"Clone completed with errors: "
            f"[green]{batch_result.success_count} succeeded[/green], "
            f"[red]{batch_result.failed_count} failed[/red]"
        )

    # Log success rate
    if batch_result.total_count > 0:
        success_rate = batch_result.success_rate
        logger.info(f"Success rate: {success_rate:.1f}%")

    # Log failed projects
    if batch_result.failed_count > 0 and not config.quiet:
        failed_results = [r for r in batch_result.results if r.failed]
        logger.error(
            f"Failed projects: {', '.join([r.project.name for r in failed_results])}"
        )

        logger.info("=== Clone Summary ===")
        logger.info(f"Duration: {duration_str}")
        logger.info(f"Total: {batch_result.total_count}")
        logger.info(f"Success: {batch_result.success_count}")
        logger.info(f"Failed: {batch_result.failed_count}")
        logger.info(f"Skipped: {batch_result.skipped_count}")

        if failed_results:
            logger.info("Failed projects:")
            for result in failed_results:
                logger.info(
                    f"  - {result.project.name}: {result.error_message or 'Unknown error'}"
                )

        # Set appropriate exit code for CI/CD
        if batch_result.failed_count > 0:
            logger.error(f"Clone completed with {batch_result.failed_count} failures")
