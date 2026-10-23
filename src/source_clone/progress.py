# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2025 Matthew Watkins <mwatkins@linuxfoundation.org>

"""Improved progress tracking with environment detection and fallbacks.

Owns :class:`ProgressTracker`, the thread-safe store of per-project clone
state, and selects the display mode for the current environment. Rendering,
Rich component lifecycle, summary statistics and result bookkeeping live in
the sibling ``progress_*`` modules and are re-exported here where they form
part of this module's public surface.
"""

from __future__ import annotations

import os
import re
import sys
import threading
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from rich.console import Console

from source_clone.logging import get_logger
from source_clone.models import CloneResult, CloneStatus, Config, Project
from source_clone.progress_display import (
    MAX_PROJECTS_FOR_TABLE,
    MIN_CONSOLE_WIDTH_FOR_TABLE,
    create_display,
    create_simple_progress_display,
)
from source_clone.progress_live import (
    fall_back_to_simple,
    initialize_rich_components,
    start_rich_periodic,
    start_rich_simple,
    start_text_mode,
    stop_display,
    update_display,
    update_progress_count,
)
from source_clone.progress_modes import ProgressMode
from source_clone.progress_results import (
    apply_status_timestamps,
    build_retry_result,
    create_initial_results,
)
from source_clone.progress_summary import (
    compute_summary,
    log_final_summary,
    log_periodic_summary,
)
from source_clone.rich_optional import RICH_AVAILABLE, Console

logger = get_logger(__name__)

__all__ = [
    "MAX_PROJECTS_FOR_TABLE",
    "MIN_CONSOLE_WIDTH_FOR_TABLE",
    "ProgressMode",
    "ProgressTracker",
    "create_progress_tracker",
    "create_simple_progress_display",
]


