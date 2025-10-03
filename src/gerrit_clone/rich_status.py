# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2025 Matthew Watkins <mwatkins@linuxfoundation.org>

"""Rich status messaging system separate from file logging."""

from __future__ import annotations

import threading
from typing import Optional, Any, Self

from rich.console import Console
from rich.panel import Panel
from rich.text import Text

# Global reference to the active progress tracker for status integration
_current_progress_tracker: Optional[Any] = None
_status_lock = threading.Lock()


def set_progress_tracker(tracker: Any) -> None:
    """Set the current progress tracker for status integration.

    Args:
        tracker: ProgressTracker instance with status update methods
    """
    global _current_progress_tracker
    with _status_lock:
        _current_progress_tracker = tracker


def clear_progress_tracker() -> None:
    """Clear the current progress tracker reference."""
    global _current_progress_tracker
    with _status_lock:
        _current_progress_tracker = None


def status(message: str, temp: bool = True) -> None:
    """Display a status message through the progress system.

    Args:
        message: Status message to display
        temp: If True, message is temporary and will be replaced by next update
    """
    with _status_lock:
        if _current_progress_tracker and hasattr(_current_progress_tracker, 'set_status'):
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
        if _current_progress_tracker and hasattr(_current_progress_tracker, 'clear_status'):
            _current_progress_tracker.clear_status()


def print_status_message(message: str, console: Optional[Console] = None) -> None:
    """Print a standalone status message to the console.

    Args:
        message: Status message to display
        console: Optional Rich console instance (creates new if None)
    """
    if console is None:
        console = Console(stderr=True)
    console.print(message)


def connecting_to_server(host: str, port: int, console: Optional[Console] = None) -> None:
    """Show connecting to server status."""
    # Try to send to progress tracker first
    status(f"Connecting to Gerrit server {host}:{port}")
    # Also show as standalone message
    print_status_message(f"🌐 Connecting to Gerrit server {host}:{port}", console)


def discovering_projects(host: str, console: Optional[Console] = None) -> None:
    """Show discovering projects status."""
    # Try to send to progress tracker first
    status(f"Discovering projects on {host}")
    # Also show as standalone message
    print_status_message(f"🔍 Discovering projects on {host}", console)


def projects_found(count: int, console: Optional[Console] = None) -> None:
    """Show projects found status."""
    # Try to send to progress tracker first
    status(f"Found {count} projects to process")
    # Also show as standalone message
    print_status_message(f"✅ Found {count} projects to process", console)


def starting_clone(filtered_count: int, threads: int, skipped_count: int = 0, console: Optional[Console] = None) -> None:
    """Show starting clone operation status."""
    if skipped_count > 0:
        message = f"Cloning {filtered_count} active projects with {threads} workers (skipping {skipped_count} archived)"
    else:
        message = f"Cloning {filtered_count} projects with {threads} workers"

    # Try to send to progress tracker first
    status(message)
    # Also show as standalone message
    print_status_message(f"🚀 {message}...", console)
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


def show_error_summary(console: Console, errors: list[str], warnings: list[str] | None = None) -> None:
    """Show error summary in a Rich panel at the end of execution.

    Args:
        console: Rich console instance
        errors: List of error messages
        warnings: Optional list of warning messages
    """
    if not errors and not warnings:
        return

    # Build summary content
    content_lines = []

    if errors:
        content_lines.append(f"[red]Errors ({len(errors)}):[/red]")
        for i, error in enumerate(errors[:5], 1):  # Show max 5 errors
            content_lines.append(f"  {i}. {error}")
        if len(errors) > 5:
            content_lines.append(f"  ... and {len(errors) - 5} more errors")
        content_lines.append("")

    if warnings:
        content_lines.append(f"[yellow]Warnings ({len(warnings)}):[/yellow]")
        for i, warning in enumerate(warnings[:3], 1):  # Show max 3 warnings
            content_lines.append(f"  {i}. {warning}")
        if len(warnings) > 3:
            content_lines.append(f"  ... and {len(warnings) - 3} more warnings")

    if content_lines:
        summary_text = Text.from_markup("\n".join(content_lines))
        panel = Panel(
            summary_text,
            title="[bold red]Issues Summary[/bold red]" if errors else "[bold yellow]Warnings Summary[/bold yellow]",
            border_style="red" if errors else "yellow",
            padding=(1, 2),
        )
        console.print("\n")
        console.print(panel)


