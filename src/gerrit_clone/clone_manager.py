# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2025 Matthew Watkins <mwatkins@linuxfoundation.org>

"""Clone manager for coordinating bulk repository operations."""

from __future__ import annotations

import json
import os
import threading
from concurrent.futures import as_completed
from datetime import UTC, datetime
from typing import Any

from typing import cast

from gerrit_clone.concurrent_utils import interruptible_executor
from gerrit_clone.git_utils import is_git_repository
from gerrit_clone.logging import get_logger, suppress_console_logging
from gerrit_clone.models import (
    BatchResult,
    CloneResult,
    CloneStatus,
    Config,
    Project,
    SourceType,
)
from gerrit_clone.progress import ProgressTracker, create_progress_tracker
from gerrit_clone.rich_status import (
    clone_completed,
    connecting_to_server,
    create_status_manager,
    starting_clone,
)
from gerrit_clone.rich_status import (
    success_rate as show_success_rate,
)
from gerrit_clone.unified_discovery import discover_projects
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
        # Initialize nested stats tracking
        self._nested_candidates: set[str] = set()
        self._nested_detected: set[str] = set()
        self._nested_parent_usage: set[str] = set()

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
            return []
        # Reset nested stats tracking for this clone operation
        self._nested_candidates.clear()
        self._nested_detected.clear()
        self._nested_parent_usage.clear()

        # Remove duplicates (fast operation)
        unique_projects = self._remove_duplicates(projects)

        # Apply include-project filtering (exact match) if configured
        if getattr(self.config, "include_projects", None):
            include_set = set(self.config.include_projects)
            before_count = len(unique_projects)
            unique_projects = [p for p in unique_projects if p.name in include_set]
            after_count = len(unique_projects)
            logger.debug(
                f"Inclusion filter active: kept {after_count}/{before_count} projects "
                f"({sorted(list(include_set))})"
            )

        # Update project name index early
        self._project_name_index = {p.name for p in unique_projects}
        # Pre-compute depth for candidate nested tracking
        for p in unique_projects:
            if "/" in p.name:
                self._nested_candidates.add(p.name)

        # Planning / hierarchy analysis pass (parent → direct child count)
        names = {p.name for p in unique_projects}
        parent_children: dict[str, int] = {}
        for parent in names:
            prefix = parent + "/"
            # Fast scan for direct descendants
            count = 0
            for candidate in names:
                if candidate != parent and candidate.startswith(prefix):
                    count += 1
            if count:
                parent_children[parent] = count

        parent_count = len(parent_children)
        total_direct_children = sum(parent_children.values())
        logger.debug(
            f"Planning summary: {len(unique_projects)} repositories; "
            f"{parent_count} parents with {total_direct_children} direct child mappings"
        )
        if parent_children:
            # Log a small deterministic sample for debugging
            sample_items = sorted(parent_children.items())[:5]
            logger.debug(f"Parent sample (up to 5): {sample_items}")

        logger.debug(
            f"Starting bulk clone of {len(unique_projects)} projects (include filter applied)"
            if getattr(self.config, "include_projects", None)
            else f"Starting bulk clone of {len(unique_projects)} projects"
        )

        if self.progress_tracker:
            self.progress_tracker.start(unique_projects)

        try:
            # Sort projects by dependencies - this handles all parent/child relationships
            dependency_ordered_projects = self._topological_sort_projects(
                unique_projects
            )

            # Use dependency-aware processing to prevent conflicts
            return self._execute_dependency_aware_clone(dependency_ordered_projects)
        finally:
            if self.progress_tracker:
                self.progress_tracker.stop()

    def _remove_duplicates(self, projects: list[Project]) -> list[Project]:
        """Remove duplicate projects by name.

        Args:
            projects: Input projects

        Returns:
            Unique projects
        """
        seen = set()
        unique_projects = []
        for project in projects:
            if project.name not in seen:
                unique_projects.append(project)
                seen.add(project.name)

        if len(unique_projects) != len(projects):
            logger.debug(
                f"Removed {len(projects) - len(unique_projects)} duplicate projects"
            )

        return unique_projects

    def _topological_sort_projects(self, projects: list[Project]) -> list[Project]:
        """Sort projects by dependencies using topological sort.

        This ensures parent projects are always processed before their children,
        completely eliminating directory conflicts during parallel processing.

        Args:
            projects: List of projects to sort

        Returns:
            Projects ordered by dependencies (parents before children)
        """
        project_map = {p.name: p for p in projects}
        project_names = set(project_map.keys())

        # Build dependency graph
        dependencies: dict[str, str] = {}  # child -> parent (only immediate parent)
        dependents: dict[str, set[str]] = {}  # parent -> set of children
        in_degree: dict[str, int] = {}  # project -> number of dependencies

        # Initialize in_degree for all projects
        for name in project_names:
            in_degree[name] = 0

        # Build dependency relationships
        for project_name in project_names:
            path_parts = project_name.split("/")
            # Find immediate parent dependency
            for i in range(len(path_parts) - 1, 0, -1):
                parent_path = "/".join(path_parts[:i])
                if parent_path in project_names:
                    # Found immediate parent
                    dependencies[project_name] = parent_path
                    in_degree[project_name] = 1

                    if parent_path not in dependents:
                        dependents[parent_path] = set()
                    dependents[parent_path].add(project_name)
                    break

        # Topological sort using Kahn's algorithm
        result = []
        queue = [name for name in project_names if in_degree[name] == 0]

        logger.debug(f"Dependency analysis: {len(dependencies)} dependencies found")
        if dependencies:
            sample_deps = dict(list(dependencies.items())[:3])
            logger.debug(f"Sample dependencies: {sample_deps}")

        while queue:
            current = queue.pop(0)
            result.append(project_map[current])

            # Process all dependents of current project
            for dependent in dependents.get(current, set()):
                in_degree[dependent] -= 1
                if in_degree[dependent] == 0:
                    queue.append(dependent)

        # Verify all projects were processed
        if len(result) != len(projects):
            remaining = [
                name for name in project_names if name not in {p.name for p in result}
            ]
            logger.error(f"Topological sort failed - remaining projects: {remaining}")
            # Add remaining projects (shouldn't happen with valid hierarchies)
            for name in remaining:
                result.append(project_map[name])

        dependency_count = sum(1 for p in result if "/" in p.name)
        logger.debug(
            f"Dependency ordering complete: {len(result)} projects, {dependency_count} have dependencies"
        )

        # Show sample of ordering for debugging
        if result:
            sample_order = [p.name for p in result[:10]]
            logger.debug(f"Dependency order sample (first 10): {sample_order}")

        return result

    def _create_dependency_batches(
        self, projects: list[Project]
    ) -> list[list[Project]]:
        """Create depth-based batches of projects for safe parallel cloning.

        Rationale:
          * We now deliberately allow parent + child (nested) repositories.
          * Dependency ordering alone collapsed into a single batch for most trees.
          * Grouping by hierarchical depth (slash count) gives:
              - Parents first (depth 0 / 1)
              - Immediate children next
              - Deeper descendants later
          * This reduces early I/O contention and improves log clarity.

        Args:
            projects: List of projects (already deduplicated)

        Returns:
            Ordered list of batches (each batch: projects of one depth level)
        """
        if not projects:
            return []

        # Build depth map
        depth_map: dict[int, list[Project]] = {}
        for p in projects:
            depth = p.name.count("/")
            depth_map.setdefault(depth, []).append(p)

        batches: list[list[Project]] = []
        for depth in sorted(depth_map.keys()):
            group = sorted(depth_map[depth], key=lambda pr: pr.name)
            batches.append(group)

        # Log summary
        logger.debug(
            f"Created {len(batches)} depth-based batches (min depth={min(depth_map.keys(), default=0)}, max depth={max(depth_map.keys(), default=0)})"
        )
        for idx, batch in enumerate(batches[:5]):  # Show up to first 5 batches
            sample = [p.name for p in batch[:4]]
            if len(batch) > 4:
                sample.append(f"... +{len(batch) - 4} more")
            logger.debug(
                f"Batch {idx + 1} (depth={batch[0].name.count('/') if batch else 'n/a'}): {len(batch)} projects -> {sample}"
            )

        return batches

    def _get_disk_space_info(self) -> str:
        """Get disk space information for logging."""
        try:
            stat = os.statvfs(self.config.path)
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
        """Get thread count based on CPU cores unless explicitly overridden.

        With dependency-aware batching, conflicts are eliminated by proper scheduling,
        so we can safely use the full CPU-based thread count.

        Args:
            projects: Projects being processed
            max_threads: Maximum threads from config (CPU-based unless user specified)

        Returns:
            Thread count for clone operations
        """
        project_count = len(projects)

        # Use the configured thread count (CPU-based unless user explicitly set it)
        safe_count = max_threads

        logger.debug(
            f"Using {safe_count} threads for {project_count} projects (dependency conflicts eliminated by scheduling)"
        )
        return safe_count

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

        # Create dependency-safe batches
        batches = self._create_dependency_batches(projects)
        all_results = []

        logger.debug(f"Created {len(batches)} dependency-safe batches")
        for i, batch in enumerate(batches[:3]):  # Show first 3 batches
            sample_names = [p.name for p in batch[:5]]
            if len(batch) > 5:
                sample_names.append(f"... +{len(batch) - 5} more")
            logger.debug(f"Batch {i + 1} sample: {sample_names}")

        for batch_idx, batch in enumerate(batches):
            logger.debug(
                f"🔄 Processing batch {batch_idx + 1}/{len(batches)} with {len(batch)} projects (sequential barrier before next batch)"
            )
            # Mark parents in this batch (depth == 0 or any project with children)
            project_name_index: set[str] = getattr(self, "_project_name_index", set())
            batch_depth = batch[0].name.count("/") if batch else 0
            announced_parents = 0
            for pr in batch:
                prefix = pr.name + "/"
                if any(cand.startswith(prefix) for cand in self._nested_candidates):
                    if pr.name in project_name_index:
                        if (
                            pr.name not in self._nested_parent_usage
                            and batch_depth == 0
                        ):
                            # First time we see this parent (top-level batch)
                            logger.debug(
                                f"👪 Parent ready for nesting: {pr.name} (children pending)"
                            )
                        self._nested_parent_usage.add(pr.name)

            # Promote first few nested parents summary (only for top-level batch)
            if batch_depth == 0 and self._nested_parent_usage:
                sample_parents = sorted(list(self._nested_parent_usage))[:5]
                logger.debug(
                    f"📂 Parent repositories prepared ({len(self._nested_parent_usage)}): {sample_parents}{' ...' if len(self._nested_parent_usage) > 5 else ''}"
                )

            # Execute this batch (parallel inside batch)
            batch_results = self._execute_bulk_clone(batch)
            all_results.extend(batch_results)

            # Wait for batch to finish fully (already implied by synchronous call)
            # Add explicit barrier logging for clarity
            logger.debug(
                f"✅ Completed batch {batch_idx + 1}/{len(batches)} ({len(batch_results)} results)"
            )
            # Collect nested detections from results
            for r in batch_results:
                if getattr(r, "nested_under", None):
                    self._nested_detected.add(r.project.name)

            # Check for exit-on-error between batches
            if self.config.exit_on_error:
                failed_results = [r for r in batch_results if r.failed]
                if failed_results:
                    failed_project = failed_results[0]
                    logger.error(
                        f"🛑 Stopping after batch {batch_idx + 1}: {failed_project.project.name} failed with: {failed_project.error_message}"
                    )
                    logger.debug(
                        f"📊 Processed {batch_idx + 1}/{len(batches)} batches before stopping"
                    )
                    break

            # No artificial sleep; proceed immediately to next batch
            # (Late ancestor detection logic in workers handles parent readiness)

        # Nested summary logging (after all batches complete)
        if hasattr(self, "_nested_candidates"):
            try:
                total_candidates = len(self._nested_candidates)
                detected_set: set[str] = getattr(self, "_nested_detected", set())
                detected = len(detected_set)
                if total_candidates:
                    if detected:
                        sample = sorted(list(detected_set))[:5]
                        logger.debug(
                            f"🧬 Nested repositories detected: {detected}/{total_candidates} "
                            f"(examples: {sample}{' ...' if detected > 5 else ''})"
                        )
                        # Undetected sample (potential missed nesting)
                        undetected = sorted(
                            list(self._nested_candidates - detected_set)
                        )
                        if undetected:
                            undet_sample = undetected[:5]
                            logger.debug(
                                f"🔍 Nested candidates without detected parent linkage: {len(undetected)} "
                                f"(examples: {undet_sample}{' ...' if len(undetected) > 5 else ''})"
                            )
                    else:
                        logger.debug(
                            f"🧬 No nested repositories detected out of {total_candidates} candidates"
                        )
            except Exception as e:
                logger.debug(f"Nested summary logging failed: {e}")
        return all_results

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

        results = []

        # Ensure output directory exists before starting
        self.config.path.mkdir(parents=True, exist_ok=True)

        # Use filesystem-safe thread count
        max_threads = self.config.effective_threads
        thread_count = self._get_filesystem_safe_thread_count(projects, max_threads)

        logger.debug(f"Starting clone operations with {thread_count} threads")
        logger.debug(f"About to create ThreadPoolExecutor with {thread_count} workers")

        with interruptible_executor(
            max_workers=thread_count, thread_name_prefix="clone"
        ) as executor:
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
                logger.debug("Starting to wait for clone task completion...")
                for future in as_completed(future_to_project, timeout=overall_timeout):
                    logger.debug("Clone task completed, processing result...")
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

                        # Check for exit-on-error
                        if self.config.exit_on_error and result.failed:
                            logger.error(
                                f"🛑 Exiting on error: {project.name} failed with: {result.error_message}"
                            )
                            # Cancel remaining futures
                            for remaining_future in future_to_project:
                                if not remaining_future.done():
                                    remaining_future.cancel()
                            break

                    except Exception as e:
                        logger.error(f"Unexpected error cloning {project.name}: {e}")
                        # Create failed result for exception
                        now = datetime.now(UTC)
                        error_result = CloneResult(
                            project=project,
                            status=CloneStatus.FAILED,
                            path=self.config.path / project.name,
                            attempts=0,
                            error_message=str(e),
                            started_at=now,
                            completed_at=now,
                            first_started_at=now,
                        )
                        results.append(error_result)

                        if self.progress_tracker:
                            self.progress_tracker.update_project_result(error_result)

                        # Check for exit-on-error on exceptions too
                        if self.config.exit_on_error:
                            logger.error(
                                f"🛑 Exiting on error: {project.name} failed with exception: {e}"
                            )
                            # Cancel remaining futures
                            for remaining_future in future_to_project:
                                if not remaining_future.done():
                                    remaining_future.cancel()
                            break

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
                        now = datetime.now(UTC)
                        error_result = CloneResult(
                            project=project,
                            status=CloneStatus.FAILED,
                            path=self.config.path / project.name,
                            attempts=0,
                            error_message=f"Operation timed out after {overall_timeout}s",
                            started_at=now,
                            completed_at=now,
                            first_started_at=now,
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
        logger.debug(f"Starting clone task for project: {project.name}")
        logger.debug(f"Calling worker.clone_project for: {project.name}")

        # Update status message to show current project being cloned
        if self.progress_tracker:
            self.progress_tracker.update_log_message(f"Cloning {project.name}...")

        # Use appropriate clone method based on source type
        if self.config.source_type == SourceType.GITHUB:
            from gerrit_clone.github_worker import clone_github_repository
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

    def _log_project_result(self, result: CloneResult) -> None:
        """Log the result of a project clone operation.

        Args:
            result: Clone result to log
        """
        if result.status == CloneStatus.SUCCESS:
            logger.debug(f"✓ Successfully cloned {result.project.name}")
        elif result.status == CloneStatus.ALREADY_EXISTS:
            logger.debug(f"≈ Already exists {result.project.name}")
        elif result.status == CloneStatus.FAILED:
            error_summary = (
                result.error_message[:100] + "..."
                if result.error_message and len(result.error_message) > 100
                else result.error_message
            )
            # Log at error level to ensure failures are visible in summaries and external monitoring
            logger.error(
                f"✗ Failed to clone {result.project.name} after {result.attempts} attempts: {error_summary}"
            )
        elif result.status == CloneStatus.SKIPPED:
            logger.debug(f"↷ Skipped {result.project.name}")


def _check_existing_manifest(config: Config, console: Any | None = None) -> dict[str, Any] | None:
    """Check for existing manifest and warn about configuration changes.

    Args:
        config: Current configuration
        console: Optional Rich console instance for display (created if None)

    Returns:
        Existing manifest data if found, None otherwise
    """
    manifest_path = config.path / config.manifest_filename

    if not manifest_path.exists():
        return None

    try:
        with open(manifest_path, "r") as f:
            manifest: dict[str, Any] = json.load(f)

        # Check for configuration mismatches
        warnings = []

        if "clone_config" in manifest:
            old_config = manifest["clone_config"]

            if old_config.get("use_gh_cli") != config.use_gh_cli:
                warnings.append(
                    f"Clone method changed: was {'gh CLI' if old_config.get('use_gh_cli') else 'git'}, "
                    f"now {'gh CLI' if config.use_gh_cli else 'git'}"
                )

            if old_config.get("use_https") != config.use_https:
                warnings.append(
                    f"Protocol changed: was {'HTTPS' if old_config.get('use_https') else 'SSH'}, "
                    f"now {'HTTPS' if config.use_https else 'SSH'}"
                )

            if old_config.get("depth") != config.depth:
                warnings.append(
                    f"Depth changed: was {old_config.get('depth') or 'full'}, "
                    f"now {config.depth or 'full'}"
                )

        if warnings:
            # Create console if not provided (ensures safe display timing)
            if console is None:
                from rich.console import Console
                console = Console(stderr=True)
            console.print("\n[yellow]⚠️  Configuration Changes Detected:[/yellow]")
            for warning in warnings:
                console.print(f"  • {warning}")
            console.print(
                "[yellow]Existing repositories will be skipped. "
                "To re-clone with new settings, remove the existing directory first.[/yellow]\n"
            )

        # Show summary of existing clone
        if manifest.get("already_exists", 0) > 0 or manifest.get("succeeded", 0) > 0:
            # Create console if not provided (ensures safe display timing)
            if console is None:
                from rich.console import Console
                console = Console(stderr=True)
            total_existing = manifest.get("succeeded", 0) + manifest.get("already_exists", 0)
            console.print(
                f"[cyan]ℹ️  Found {total_existing} existing repositories from previous clone[/cyan]\n"
            )

        return manifest

    except Exception as e:
        logger.debug(f"Could not read existing manifest: {e}")
        return None


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

    # Initialize progress tracker early for Rich status messages
    progress_tracker = create_progress_tracker(config)

    # Use status manager context for Rich status integration
    from gerrit_clone.rich_status import create_status_manager

    with create_status_manager(progress_tracker):
        try:
            # Fetch projects from configured source
            if config.source_type == SourceType.GITHUB:
                logger.debug("Connecting to GitHub: %s", config.host)
                from gerrit_clone.rich_status import print_status_message
                from rich.console import Console
                console = Console(stderr=True)
                print_status_message(f"🌐 Connecting to GitHub: {config.host}", console)
            else:
                # Port is guaranteed to be set for Gerrit sources (defaults to 29418)
                # Validated in Config.__post_init__ - use cast for type narrowing
                port = cast(int, config.port)
                logger.debug("Connecting to Gerrit server %s:%s", config.host, port)
                connecting_to_server(config.host, port)

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
            repos_needing_clone = []
            repos_to_refresh = []

            for project in projects:
                target_path = config.path / project.filesystem_path
                # Check if repository already exists (both regular and bare repos)
                if target_path.exists() and is_git_repository(target_path):
                    repos_to_refresh.append(project)
                else:
                    repos_needing_clone.append(project)

            # Only show clone messages and progress if there are repos to clone
            if repos_needing_clone:
                repos_to_clone = len(repos_needing_clone)
                item_name = "repositories" if config.source_type == SourceType.GITHUB else "projects"

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

            # Clone only repos that don't already exist
            # Suppress console logging during clone to prevent interference with Rich Live display
            # unless in verbose mode (users want to see all logs for debugging)
            results = []

            # Handle already-existing repos - refresh them unless --no-refresh
            if repos_to_refresh:
                if config.auto_refresh:
                    # Smart refresh: check which repos actually need updating
                    repos_needing_refresh = []
                    repos_up_to_date = []

                    # For GitHub, use SHA comparison to avoid unnecessary pulls
                    # Determine which projects actually have metadata available
                    projects_with_metadata = [
                        project for project in repos_to_refresh
                        if getattr(project, "metadata", None)
                    ]
                    projects_missing_metadata = [
                        project for project in repos_to_refresh
                        if not getattr(project, "metadata", None)
                    ]
                    if projects_missing_metadata:
                        logger.debug(
                            "Metadata not available for %d repositories; "
                            "they will be refreshed without SHA comparison",
                            len(projects_missing_metadata),
                        )
                    if (
                        config.source_type == SourceType.GITHUB
                        and repos_to_refresh
                        and projects_with_metadata
                    ):
                        logger.debug("Checking which repositories need refresh using SHA comparison")

                        for project in repos_to_refresh:
                            target_path = config.path / project.filesystem_path

                            # If no metadata is available for this project, refresh it by default
                            if not getattr(project, "metadata", None):
                                repos_needing_refresh.append(project)
                                logger.debug(
                                    "↻ %s: needs refresh (no metadata available for SHA comparison)",
                                    project.name,
                                )
                                continue

                            # Get local HEAD SHA
                            try:
                                from gerrit_clone.git_utils import get_current_commit_sha
                                local_sha = get_current_commit_sha(target_path)
                                # Metadata is guaranteed to exist by the check above (continue on line 873)
                                metadata = getattr(project, "metadata", {}) or {}
                                remote_sha = metadata.get('latest_commit_sha')

                                # Handle different SHA comparison scenarios
                                if not remote_sha and not local_sha:
                                    # Both None: Empty repository with no commits
                                    # No refresh needed since there's nothing to pull
                                    repos_up_to_date.append(project)
                                    logger.debug(f"✓ {project.name}: up-to-date (empty repository, no commits)")
                                elif remote_sha and local_sha and local_sha == remote_sha:
                                    # SHAs match: Repository is up to date
                                    repos_up_to_date.append(project)
                                    # Safe to slice here because we know local_sha is not None
                                    logger.debug(f"✓ {project.name}: up-to-date ({local_sha[:8]})")
                                else:
                                    # SHAs differ or one is missing: Needs refresh
                                    repos_needing_refresh.append(project)
                                    if not remote_sha:
                                        logger.debug(f"↻ {project.name}: needs refresh (no remote SHA available)")
                                    elif not local_sha:
                                        logger.debug(f"↻ {project.name}: needs refresh (no local SHA available)")
                                    else:
                                        logger.debug(
                                            f"↻ {project.name}: needs refresh (local: {local_sha[:8]}, remote: {remote_sha[:8]})"
                                        )
                            except Exception as e:
                                # If we can't determine, add to refresh list to be safe
                                logger.debug(f"? {project.name}: couldn't check SHA ({e}), will refresh")
                                repos_needing_refresh.append(project)
                    else:
                        # For non-GitHub or when metadata not available, refresh all
                        repos_needing_refresh = repos_to_refresh

                    logger.debug(
                        f"Refresh analysis: {len(repos_needing_refresh)} need refresh, {len(repos_up_to_date)} up-to-date"
                    )

                    # Create results for up-to-date repos (verified but not refreshed)
                    for project in repos_up_to_date:
                        target_path = config.path / project.filesystem_path
                        results.append(CloneResult(
                            project=project,
                            status=CloneStatus.VERIFIED,
                            path=target_path,
                            started_at=started_at,
                            completed_at=datetime.now(UTC),
                            duration_seconds=0.0,
                            was_refreshed=False,
                            refresh_had_updates=False,
                            refresh_commits_pulled=0,
                        ))

                    # Only refresh repos that actually need it
                    if repos_needing_refresh:
                        logger.debug(
                            f"Refreshing {len(repos_needing_refresh)} repositories (use --no-refresh to skip)"
                        )

                        # Show progress message
                        if not config.quiet:
                            from rich.console import Console
                            console = Console(stderr=True)
                            console.print(f"🔄 Refreshing {len(repos_needing_refresh)} repositories...")

                        # Refresh existing repositories using RefreshWorker
                        from gerrit_clone.refresh_worker import RefreshWorker
                        from gerrit_clone.models import RefreshStatus, RetryPolicy

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
                        )

                        # Refresh each repository with progress display
                        from rich.progress import Progress, SpinnerColumn, TextColumn, BarColumn, MofNCompleteColumn, TimeElapsedColumn
                        from rich.console import Console

                        with Progress(
                            SpinnerColumn(),
                            TextColumn("[progress.description]{task.description}"),
                            BarColumn(bar_width=40),
                            MofNCompleteColumn(),
                            TextColumn("•"),
                            TimeElapsedColumn(),
                            console=Console(stderr=True),
                            transient=False,
                        ) as progress:
                            task = progress.add_task(
                                f"Refreshing repositories...",
                                total=len(repos_needing_refresh)
                            )

                            for project in repos_needing_refresh:
                                target_path = config.path / project.filesystem_path
                                refresh_start = datetime.now(UTC)

                                # Update progress description
                                progress.update(task, description=f"Refreshing {project.name}")

                                try:
                                    # Refresh single repository using the worker
                                    refresh_result = refresh_worker.refresh_repository(target_path)

                                    # Convert RefreshResult to CloneResult
                                    # Check if refresh failed first
                                    if refresh_result.status == RefreshStatus.FAILED:
                                        clone_status = CloneStatus.FAILED
                                        error_message = f"Refresh failed: {refresh_result.error_message}"
                                    # Use VERIFIED if up-to-date, REFRESHED if changes were pulled
                                    elif refresh_result.was_behind:
                                        clone_status = CloneStatus.REFRESHED
                                        error_message = None
                                    else:
                                        clone_status = CloneStatus.VERIFIED
                                        error_message = None

                                    clone_result = CloneResult(
                                        project=project,
                                        status=clone_status,
                                        path=target_path,
                                        started_at=refresh_start,
                                        completed_at=datetime.now(UTC),
                                        duration_seconds=refresh_result.duration_seconds,
                                        was_refreshed=refresh_result.was_behind,
                                        refresh_had_updates=refresh_result.was_behind,
                                        refresh_commits_pulled=refresh_result.commits_pulled,
                                        error_message=error_message,
                                    )

                                    results.append(clone_result)

                                except Exception as e:
                                    # If refresh fails, create a failed result with clear context
                                    logger.warning(f"Failed to refresh {project.name}: {e}")
                                    results.append(CloneResult(
                                        project=project,
                                        status=CloneStatus.FAILED,
                                        path=target_path,
                                        started_at=refresh_start,
                                        completed_at=datetime.now(UTC),
                                        duration_seconds=(datetime.now(UTC) - refresh_start).total_seconds(),
                                        error_message=f"Refresh failed for {project.name}: {e}",
                                    ))

                                # Advance progress
                                progress.update(task, advance=1)
                else:
                    # --no-refresh: Just mark as already exists
                    logger.debug(
                        "Skipping refresh for %d existing repositories (--no-refresh enabled)",
                        len(repos_to_refresh)
                    )
                    for project in repos_to_refresh:
                        target_path = config.path / project.filesystem_path
                        results.append(CloneResult(
                            project=project,
                            status=CloneStatus.ALREADY_EXISTS,
                            path=target_path,
                            started_at=started_at,
                            completed_at=datetime.now(UTC),
                            duration_seconds=0.0,
                        ))

            # Only clone repos that need cloning
            if repos_needing_clone:
                with suppress_console_logging(verbose=config.verbose):
                    try:
                        clone_results = manager.clone_projects(repos_needing_clone)
                        results.extend(clone_results)
                    finally:
                        # Progress tracker cleanup handled by status manager context
                        pass
            else:
                logger.debug("All repositories already exist - nothing to clone")

            # Retry failed clones (but not failed refreshes)
            # Refresh failures should not be retried as clone operations
            repos_that_were_refreshed = {p.name for p in repos_to_refresh}
            failed_results = [
                r for r in results
                if r.failed and r.project.name not in repos_that_were_refreshed
            ]
            if failed_results:
                # Always use single thread for retry to avoid SSH agent contention
                retry_threads = 1
                logger.debug(
                    f"Retrying {len(failed_results)} failed clone(s) with single thread to avoid SSH agent contention"
                )
                logger.debug(
                    f"Retrying {len(failed_results)} failed clone(s) with single thread..."
                )
                retry_projects = [r.project for r in failed_results]

                # Update the progress tracker for retry operations
                if progress_tracker:
                    progress_tracker.update_for_retry(retry_projects)
                    progress_tracker.update_log_message(
                        f"🔄 Retrying {len(failed_results)} failed clone(s)..."
                    )

                # Create a modified config with fewer threads using dataclass replace
                from dataclasses import replace

                retry_config = replace(config, threads=retry_threads)

                # Reuse the existing manager and progress tracker for retries
                manager.config = retry_config
                retry_results = manager._execute_dependency_aware_clone(retry_projects)

                # Update results - replace failed with retry results
                failed_names = {r.project.name for r in failed_results}
                final_results = [
                    r for r in results if r.project.name not in failed_names
                ] + retry_results

                retry_succeeded = sum(1 for r in retry_results if r.success)
                retry_still_failed = len(retry_results) - retry_succeeded

                if retry_succeeded > 0:
                    logger.debug(
                        f"Retry successful: {retry_succeeded}/{len(failed_results)} "
                        f"previously failed clone(s) now succeeded"
                    )
                    from rich.console import Console

                    console = Console(stderr=True)
                    if retry_still_failed == 0:
                        console.print(
                            f"[green]✓ {retry_succeeded} failed clone(s) succeeded on retry[/green]"
                        )
                    else:
                        console.print(
                            f"[yellow]⚠ Retry results: {retry_succeeded} succeeded, {retry_still_failed} still failed[/yellow]"
                        )
                else:
                    logger.warning(f"All {len(failed_results)} retry attempts failed")
                    from rich.console import Console

                    console = Console(stderr=True)
                    console.print(
                        f"[red]✗ All {len(failed_results)} retry attempts failed[/red]"
                    )

                results = final_results

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
    manifest_path = config.path / config.manifest_filename

    try:
        manifest_data = batch_result.to_dict()

        with manifest_path.open("w", encoding="utf-8") as f:
            json.dump(manifest_data, f, indent=2, sort_keys=True)

        logger.debug(f"Wrote clone manifest to [path]{manifest_path}[/path]")

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
        logger.debug(
            "Clone completed successfully! %d repositories cloned in %s",
            batch_result.success_count,
            duration_str,
        )
        clone_completed(
            batch_result.success_count, batch_result.failed_count, duration_str
        )
    else:
        # Some failures
        logger.debug(
            "Clone completed with errors: %d succeeded, %d failed in %s",
            batch_result.success_count,
            batch_result.failed_count,
            duration_str,
        )
        clone_completed(
            batch_result.success_count, batch_result.failed_count, duration_str
        )

    # Show success rate with Rich status
    if batch_result.total_count > 0:
        success_rate_val = batch_result.success_rate
        logger.debug("Success rate: %.1f%%", success_rate_val)
        show_success_rate(success_rate_val, batch_result.failed_count)

    # Log failed projects
    if batch_result.failed_count > 0 and not config.quiet:
        failed_results = [r for r in batch_result.results if r.failed]
        logger.debug(
            "Failed projects: %s", ", ".join([r.project.name for r in failed_results])
        )

        logger.debug("=== Clone Summary ===")
        logger.debug("Duration: %s", duration_str)
        logger.debug("Total: %d", batch_result.total_count)
        logger.debug("Success: %d", batch_result.success_count)
        logger.debug("Failed: %d", batch_result.failed_count)
        logger.debug("Skipped: %d", batch_result.skipped_count)

        if failed_results:
            logger.debug("Failed projects:")
            for result in failed_results:
                logger.debug(
                    "  - %s: %s",
                    result.project.name,
                    result.error_message or "Unknown error",
                )

        # Set appropriate exit code for CI/CD
        if batch_result.failed_count > 0:
            logger.debug("Clone completed with %d failures", batch_result.failed_count)
