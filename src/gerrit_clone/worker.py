# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2025 Matthew Watkins <mwatkins@linuxfoundation.org>

"""Clone worker for individual repository operations."""

from __future__ import annotations

import subprocess
import time
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from gerrit_clone.clone_conflicts import resolve_path_conflict
from gerrit_clone.clone_diagnostics import analyze_clone_error, log_ssh_debug_output
from gerrit_clone.clone_git_env import (
    build_clone_environment,
    create_isolated_git_config,
    set_ssh_remote,
)
from gerrit_clone.clone_locking import _file_lock
from gerrit_clone.clone_nesting import (
    annotate_nested_parent,
    apply_nested_protection,
    find_project_git_ancestor,
    recheck_nested_ancestor,
    reject_nested_clone,
)
from gerrit_clone.clone_retry_policy import (
    calculate_adaptive_delay,
    is_filesystem_error_retryable,
    is_retryable_clone_error,
)
from gerrit_clone.clone_timeout import claim_new_target
from gerrit_clone.clone_utils import build_base_clone_command
from gerrit_clone.logging import get_logger
from gerrit_clone.models import CloneResult, CloneStatus, Config, Project
from gerrit_clone.pathing import check_path_conflicts, get_project_path
from gerrit_clone.subprocess_tracking import run_tracked

if TYPE_CHECKING:
    from pathlib import Path

# Re-exported so that `gerrit_clone.worker.<name>` keeps resolving after the
# supporting logic moved into sibling modules.
__all__ = [
    "CloneError",
    "CloneTimeoutError",
    "CloneWorker",
    "_file_lock",
]

logger = get_logger(__name__)


class CloneError(Exception):
    """Base exception for clone operations."""


class CloneTimeoutError(CloneError):
    """Raised when clone operation times out."""


