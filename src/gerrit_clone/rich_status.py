# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2025 Matthew Watkins <mwatkins@linuxfoundation.org>

"""Rich status messaging system separate from file logging."""

from __future__ import annotations

import threading
import traceback
from typing import Any, Self

from rich.console import Console
from rich.panel import Panel
from rich.text import Text

# Global reference to the active progress tracker for status integration
_current_progress_tracker: Any | None = None
_status_lock = threading.Lock()


def set_progress_tracker(tracker: Any) -> None:
    """Set the current progress tracker for status integration.

    Args:
        tracker: ProgressTracker instance with status update methods
    """
    global _current_progress_tracker  # noqa: PLW0603
    with _status_lock:
        _current_progress_tracker = tracker


def clear_progress_tracker() -> None:
    """Clear the current progress tracker reference."""
    global _current_progress_tracker  # noqa: PLW0603
    with _status_lock:
        _current_progress_tracker = None


def status(message: str, temp: bool = True) -> None:
    """Display a status message through the progress system.

    Args:
        message: Status message to display
        temp: If True, message is temporary and will be replaced by next update
    """
    with _status_lock:
        if _current_progress_tracker and hasattr(
            _current_progress_tracker, "set_status"
        ):
            _current_progress_tracker.set_status(message, temp=temp)


def persistent_status(message: str) -> None:
    """Display a persistent status message.

    Args:
        message: Persistent message to display
    """
    status(message, temp=False)


def clear_status() -> None:
    """Clear the current status message."""
    with _status_lock:
        if _current_progress_tracker and hasattr(
            _current_progress_tracker, "clear_status"
        ):
            _current_progress_tracker.clear_status()


def print_status_message(message: str, console: Console | None = None) -> None:
    """Print a standalone status message to the console.

    Args:
        message: Status message to display
        console: Optional Rich console instance (creates new if None)
    """
    if console is None:
        console = Console(stderr=True)
    console.print(message)


def connecting_to_server(
    host: str, port: int, console: Console | None = None
) -> None:
    """Show connecting to server status."""
    # Try to send to progress tracker first
    status(f"Connecting to Gerrit server {host}:{port}")
    # Also show as standalone message
    print_status_message(f"🌐 Connecting to Gerrit server {host}:{port}", console)


def discovering_projects(
    host: str, method: str = "", console: Console | None = None
) -> None:
    """Show discovering projects status.

    Args:
        host: Gerrit server hostname
        method: Discovery method (e.g., "HTTP", "SSH")
        console: Optional Rich console instance
    """
    # Try to send to progress tracker first
    method_suffix = f" [{method.upper()}]" if method else ""
    status(f"Discovering projects on {host}{method_suffix}")
    # Also show as standalone message
    print_status_message(f"🔍 Discovering projects on {host}{method_suffix}", console)


def projects_found(
    count: int, method: str = "", console: Console | None = None  # noqa: ARG001
) -> None:
    """Show projects found status.

    Args:
        count: Number of projects found
        method: Discovery method (e.g., "HTTP", "SSH")
        console: Optional Rich console instance
    """
    # Try to send to progress tracker first
    status(f"Found {count} projects to process")
    # Also show as standalone message
    print_status_message(f"✅ Found {count} projects to process", console)


def starting_clone(
    filtered_count: int,
    threads: int,
    skipped_count: int = 0,
    console: Console | None = None,
    item_name: str = "projects",
) -> None:
    """Show starting clone operation status."""
    if skipped_count > 0:
        message = f"Cloning {filtered_count} active {item_name} with {threads} workers (skipping {skipped_count} archived)"
    else:
        message = f"Cloning {filtered_count} {item_name} with {threads} workers"

    # Try to send to progress tracker first
    status(message)
    # Also show as standalone message
    print_status_message(f"🚀 {message}...", console)


def retrying_failed_clones(
    failed_count: int, threads: int, console: Console | None = None
) -> None:
    """Show retrying failed clones status.

    Args:
        failed_count: Number of failed clones to retry
        threads: Number of worker threads for retry
        console: Optional Rich console instance
    """
    message = f"Retrying {failed_count} failed clone(s) with {threads} workers"
    status(message)
    print_status_message(f"🔄 {message}...", console)
    # Add extra line separation before progress bar
    if console is None:
        console = Console(stderr=True)
    console.print()


def clone_completed(success_count: int, failed_count: int, duration_str: str) -> None:
    """Show clone completion status."""
    if failed_count == 0:
        message = f"Clone completed successfully! {success_count} repositories cloned in {duration_str}"
    else:
        message = f"Clone completed: {success_count} succeeded, {failed_count} failed in {duration_str}"

    persistent_status(message)


def success_rate(rate: float, failed_count: int) -> None:
    """Show success rate status."""
    emoji = "✅" if failed_count == 0 else "❌"
    status(f"{emoji} Success rate: {rate:.1f}%")


