# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""Rich rendering helpers for the clone progress display.

Builds the panel, project table and formatted status/duration cells shown by
:class:`~source_clone.progress.ProgressTracker`. Each renderer checks
``RICH_AVAILABLE`` and returns a plain-text equivalent when Rich is absent.
The tracker is referenced for type checking only, keeping this module a leaf.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from source_clone.model_enums import CloneStatus
from source_clone.rich_optional import (
    RICH_AVAILABLE,
    BarColumn,
    Console,
    Group,
    MofNCompleteColumn,
    Panel,
    Progress,
    SpinnerColumn,
    Table,
    Text,
    TextColumn,
)

if TYPE_CHECKING:
    from collections.abc import Iterable
    from datetime import timedelta

    from source_clone.models import CloneResult
    from source_clone.progress import ProgressTracker

# Display configuration constants
MAX_PROJECTS_FOR_TABLE = 30
MIN_CONSOLE_WIDTH_FOR_TABLE = 100


def create_display(tracker: ProgressTracker) -> Any:
    """Create Rich display content."""
    if not RICH_AVAILABLE or not tracker.console:
        return ""

    summary = tracker._get_summary_unsafe()

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

    status_text = " | ".join(status_parts) if status_parts else "[dim]No activity[/dim]"

    content_parts: list[Any] = []
    if tracker._progress:
        summary = tracker._get_summary_unsafe()

        fresh_progress = Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            BarColumn(),
            MofNCompleteColumn(),
            TextColumn("[progress.elapsed]{task.fields[elapsed]}"),
            expand=True,
        )
        fresh_progress.add_task(
            "Cloning repositories",
            completed=summary["completed"],
            total=summary["total"],
            # Calculate manual elapsed time to avoid reset
            elapsed=format_elapsed(tracker._start_time),
        )
        content_parts.append(fresh_progress)

    # Add project table if reasonable number of projects and terminal is wide enough
    if (
        len(tracker._results) <= MAX_PROJECTS_FOR_TABLE
        and tracker.console
        and tracker.console.size.width > MIN_CONSOLE_WIDTH_FOR_TABLE
    ):
        content_parts.append(create_project_table(tracker._results.values()))

    # Combine content
    if len(content_parts) == 1:
        main_content = content_parts[0]
    else:
        main_content = Group(*content_parts)

    # Add log message line
    log_message = tracker.get_current_log_message()
    log_line = Text.from_markup(
        f"[dim]ℹ️  {log_message}[/dim]" if log_message else "[dim]Ready...[/dim]",  # noqa: RUF001
        overflow="fold",
    )

    # Combine progress and log message
    display_content = Group(main_content, "", log_line)

    return Panel(
        display_content,
        title="Repository Clone Progress",
        subtitle=status_text,
        border_style="blue",
    )


def format_elapsed(start_time: datetime | None) -> str:
    """Format elapsed time since *start_time* for the progress bar column.

    Elapsed time is computed manually rather than read from Rich so that
    rebuilding the progress widget on every refresh does not reset it.
    """
    if not start_time:
        return "0:00"

    elapsed_seconds = (datetime.now(UTC) - start_time).total_seconds()
    elapsed_minutes, secs = divmod(int(elapsed_seconds), 60)
    elapsed_hours, mins = divmod(elapsed_minutes, 60)
    if elapsed_hours > 0:
        return f"{elapsed_hours}:{mins:02d}:{secs:02d}"
    return f"{mins}:{secs:02d}"


def create_project_table(results: Iterable[CloneResult]) -> Any:
    """Create table showing project status."""
    if not RICH_AVAILABLE:
        return ""

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
        results,
        key=lambda r: (status_order.get(r.status, 99), r.project.name),
    )

    # Show up to 20 most relevant projects
    for result in sorted_results[:20]:
        status_display = format_status_display(result.status)

        # Format duration
        if result.completed_at and result.started_at:
            duration = result.completed_at - result.started_at
            duration_str = format_duration(duration)
        elif result.started_at:
            current_duration = datetime.now(UTC) - result.started_at
            duration_str = f"~{format_duration(current_duration)}"
        else:
            duration_str = ""

        table.add_row(result.project.name, status_display, duration_str)

    return table


def format_status_display(status: CloneStatus) -> str | Any:
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
    return Text(icon, style=style)


def format_duration(duration: timedelta) -> str:
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