def show_final_results(console: Console, batch_result: Any, log_file_path: Optional[str] = None) -> None:
    """Show final results summary in a Rich panel.

    Args:
        console: Rich console instance
        batch_result: BatchResult with operation results
        log_file_path: Path to log file, if created
    """
    # Calculate duration
    if hasattr(batch_result, 'started_at') and hasattr(batch_result, 'completed_at'):
        duration = batch_result.completed_at - batch_result.started_at
        duration_str = f"{duration.total_seconds():.1f}s"
    else:
        duration_str = "unknown"

    # Build results content
    content_lines = []

    # Summary statistics
    content_lines.extend([
        f"[bold]Duration:[/bold] {duration_str}",
        f"[bold]Total Repositories:[/bold] {batch_result.total_count}",
        f"[green]Successfully Cloned:[/green] {batch_result.success_count}",
        f"[blue]Already Existed:[/blue] {getattr(batch_result, 'already_exists_count', 0)}",
        f"[red]Failed:[/red] {batch_result.failed_count}",
        f"[dim]Skipped:[/dim] {getattr(batch_result, 'skipped_count', 0)}",
    ])

    # Success rate
    if batch_result.total_count > 0:
        success_rate = (batch_result.success_count / batch_result.total_count) * 100
        content_lines.append(f"[bold]Success Rate:[/bold] {success_rate:.1f}%")

    # Log file reference
    if log_file_path:
        content_lines.append(f"[dim]Log File:[/dim] {log_file_path}")

    # Create panel
    title_color = "green" if batch_result.failed_count == 0 else "yellow"
    title_text = "✅ Clone Completed Successfully" if batch_result.failed_count == 0 else "⚠️ Clone Completed with Issues"

    summary_text = Text.from_markup("\n".join(content_lines))
    panel = Panel(
        summary_text,
        title=f"[bold {title_color}]{title_text}[/bold {title_color}]",
        border_style=title_color,
        padding=(1, 2),
    )

    console.print("\n")
    console.print(panel)


def handle_crash_display(console: Console, exception: Exception, log_file_path: Optional[str] = None) -> None:
    """Display crash information in a tidy Rich panel.

    Args:
        console: Rich console instance
        exception: Exception that caused the crash
        log_file_path: Path to log file, if available
    """
    import traceback

    # Get crash context
    tb = traceback.extract_tb(exception.__traceback__)
    crash_context = "unknown location"

    if tb:
        last_frame = tb[-1]
        filename = last_frame.filename.split('/')[-1]
        crash_context = f"{last_frame.name}() at {filename}:{last_frame.lineno}"

    # Build crash content
    content_lines = [
        f"[bold red]Exception:[/bold red] {type(exception).__name__}",
        f"[bold red]Location:[/bold red] {crash_context}",
        f"[bold red]Message:[/bold red] {str(exception)}",
    ]

    if log_file_path:
        content_lines.append(f"[dim]Log File:[/dim] {log_file_path}")

    # Create crash panel
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
        global _current_progress_tracker
        with _status_lock:
            self._previous_tracker = _current_progress_tracker
            _current_progress_tracker = self.progress_tracker
        return self

    def __exit__(self, exc_type: type[BaseException] | None, exc_val: BaseException | None, exc_tb: object | None) -> None:
        """Exit context and restore previous tracker."""
        global _current_progress_tracker
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