def show_error_summary(
    console: Console, errors: list[str], warnings: list[str] | None = None
) -> None:
    """Show error/warning summary in plain text matching Mirror Summary style.

    Args:
        console: Rich console instance
        errors: List of error messages
        warnings: Optional list of warning messages
    """
    if not errors and not warnings:
        return

    if errors:
        console.print(
            f"[bold red]Issues Summary ({len(errors)} "
            f"error{'s' if len(errors) != 1 else ''})[/bold red]"
        )
        for i, error in enumerate(errors[:5], 1):
            console.print(f"  [red]{i}. {error}[/red]")
        if len(errors) > 5:
            console.print(
                f"  [red]... and {len(errors) - 5} more[/red]"
            )

    if warnings:
        console.print(
            f"[bold yellow]Warnings Summary ({len(warnings)} "
            f"warning{'s' if len(warnings) != 1 else ''})[/bold yellow]"
        )
        for i, warning in enumerate(warnings[:5], 1):
            console.print(f"  [yellow]{i}. {warning}[/yellow]")
        if len(warnings) > 5:
            console.print(
                f"  [yellow]... and {len(warnings) - 5} more[/yellow]"
            )


def show_final_results(
    console: Console, batch_result: Any, log_file_path: str | None = None
) -> None:
    """Show final results summary in a Rich panel.

    Args:
        console: Rich console instance
        batch_result: BatchResult with operation results
        log_file_path: Path to log file, if created
    """
    # Calculate duration
    if hasattr(batch_result, "started_at") and hasattr(batch_result, "completed_at"):
        duration = batch_result.completed_at - batch_result.started_at
        duration_str = f"{duration.total_seconds():.1f}s"
    else:
        duration_str = "unknown"

    content_lines = []

    # Summary statistics
    content_lines.extend(
        [
            f"[bold]Duration:[/bold] {duration_str}",
            f"[bold]Total Repositories:[/bold] {batch_result.total_count}",
            f"[green]Successfully Cloned:[/green] {batch_result.success_count}",
            f"[cyan]Refreshed:[/cyan] {getattr(batch_result, 'refreshed_count', 0)}",
            f"[blue]Verified:[/blue] {getattr(batch_result, 'verified_count', 0)}",
            f"[dim]Already Existed:[/dim] {getattr(batch_result, 'already_exists_count', 0)}",
            f"[red]Failed:[/red] {batch_result.failed_count}",
            f"[dim]Skipped:[/dim] {getattr(batch_result, 'skipped_count', 0)}",
        ]
    )

    # Success rate (includes both newly cloned and already existing as successful)
    if batch_result.total_count > 0:
        content_lines.append(f"[bold]Success Rate:[/bold] {batch_result.success_rate:.1f}%")

    if log_file_path:
        content_lines.append(f"[dim]Log File:[/dim] {log_file_path}")

    title_color = "green" if batch_result.failed_count == 0 else "yellow"

    summary_text = Text.from_markup("\n".join(content_lines))
    panel = Panel(
        summary_text,
        title="[bold]✅ Command Summary[/bold]",
        border_style=title_color,
        padding=(1, 2),
    )

    console.print(panel)


def handle_crash_display(
    console: Console, exception: Exception, log_file_path: str | None = None
) -> None:
    """Display crash information in a tidy Rich panel.

    Args:
        console: Rich console instance
        exception: Exception that caused the crash
        log_file_path: Path to log file, if available
    """
    tb = traceback.extract_tb(exception.__traceback__)
    crash_context = "unknown location"

    if tb:
        last_frame = tb[-1]
        filename = last_frame.filename.split("/")[-1]
        crash_context = f"{last_frame.name}() at {filename}:{last_frame.lineno}"

    content_lines = [
        f"[bold red]Exception:[/bold red] {type(exception).__name__}",
        f"[bold red]Location:[/bold red] {crash_context}",
        f"[bold red]Message:[/bold red] {exception!s}",
    ]

    if log_file_path:
        content_lines.append(f"[dim]Log File:[/dim] {log_file_path}")

    crash_text = Text.from_markup("\n".join(content_lines))
    panel = Panel(
        crash_text,
        title="[bold red]💥 Tool Crashed[/bold red]",
        border_style="red",
        padding=(1, 2),
    )

    console.print("\n")
    console.print(panel)


class RichStatusManager:
    """Context manager for Rich status operations."""

    def __init__(self, progress_tracker: Any):
        """Initialize with progress tracker.

        Args:
            progress_tracker: ProgressTracker instance
        """
        self.progress_tracker = progress_tracker
        self._previous_tracker = None

    def __enter__(self) -> Self:
        """Enter context and set progress tracker."""
        global _current_progress_tracker  # noqa: PLW0603
        with _status_lock:
            self._previous_tracker = _current_progress_tracker
            _current_progress_tracker = self.progress_tracker
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: object | None,
    ) -> None:
        """Exit context and restore previous tracker."""
        global _current_progress_tracker  # noqa: PLW0603
        with _status_lock:
            _current_progress_tracker = self._previous_tracker


def create_status_manager(progress_tracker: Any) -> RichStatusManager:
    """Create a status manager for the given progress tracker.

    Args:
        progress_tracker: ProgressTracker instance

    Returns:
        RichStatusManager instance
    """
    return RichStatusManager(progress_tracker)