class CloneWorker:
    """Worker for cloning individual repositories."""

    def __init__(self, config: Config, project_index: set[str] | None = None) -> None:
        """Initialize clone worker.

        Args:
            config: Configuration for clone operations
            project_index: Set of all project names (for accurate ancestor detection)
        """
        self.config = config
        self._project_index = project_index or set()
        # Track whether we attempted late ancestor detection
        self._late_nested_checks: int = 0

    def clone_project(self, project: Project) -> CloneResult:
        """Clone a single project repository.

        Note: With dependency-aware batching, parent/child conflicts are eliminated
        by architectural design, so complex locking is no longer needed.

        Args:
            project: Project to clone

        Returns:
            CloneResult with operation details
        """
        logger.debug(f"🔄 Processing {project.name}")
        target_path = get_project_path(project.name, self.config.path)
        started_at = datetime.now(UTC)

        result = CloneResult(
            project=project,
            status=CloneStatus.PENDING,
            path=target_path,
            started_at=started_at,
            first_started_at=started_at,
        )

        try:
            logger.debug(f"📁 Processing {project.name}")
            depth = project.name.count("/")

            ancestor_repo = find_project_git_ancestor(
                target_path, self.config.path, self._project_index
            )

            # Handle nested repositories (always clone both parent and children)
            allow_nested = getattr(self.config, "allow_nested_git", False)
            nested_protection = getattr(self.config, "nested_protection", False)

            if ancestor_repo and not allow_nested:
                return reject_nested_clone(result, ancestor_repo, started_at)

            if ancestor_repo and allow_nested:
                annotate_nested_parent(
                    result, ancestor_repo, self.config.path, project.name
                )
            elif depth > 0:
                logger.debug(
                    f"No early ancestor detected for candidate nested project {project.name} (depth={depth})"
                )

            is_nested = result.nested_under is not None
            conflict = check_path_conflicts(target_path, is_nested_repo=is_nested)
            if conflict is not None and resolve_path_conflict(
                conflict,
                result,
                started_at,
                getattr(self.config, "move_conflicting", True),
            ):
                return result

            # Ensure parent directories exist (safe due to dependency batching)
            target_path.parent.mkdir(parents=True, exist_ok=True)

            # If nested and protection enabled, add child path to parent exclude
            if ancestor_repo and allow_nested and nested_protection:
                apply_nested_protection(
                    ancestor_repo, target_path, project.name, result.nested_under
                )

            result.status = CloneStatus.CLONING

            # Instrumentation: mark potential nested candidate if depth > 0 and still no ancestor
            if depth > 0 and result.nested_under is None:
                logger.debug(
                    f"Nested candidate (no parent yet): {project.name} (will re-check before clone subprocess)"
                )

            # Perform clone with adaptive retry
            logger.debug(f"Starting clone execution for {project.name}")
            success = self._execute_adaptive_clone(project, target_path, result)
            logger.debug(
                f"Clone execution completed for {project.name}, success: {success}"
            )

            if success:
                result.status = CloneStatus.SUCCESS
                if ancestor_repo and allow_nested:
                    logger.debug(
                        f"📚 Nested clone succeeded: {project.name} (ancestor={ancestor_repo.name})"
                    )
            else:
                result.status = CloneStatus.FAILED
                if not result.error_message:
                    result.error_message = "Clone failed for unknown reason"

        except Exception as e:
            result.status = CloneStatus.FAILED
            result.error_message = str(e)
            logger.error(f"Failed to clone [project]{project.name}[/project]: {e}")

        finally:
            result.completed_at = datetime.now(UTC)
            result.duration_seconds = (result.completed_at - started_at).total_seconds()

        return result

    def _execute_adaptive_clone(
        self, project: Project, target_path: Path, result: CloneResult
    ) -> bool:
        """Execute clone with adaptive retry based on filesystem conditions.

        Args:
            project: Project to clone
            target_path: Target path for clone
            result: Result object to update

        Returns:
            True if clone succeeded, False otherwise
        """
        max_attempts = self.config.retry_policy.max_attempts

        for attempt in range(1, max_attempts + 1):
            try:
                success = self._perform_clone(project, target_path, result)
                if success:
                    return True

                # Clone failed - determine if we should retry
                error_msg = result.error_message or ""

                # Don't retry non-retryable errors
                if not self._is_filesystem_error_retryable(error_msg):
                    logger.error(
                        f"Non-retryable error for {project.name}: {error_msg[:100]}..."
                    )
                    return False

                # Calculate adaptive delay based on error type
                delay = self._calculate_adaptive_delay(attempt, error_msg)

                if attempt < max_attempts:
                    # Log at warning level for retryable failures (not final attempt)
                    logger.warning(
                        f"Retry clone {project.name} (attempt {attempt + 1}/{max_attempts}) after {delay:.2f}s: {error_msg[:100]}..."
                    )
                    time.sleep(delay)
                else:
                    # Final attempt failed - log at error level
                    logger.error(
                        f"Final retry failed for {project.name}: {error_msg[:100]}..."
                    )

            except Exception as e:
                result.error_message = str(e)
                logger.error(f"Unexpected error cloning {project.name}: {e}")
                return False

        return False

    def _is_filesystem_error_retryable(self, error_msg: str) -> bool:
        """Determine if a filesystem error should be retried."""
        return is_filesystem_error_retryable(error_msg)

    def _calculate_adaptive_delay(self, attempt: int, error_msg: str) -> float:
        """Calculate adaptive delay based on error type and attempt."""
        return calculate_adaptive_delay(attempt, error_msg)

    def _perform_clone(
        self, project: Project, target_path: Path, result: CloneResult
    ) -> bool:
        """Perform the actual clone operation with simple direct approach.

        Args:
            project: Project to clone
            target_path: Target path for clone
            result: Result object to update with attempt info

        Returns:
            True if clone succeeded, False otherwise

        Raises:
            CloneError: If clone fails with retryable error
            CloneTimeoutError: If clone times out
        """
        # Build clone command - clone directly to final path, let Git handle atomicity
        cmd = self._build_clone_command(project, target_path)
        env = self._build_clone_environment()

        result.attempts += 1
        logger.debug(
            f"⬇️ Cloning {project.name} (attempt {result.attempts}/{self.config.retry_policy.max_attempts})"
        )
        logger.debug(
            f"Cloning [project]{project.name}[/project] (attempt {result.attempts})"
        )
        logger.debug(f"Clone command: {' '.join(cmd)}")

        try:
            logger.debug(f"🔧 Executing git clone for {project.name}")
            logger.debug(f"Starting clone subprocess for {project.name}")
            start_time = datetime.now(UTC)

            if recheck_nested_ancestor(
                self.config,
                project.name,
                target_path,
                self._project_index,
                result,
            ):
                self._late_nested_checks += 1

            # Claimed as it is taken, so a timeout may discard what this
            # clone leaves behind; an existing destination belongs to
            # whoever put it there. Tracked so a batch that gives up can
            # terminate the child rather than wait for it.
            claim_new_target(target_path, project.name)
            process_result = run_tracked(
                cmd,
                timeout=self.config.clone_timeout,
                env=env,
                cwd=self.config.path,
            )
            log_ssh_debug_output(self.config, process_result)

            end_time = datetime.now(UTC)
            duration = (end_time - start_time).total_seconds()
            logger.debug(
                f"Clone subprocess completed for {project.name} in {duration:.1f}s"
            )

            if process_result.returncode == 0:
                # Set SSH remote if requested and we cloned with HTTPS
                if self.config.use_https and not self.config.keep_remote_protocol:
                    self._set_ssh_remote(project, target_path, env)

                logger.debug(f"✅ Successfully cloned {project.name}")
                logger.debug(f"Successfully cloned [project]{project.name}[/project]")
                return True

            # Clone failed - analyze error
            error_msg = self._analyze_clone_error(process_result, project.name)
            result.error_message = error_msg

            # Determine if error is retryable
            if self._is_retryable_clone_error(process_result):
                # Log at warning level for retryable errors (first phase)
                logger.warning(
                    f"Retryable clone error for [project]{project.name}[/project]: {error_msg}"
                )
                raise CloneError(error_msg)  # Will trigger retry

            logger.error(
                f"Non-retryable clone error for [project]{project.name}[/project]: {error_msg}"
            )
            return False

        except subprocess.TimeoutExpired:
            error_msg = f"Clone timeout after {self.config.clone_timeout}s"
            result.error_message = error_msg
            logger.warning(
                f"Clone timed out for {project.name} after {self.config.clone_timeout}s"
            )
            raise CloneTimeoutError(error_msg)

        except Exception as e:
            error_msg = f"Unexpected clone error: {e}"
            result.error_message = error_msg
            # Log at warning level since this error will trigger retry logic
            # Error level logging happens only after all retries are exhausted
            logger.warning(f"Unexpected subprocess error for {project.name}: {e}")
            raise CloneError(error_msg)

    def _build_clone_command(self, project: Project, target_path: Path) -> list[str]:
        """Build git clone command for project.

        Args:
            project: Project to clone
            target_path: Target clone path

        Returns:
            Git clone command as list of strings
        """
        # Build clone URL (HTTPS or SSH)
        if self.config.use_https:
            clone_url = self._build_https_url(project)
        else:
            clone_url = self._build_ssh_url(project)

        # Use shared utility to build base clone command
        return build_base_clone_command(clone_url, target_path, self.config)

    def _build_ssh_url(self, project: Project) -> str:
        """Build SSH URL for project.

        Args:
            project: Project to clone

        Returns:
            SSH clone URL
        """
        user_prefix = f"{self.config.ssh_user}@" if self.config.ssh_user else ""
        return (
            f"ssh://{user_prefix}{self.config.host}:{self.config.port}/{project.name}"
        )

    def _build_https_url(self, project: Project) -> str:
        """Build HTTPS URL for project.

        Args:
            project: Project to clone

        Returns:
            HTTPS clone URL
        """
        return f"{self.config.base_url}/{project.name}"

    def _set_ssh_remote(
        self, project: Project, repo_path: Path, env: dict[str, str]
    ) -> None:
        """Set the remote URL to SSH after HTTPS clone with isolated environment.

        Args:
            project: Project that was cloned
            repo_path: Path to the cloned repository
            env: Isolated git environment to use
        """
        set_ssh_remote(project.name, repo_path, self._build_ssh_url(project), env)

    def _create_isolated_git_config(self, config_dir: Path) -> None:
        """Create minimal git configuration in isolated directory."""
        create_isolated_git_config(config_dir)

    def _build_clone_environment(self) -> dict[str, str]:
        """Build environment variables for git clone."""
        return build_clone_environment(self.config)

    def _analyze_clone_error(
        self, process_result: subprocess.CompletedProcess[str], project_name: str
    ) -> str:
        """Analyze clone error and return descriptive message."""
        return analyze_clone_error(process_result, project_name, self.config)

    def _is_retryable_clone_error(
        self, process_result: subprocess.CompletedProcess[str]
    ) -> bool:
        """Check if a clone error is retryable."""
        return is_retryable_clone_error(process_result)

    def _format_duration(self, seconds: float) -> str:
        """Format duration in human-readable format.

        Args:
            seconds: Duration in seconds

        Returns:
            Formatted duration string
        """
        if seconds < 60:
            return f"{seconds:.1f}s"
        elif seconds < 3600:
            minutes = int(seconds / 60)
            return f"{minutes}m"
        else:
            hours = int(seconds / 3600)
            return f"{hours}h"