class ProgressTracker:
    """Environment-aware progress tracker with automatic fallbacks."""

    def __init__(
        self,
        config: Config,
        console: Any | None = None,
        force_mode: ProgressMode | None = None,
    ) -> None:
        """Initialize progress tracker with automatic environment detection.

        Args:
            config: Configuration for display options
            console: Optional Rich console instance
            force_mode: Force specific progress mode (for testing)
        """
        self.config = config
        self._lock = threading.Lock()
        self._projects: dict[str, Project] = {}
        self._results: dict[str, CloneResult] = {}
        self._start_time: datetime | None = None
        self._end_time: datetime | None = None
        self._current_log_message: str = ""
        self._log_message_lock = threading.Lock()

        # Type annotations for Rich components
        self.console: Any | None = None
        self._progress: Any | None = None
        self._live: Any | None = None
        self._main_task: Any | None = None
        # Display refresh pacing; reset when Rich components are initialized
        self._last_update_time = datetime.now(UTC)
        self._update_interval = 0.5

        # Determine progress mode
        self._mode = force_mode or self._detect_progress_mode()
        logger.debug(f"Progress tracker mode: {self._mode.value}")

        if self._mode in (ProgressMode.RICH_PERIODIC, ProgressMode.RICH_SIMPLE):
            if not RICH_AVAILABLE:
                logger.warning("Rich not available, falling back to text mode")
                self._mode = ProgressMode.TEXT_ONLY
                self.console = None
                self._progress = None
                self._live = None
            else:
                self.console = console or Console(
                    stderr=True,  # Use stderr to avoid interfering with piped output
                    force_terminal=self._mode == ProgressMode.RICH_PERIODIC,
                    force_interactive=self._mode == ProgressMode.RICH_PERIODIC,
                )
                initialize_rich_components(self)
        else:
            self.console = None
            self._progress = None
            self._live = None
        self._last_log_time = datetime.now(UTC)
        self._log_interval = 5.0  # Log summary every 5 seconds in text mode

    def _detect_progress_mode(self) -> ProgressMode:
        """Detect appropriate progress mode based on environment.

        Returns:
            Appropriate ProgressMode for current environment
        """
        # Check if progress is explicitly disabled
        if self.config.quiet:
            return ProgressMode.DISABLED

        # Check if Rich is available
        if not RICH_AVAILABLE:
            return ProgressMode.TEXT_ONLY

        if not sys.stderr.isatty():
            # Not a terminal - use simple mode or text only
            if os.environ.get("CI") or os.environ.get("GITHUB_ACTIONS"):
                return ProgressMode.TEXT_ONLY
            return ProgressMode.RICH_SIMPLE

        try:
            size = os.get_terminal_size()
            if size.columns < 80 or size.lines < 24:
                return ProgressMode.RICH_SIMPLE
        except OSError:
            return ProgressMode.RICH_SIMPLE

        non_interactive_vars = [
            "CI",
            "GITHUB_ACTIONS",
            "GITLAB_CI",
            "JENKINS_URL",
            "BUILD_NUMBER",
            "TEAMCITY_VERSION",
        ]
        if any(os.environ.get(var) for var in non_interactive_vars):
            return ProgressMode.RICH_SIMPLE

        # Default to periodic Rich mode with Live display
        return ProgressMode.RICH_PERIODIC

    def start(self, projects: list[Project]) -> None:
        """Start progress tracking for projects.

        Args:
            projects: List of projects to track
        """
        with self._lock:
            self._start_time = datetime.now(UTC)
            self._projects = {p.name: p for p in projects}
            self._results = create_initial_results(projects, self.config.path)

        if self._mode == ProgressMode.RICH_PERIODIC:
            start_rich_periodic(self, projects)
        elif self._mode == ProgressMode.RICH_SIMPLE:
            start_rich_simple(self, projects)
        elif self._mode == ProgressMode.TEXT_ONLY:
            start_text_mode(projects)
        # DISABLED mode does nothing

    def update_for_retry(self, retry_projects: list[Project]) -> None:
        """Update progress tracker for retry operations without resetting display.

        Args:
            retry_projects: List of projects to retry
        """
        with self._lock:
            # Reset only the retry projects' status to pending, keep existing results
            for project in retry_projects:
                if project.name in self._results:
                    self._results[project.name] = build_retry_result(
                        project, self._results[project.name]
                    )

            # Don't reset progress bar - keep existing total and continue from current state
            # The progress will be updated as retry operations complete

    def stop(self) -> None:
        """Stop progress tracking."""
        with self._lock:
            self._end_time = datetime.now(UTC)

        # Show final summary
        self._show_final_summary()
        if (
            self._mode in (ProgressMode.RICH_PERIODIC, ProgressMode.RICH_SIMPLE)
            and self._progress
        ):
            try:
                if RICH_AVAILABLE and hasattr(self._progress, "stop"):
                    self._progress.stop()
            except Exception as e:
                logger.warning(f"Error stopping progress display: {e}")

        # Always call stop_display for proper cleanup
        stop_display(self)

        if self._mode == ProgressMode.TEXT_ONLY:
            self._show_final_summary()

    def update_project_status(
        self, project_name: str, status: CloneStatus, error: str | None = None
    ) -> None:
        """Update project status.

        Args:
            project_name: Name of project
            status: New status
            error: Optional error message
        """
        with self._lock:
            if project_name not in self._results:
                return

            result = self._results[project_name]
            old_status = result.status
            result.status = status

            if error:
                result.error_message = error

            # Set timestamps
            apply_status_timestamps(result, status, datetime.now(UTC))

            if self._main_task and self._progress:
                if old_status == CloneStatus.PENDING and status in (
                    CloneStatus.SUCCESS,
                    CloneStatus.FAILED,
                    CloneStatus.SKIPPED,
                    CloneStatus.ALREADY_EXISTS,
                ):
                    update_progress_count(self)

        update_display(self)

        if self._mode == ProgressMode.TEXT_ONLY:
            self._log_project_status(project_name, status, error)

    def update_project_result(self, result: CloneResult) -> None:
        """Update complete project result.

        Args:
            result: Complete clone result
        """
        with self._lock:
            if result.project.name in self._results:
                self._results[result.project.name] = result
                update_progress_count(self)

        update_display(self)

    def _log_project_status(
        self, project_name: str, status: CloneStatus, error: str | None = None
    ) -> None:
        """Log project status change in text mode."""
        status_msg = f"Project {project_name}: {status.value}"
        if error:
            logger.error(f"{status_msg} - {error}")
        else:
            logger.debug(status_msg)

        # Periodic summary
        now = datetime.now(UTC)
        if (now - self._last_log_time).total_seconds() >= self._log_interval:
            log_periodic_summary(self._get_summary_unsafe())
            self._last_log_time = now

    def update_log_message(self, message: str) -> None:
        """Update the current log message displayed below progress.

        Args:
            message: New log message to display
        """
        with self._log_message_lock:
            self._current_log_message = message

        # Refresh display if using Live mode
        if self._mode == ProgressMode.RICH_PERIODIC and self._live and RICH_AVAILABLE:
            try:
                self._live.update(create_display(self))
            except Exception as e:
                logger.debug(f"Error updating live display: {e}")
                # If Live display fails, fall back to simple mode
                fall_back_to_simple(self)

    def set_status(self, message: str, temp: bool = False) -> None:  # noqa: ARG002
        """Set a status message that integrates with the progress display.

        Args:
            message: Status message to display
            temp: If True, message is temporary and will be replaced by next update
        """
        # Strip ANSI codes and emojis that might interfere with Rich formatting
        clean_message = re.sub(r"\x1b\[[0-9;]*m", "", message)
        clean_message = re.sub(r"[🌐🔍✅🚀🎉❌⚠️]", "", clean_message).strip()

        self.update_log_message(clean_message)

    def add_persistent_message(self, message: str) -> None:
        """Add a persistent message that stays visible.

        Args:
            message: Persistent message to add
        """
        # For now, just update the log message - could be enhanced later
        # to maintain a list of persistent messages
        self.set_status(message, temp=False)

    def clear_status(self) -> None:
        """Clear the current status message."""
        self.update_log_message("")

    def get_current_log_message(self) -> str:
        """Get the current log message.

        Returns:
            Current log message string
        """
        with self._log_message_lock:
            return self._current_log_message

    def _show_final_summary(self) -> None:
        """Log final summary."""
        log_final_summary(self.get_summary(), self._results)

    def get_results(self) -> list[CloneResult]:
        """Get all project results.

        Returns:
            List of clone results
        """
        with self._lock:
            return list(self._results.values())

    def get_summary(self) -> dict[str, Any]:
        """Get summary statistics.

        Returns:
            Dictionary with summary statistics
        """
        with self._lock:
            return self._get_summary_unsafe()

    def _get_summary_unsafe(self) -> dict[str, Any]:
        """Get summary without locking (internal use only)."""
        return compute_summary(self._results, self._start_time, self._end_time)


def create_progress_tracker(config: Config) -> ProgressTracker | None:
    """Create a progress tracker with automatic environment detection.

    Args:
        config: Configuration object

    Returns:
        ProgressTracker instance or None if disabled
    """
    if config.quiet:
        return None
    return ProgressTracker(config)
