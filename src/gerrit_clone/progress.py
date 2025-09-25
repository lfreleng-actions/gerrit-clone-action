# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2025 Matthew Watkins <mwatkins@linuxfoundation.org>

"""Improved progress tracking with environment detection and fallbacks."""

from __future__ import annotations

import os
import sys
import threading
from datetime import UTC, datetime, timedelta
from enum import Enum
from typing import TYPE_CHECKING, Any

# Handle Rich imports with TYPE_CHECKING
if TYPE_CHECKING:
    from rich.console import Console, Group
    from rich.live import Live
    from rich.panel import Panel
    from rich.progress import Progress, TaskID
    from rich.table import Table
    from rich.text import Text

try:
    from rich.console import Console, Group
    from rich.live import Live
    from rich.panel import Panel
    from rich.progress import (
        BarColumn,
        MofNCompleteColumn,
        Progress,
        SpinnerColumn,
        TaskID,
        TextColumn,
    )
    from rich.table import Table
    from rich.text import Text

    RICH_AVAILABLE = True
except ImportError:
    RICH_AVAILABLE = False

from gerrit_clone.logging import get_logger
from gerrit_clone.models import CloneResult, CloneStatus, Config, Project

logger = get_logger(__name__)

# Display configuration constants
MAX_PROJECTS_FOR_TABLE = 30
MIN_CONSOLE_WIDTH_FOR_TABLE = 100


class ProgressMode(Enum):
    """Progress display modes."""

    RICH_PERIODIC = "rich_periodic"  # Rich UI with periodic updates (no Live)
    RICH_SIMPLE = "rich_simple"  # Simple Rich progress without Live
    TEXT_ONLY = "text_only"  # Plain text logging only
    DISABLED = "disabled"  # No progress display


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

        # Determine progress mode
        self._mode = force_mode or self._detect_progress_mode()
        logger.debug(f"Progress tracker mode: {self._mode.value}")

        # Initialize components based on mode
        if self._mode in (ProgressMode.RICH_PERIODIC, ProgressMode.RICH_SIMPLE):
            if not RICH_AVAILABLE:
                logger.warning("Rich not available, falling back to text mode")
                self._mode = ProgressMode.TEXT_ONLY
                self.console = None
                self._progress = None
                self._live = None
            else:
                from rich.console import Console

                self.console = console or Console(
                    stderr=True,  # Use stderr to avoid interfering with piped output
                    force_terminal=self._mode == ProgressMode.RICH_PERIODIC,
                    force_interactive=self._mode == ProgressMode.RICH_PERIODIC,
                )
                self._initialize_rich_components()
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

        # Check terminal capabilities
        if not sys.stderr.isatty():
            # Not a terminal - use simple mode or text only
            if os.environ.get("CI") or os.environ.get("GITHUB_ACTIONS"):
                return ProgressMode.TEXT_ONLY
            return ProgressMode.RICH_SIMPLE

        # Check terminal size
        try:
            size = os.get_terminal_size()
            if size.columns < 80 or size.lines < 24:
                return ProgressMode.RICH_SIMPLE
        except OSError:
            return ProgressMode.RICH_SIMPLE

        # Check environment variables that suggest non-interactive
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

    def _initialize_rich_components(self) -> None:
        """Initialize Rich components based on mode."""
        if not RICH_AVAILABLE or not self.console:
            return

        # Create progress bar
        from rich.progress import (
            BarColumn,
            MofNCompleteColumn,
            Progress,
            SpinnerColumn,
            TextColumn,
        )

        columns = [
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            BarColumn(),
            MofNCompleteColumn(),
            TextColumn("[progress.elapsed]{task.fields[elapsed]}"),
        ]

        self._progress = Progress(
            *columns,
            console=self.console,
            transient=self._mode == ProgressMode.RICH_SIMPLE,
        )

        # Initialize Live display for fixed progress area
        self._live = None
        self._last_update_time = datetime.now(UTC)
        self._update_interval = 0.5  # Update every 0.5 seconds for responsiveness

    def start(self, projects: list[Project]) -> None:
        """Start progress tracking for projects.

        Args:
            projects: List of projects to track
        """
        with self._lock:
            self._start_time = datetime.now(UTC)
            self._projects = {p.name: p for p in projects}
            self._results = {}

            # Initialize results
            for project in projects:
                target_path = self.config.path_prefix / project.name
                self._results[project.name] = CloneResult(
                    project=project,
                    status=CloneStatus.PENDING,
                    path=target_path,
                    started_at=None,
                    completed_at=None,
                    error_message=None,
                )

        # Start mode-specific display
        if self._mode == ProgressMode.RICH_PERIODIC:
            self._start_rich_periodic(projects)
        elif self._mode == ProgressMode.RICH_SIMPLE:
            self._start_rich_simple(projects)
        elif self._mode == ProgressMode.TEXT_ONLY:
            self._start_text_mode(projects)
        # DISABLED mode does nothing

    def _start_rich_periodic(self, projects: list[Project]) -> None:
        """Start Rich display with periodic updates (no Live interference)."""
        if not self._progress or not self.console:
            return

        try:
            # Create main progress task with elapsed time field
            self._main_task = self._progress.add_task(
                "Cloning repositories", total=len(projects), elapsed="0:00"
            )

            # Initialize Live display for fixed progress area
            from rich.live import Live

            display_content = self._create_display()
            self._live = Live(
                display_content,
                console=self.console,
                refresh_per_second=2,
                vertical_overflow="visible",
            )
            self._live.start()

            # Set initial log message
            self.update_log_message("Starting repository clone operations...")

        except Exception as e:
            logger.warning(f"Error starting Rich periodic display: {e}")
            # Ensure Live display is properly stopped if it was partially started
            if self._live:
                try:
                    self._live.stop()
                except Exception:
                    pass
                self._live = None
            # Fall back to simple mode
            self._mode = ProgressMode.RICH_SIMPLE
            self._start_rich_simple(projects)

    def _start_rich_simple(self, projects: list[Project]) -> None:
        """Start simple Rich progress bar."""
        if not self._progress:
            return

        try:
            # Create main progress task with elapsed time field
            self._main_task = self._progress.add_task(
                "Cloning repositories", total=len(projects), elapsed="0:00"
            )

            if RICH_AVAILABLE:
                self._progress.start()

        except Exception as e:
            logger.warning(f"Failed to start Rich simple display: {e}")
            # Fall back to text mode
            self._mode = ProgressMode.TEXT_ONLY
            self._start_text_mode(projects)

    def _start_text_mode(self, projects: list[Project]) -> None:
        """Start text-only progress logging."""
        logger.info(f"Starting clone of {len(projects)} repositories")

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

        # Always call _stop_display for proper cleanup
        self._stop_display()

        # Log final summary
        if self._mode == ProgressMode.TEXT_ONLY:
            self._show_final_summary()

    def _stop_display(self) -> None:
        """Stop and cleanup display components."""
        if self._live and RICH_AVAILABLE:
            try:
                self._live.stop()
            except Exception as e:
                logger.debug(f"Error stopping live display: {e}")
            finally:
                self._live = None

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
            now = datetime.now(UTC)
            if status == CloneStatus.CLONING and not result.started_at:
                result.started_at = now
            elif status in (
                CloneStatus.SUCCESS,
                CloneStatus.FAILED,
                CloneStatus.SKIPPED,
                CloneStatus.ALREADY_EXISTS,
            ):
                if not result.completed_at:
                    result.completed_at = now
                    if result.started_at:
                        result.duration_seconds = (
                            result.completed_at - result.started_at
                        ).total_seconds()

            # Update progress display
            if self._main_task and self._progress:
                if old_status == CloneStatus.PENDING and status in (
                    CloneStatus.SUCCESS,
                    CloneStatus.FAILED,
                    CloneStatus.SKIPPED,
                    CloneStatus.ALREADY_EXISTS,
                ):
                    # Update progress count
                    self._update_progress_count()

        # Update display after progress update
        self._update_display()

        # Log status change in text mode
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
                # Update progress count
                self._update_progress_count()

        self._update_display()

    def _update_progress_count(self) -> None:
        """Update the progress bar with current completion count and elapsed time."""
        if self._main_task and self._progress and RICH_AVAILABLE:
            if hasattr(self._progress, "update"):
                summary = self._get_summary_unsafe()

                # Calculate elapsed time
                if self._start_time:
                    elapsed_seconds = (datetime.now(UTC) - self._start_time).total_seconds()
                    elapsed_minutes, secs = divmod(int(elapsed_seconds), 60)
                    elapsed_hours, mins = divmod(elapsed_minutes, 60)
                    if elapsed_hours > 0:
                        elapsed_str = f"{elapsed_hours}:{mins:02d}:{secs:02d}"
                    else:
                        elapsed_str = f"{mins}:{secs:02d}"
                else:
                    elapsed_str = "0:00"

                self._progress.update(self._main_task, completed=summary["completed"], elapsed=elapsed_str)

    def _update_display(self) -> None:
        """Update the display based on current mode."""
        if self._mode == ProgressMode.RICH_PERIODIC and self._live and RICH_AVAILABLE:
            # Use Live display for real-time updates
            try:
                self._live.update(self._create_display())
                self._last_update_time = datetime.now(UTC)
            except Exception as e:
                logger.debug(f"Error updating live display: {e}")
                # If Live display fails, fall back to simple mode to prevent further issues
                try:
                    self._live.stop()
                except Exception:
                    pass
                self._live = None
                self._mode = ProgressMode.RICH_SIMPLE
        elif (
            self._mode == ProgressMode.RICH_PERIODIC and self.console and RICH_AVAILABLE
        ):
            # Fallback to periodic console updates for RICH_PERIODIC without Live
            now = datetime.now(UTC)
            if (now - self._last_update_time).total_seconds() >= self._update_interval:
                try:
                    self.console.print(self._create_display())
                    self._last_update_time = now
                except Exception as e:
                    logger.warning(f"Error updating periodic display: {e}")
        elif self._mode == ProgressMode.RICH_SIMPLE and self._progress and RICH_AVAILABLE:
            # Handle RICH_SIMPLE mode - just update the progress bar, no custom display
            if self._main_task:
                summary = self._get_summary_unsafe()
                try:
                    self._progress.update(self._main_task, completed=summary["completed"])
                except Exception as e:
                    logger.debug(f"Error updating simple progress: {e}")

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
            self._log_periodic_summary()
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
                self._live.update(self._create_display())
            except Exception as e:
                logger.debug(f"Error updating live display: {e}")
                # If Live display fails, fall back to simple mode
                try:
                    self._live.stop()
                except Exception:
                    pass
                self._live = None
                self._mode = ProgressMode.RICH_SIMPLE

    def set_status(self, message: str, temp: bool = False) -> None:
        """Set a status message that integrates with the progress display.

        Args:
            message: Status message to display
            temp: If True, message is temporary and will be replaced by next update
        """
        # Strip ANSI codes and emojis that might interfere with Rich formatting
        import re
        clean_message = re.sub(r'\x1b\[[0-9;]*m', '', message)
        clean_message = re.sub(r'[🌐🔍✅🚀🎉❌⚠️]', '', clean_message).strip()

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

    def _log_periodic_summary(self) -> None:
        """Log periodic summary in text mode."""
        summary = self._get_summary_unsafe()
        total = summary["total"]
        completed = (
            summary["success"]
            + summary["failed"]
            + summary["skipped"]
            + summary["already_exists"]
        )
        logger.debug(
            f"Progress: {completed}/{total} completed ({summary['cloning']} active, {summary['pending']} pending)"
        )

    def _show_final_summary(self) -> None:
        """Log final summary."""
        summary = self.get_summary()
        duration = self._format_duration(summary["duration"])

        logger.debug("=== Clone Summary ===")
        logger.debug(f"Duration: {duration}")
        logger.debug(f"Total: {summary['total']}")
        logger.debug(f"Success: {summary['success']}")
        logger.debug(f"Failed: {summary['failed']}")
        logger.debug(f"Skipped: {summary['skipped']}")
        logger.debug(f"Already exists: {summary['already_exists']}")

        if summary["failed"] > 0:
            logger.debug("Failed projects:")
            for result in self._results.values():
                if result.status == CloneStatus.FAILED:
                    error_msg = result.error_message or "Unknown error"
                    logger.debug(f"  - {result.project.name}: {error_msg}")

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
        success = sum(
            1 for r in self._results.values() if r.status == CloneStatus.SUCCESS
        )
        failed = sum(
            1 for r in self._results.values() if r.status == CloneStatus.FAILED
        )
        skipped = sum(
            1 for r in self._results.values() if r.status == CloneStatus.SKIPPED
        )
        already_exists = sum(
            1 for r in self._results.values() if r.status == CloneStatus.ALREADY_EXISTS
        )
        cloning = sum(
            1 for r in self._results.values() if r.status == CloneStatus.CLONING
        )
        pending = sum(
            1 for r in self._results.values() if r.status == CloneStatus.PENDING
        )

        total = len(self._results)
        completed = success + failed + skipped + already_exists

        # Calculate duration
        if self._start_time and self._end_time:
            duration = self._end_time - self._start_time
        elif self._start_time:
            duration = datetime.now(UTC) - self._start_time
        else:
            duration = timedelta(0)

        return {
            "total": total,
            "completed": completed,
            "success": success,
            "failed": failed,
            "skipped": skipped,
            "already_exists": already_exists,
            "cloning": cloning,
            "pending": pending,
            "duration": duration,
        }

    def _create_display(self) -> Any:
        """Create Rich display content."""
        if not RICH_AVAILABLE or not self.console:
            return ""

        summary = self._get_summary_unsafe()

        # Create status line
        status_parts = []
        if summary["success"] > 0:
            status_parts.append(f"[green]✓ {summary['success']}[/green]")
        if summary["failed"] > 0:
            status_parts.append(f"[red]✗ {summary['failed']}[/red]")
        if summary["already_exists"] > 0:
            status_parts.append(f"[yellow]≈ {summary['already_exists']}[/yellow]")
        if summary["skipped"] > 0:
            status_parts.append(f"[dim]⊘ {summary['skipped']}[/dim]")
        if summary["cloning"] > 0:
            status_parts.append(f"[blue]⬇ {summary['cloning']}[/blue]")
        if summary["pending"] > 0:
            status_parts.append(f"[dim]⏳ {summary['pending']}[/dim]")

        status_text = (
            " | ".join(status_parts) if status_parts else "[dim]No activity[/dim]"
        )

        # Create main content with progress bar
        content_parts: list[Any] = []
        if self._progress:
            # Create fresh progress bar with current values
            summary = self._get_summary_unsafe()
            from rich.progress import Progress, BarColumn, TextColumn, MofNCompleteColumn, SpinnerColumn

            # Calculate manual elapsed time to avoid reset
            if self._start_time:
                elapsed_seconds = (datetime.now(UTC) - self._start_time).total_seconds()
                elapsed_minutes, secs = divmod(int(elapsed_seconds), 60)
                elapsed_hours, mins = divmod(elapsed_minutes, 60)
                if elapsed_hours > 0:
                    elapsed_str = f"{elapsed_hours}:{mins:02d}:{secs:02d}"
                else:
                    elapsed_str = f"{mins}:{secs:02d}"
            else:
                elapsed_str = "0:00"

            fresh_progress = Progress(
                SpinnerColumn(),
                TextColumn("[progress.description]{task.description}"),
                BarColumn(),
                MofNCompleteColumn(),
                TextColumn("[progress.elapsed]{task.fields[elapsed]}"),
                expand=True,
            )
            task = fresh_progress.add_task("Cloning repositories", completed=summary["completed"], total=summary["total"], elapsed=elapsed_str)
            content_parts.append(fresh_progress)

        # Add project table if reasonable number of projects and terminal is wide enough
        if len(self._results) <= MAX_PROJECTS_FOR_TABLE and self.console and self.console.size.width > MIN_CONSOLE_WIDTH_FOR_TABLE:
            content_parts.append(self._create_project_table())

        # Combine content
        if len(content_parts) == 1:
            main_content = content_parts[0]
        else:
            from rich.console import Group

            main_content = Group(*content_parts)

        # Add log message line
        from rich.console import Group
        from rich.text import Text

        log_message = self.get_current_log_message()
        log_line = Text.from_markup(
            f"[dim]ℹ️  {log_message}[/dim]" if log_message else "[dim]Ready...[/dim]",
            overflow="ellipsis",
        )

        # Combine progress and log message
        if isinstance(main_content, Group):
            display_content = Group(main_content, "", log_line)
        else:
            display_content = Group(main_content, "", log_line)

        # Create panel with status
        from rich.panel import Panel

        return Panel(
            display_content,
            title="Repository Clone Progress",
            subtitle=status_text,
            border_style="blue",
        )

    def _create_project_table(self) -> Any:
        """Create table showing project status."""
        if not RICH_AVAILABLE:
            return ""

        from rich.table import Table

        table = Table(show_header=True, header_style="bold blue", show_lines=False)
        table.add_column("Project", style="cyan", no_wrap=True)
        table.add_column("Status", justify="center", width=8)
        table.add_column("Duration", justify="right", width=10)

        # Sort projects by status (active first, then completed, then pending)
        status_order = {
            CloneStatus.CLONING: 0,
            CloneStatus.SUCCESS: 1,
            CloneStatus.FAILED: 2,
            CloneStatus.ALREADY_EXISTS: 3,
            CloneStatus.SKIPPED: 4,
            CloneStatus.PENDING: 5,
        }

        sorted_results = sorted(
            self._results.values(),
            key=lambda r: (status_order.get(r.status, 99), r.project.name),
        )

        # Show up to 20 most relevant projects
        for result in sorted_results[:20]:
            status_display = self._format_status_display(result.status)

            # Format duration
            if result.completed_at and result.started_at:
                duration = result.completed_at - result.started_at
                duration_str = self._format_duration(duration)
            elif result.started_at:
                current_duration = datetime.now(UTC) - result.started_at
                duration_str = f"~{self._format_duration(current_duration)}"
            else:
                duration_str = ""

            table.add_row(result.project.name, status_display, duration_str)

        return table

    def _format_status_display(self, status: CloneStatus) -> str | Any:
        """Format status with icon and color for display.

        Args:
            status: Clone status

        Returns:
            Formatted Rich Text or string if Rich not available
        """
        if not RICH_AVAILABLE:
            return str(status.value)

        status_map = {
            CloneStatus.PENDING: ("⏳", "dim"),
            CloneStatus.CLONING: ("⬇", "blue"),
            CloneStatus.SUCCESS: ("✓", "green"),
            CloneStatus.FAILED: ("✗", "red"),
            CloneStatus.SKIPPED: ("⊘", "dim"),
            CloneStatus.ALREADY_EXISTS: ("≈", "yellow"),
        }

        icon, style = status_map.get(status, ("?", "white"))
        from rich.text import Text

        return Text(icon, style=style)

    def _format_duration(self, duration: timedelta) -> str:
        """Format duration for display.

        Args:
            duration: Duration to format

        Returns:
            Formatted duration string
        """
        total_seconds = int(duration.total_seconds())

        if total_seconds < 60:
            return f"{total_seconds}s"
        elif total_seconds < 3600:
            minutes, seconds = divmod(total_seconds, 60)
            return f"{minutes}m{seconds:02d}s"
        else:
            hours, remainder = divmod(total_seconds, 3600)
            minutes, _ = divmod(remainder, 60)
            return f"{hours}h{minutes:02d}m"


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


def create_simple_progress_display(
    total: int, description: str = "Processing"
) -> Any | None:
    """Create a simple progress display for basic operations.

    Args:
        total: Total number of items
        description: Description for progress bar

    Returns:
        Simple progress display or None if Rich not available
    """
    if not RICH_AVAILABLE:
        return None

    try:
        from rich.console import Console
        from rich.progress import BarColumn, MofNCompleteColumn, Progress, TextColumn

        console = Console(stderr=True)
        progress = Progress(
            TextColumn("[progress.description]{task.description}"),
            BarColumn(),
            MofNCompleteColumn(),
            console=console,
        )

        task = progress.add_task(description, total=total)
        progress.start()

        return {"progress": progress, "task": task, "console": console}
    except Exception:
        return None
