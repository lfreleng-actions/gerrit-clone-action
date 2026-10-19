# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""Lifecycle and refresh of the Rich progress components.

Starts, refreshes and tears down the Rich ``Progress``/``Live`` widgets owned
by :class:`~source_clone.progress.ProgressTracker`, including the documented
fallback chain from the Live display to a simple bar and finally to plain text
logging. The tracker is referenced for type checking only.
"""

from __future__ import annotations

import contextlib
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from source_clone.logging import get_logger
from source_clone.progress_display import create_display, format_elapsed
from source_clone.progress_modes import ProgressMode
from source_clone.rich_optional import (
    RICH_AVAILABLE,
    BarColumn,
    Live,
    MofNCompleteColumn,
    Progress,
    SpinnerColumn,
    TextColumn,
)

if TYPE_CHECKING:
    from source_clone.models import Project
    from source_clone.progress import ProgressTracker

logger = get_logger(__name__)


def initialize_rich_components(tracker: ProgressTracker) -> None:
    """Initialize Rich components based on mode."""
    if not RICH_AVAILABLE or not tracker.console:
        return

    columns = [
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        MofNCompleteColumn(),
        TextColumn("[progress.elapsed]{task.fields[elapsed]}"),
    ]

    tracker._progress = Progress(
        *columns,
        console=tracker.console,
        transient=tracker._mode == ProgressMode.RICH_SIMPLE,
    )

    tracker._live = None
    tracker._last_update_time = datetime.now(UTC)
    tracker._update_interval = 0.5  # Update every 0.5 seconds for responsiveness


def start_rich_periodic(tracker: ProgressTracker, projects: list[Project]) -> None:
    """Start Rich display with periodic updates (no Live interference)."""
    if not tracker._progress or not tracker.console:
        return

    try:
        tracker._main_task = tracker._progress.add_task(
            "Cloning repositories", total=len(projects), elapsed="0:00"
        )

        display_content = create_display(tracker)
        tracker._live = Live(
            display_content,
            console=tracker.console,
            refresh_per_second=2,
            vertical_overflow="visible",
        )
        tracker._live.start()

        # Set initial log message
        tracker.update_log_message("Starting repository clone operations...")

    except Exception as e:
        logger.warning(f"Error starting Rich periodic display: {e}")
        # Ensure Live display is properly stopped if it was partially started
        if tracker._live:
            with contextlib.suppress(Exception):
                tracker._live.stop()
            tracker._live = None
        # Fall back to simple mode
        tracker._mode = ProgressMode.RICH_SIMPLE
        start_rich_simple(tracker, projects)


def start_rich_simple(tracker: ProgressTracker, projects: list[Project]) -> None:
    """Start simple Rich progress bar."""
    if not tracker._progress:
        return

    try:
        tracker._main_task = tracker._progress.add_task(
            "Cloning repositories", total=len(projects), elapsed="0:00"
        )

        if RICH_AVAILABLE:
            tracker._progress.start()

    except Exception as e:
        logger.warning(f"Failed to start Rich simple display: {e}")
        # Fall back to text mode
        tracker._mode = ProgressMode.TEXT_ONLY
        start_text_mode(projects)


def start_text_mode(projects: list[Project]) -> None:
    """Start text-only progress logging."""
    logger.info(f"Starting clone of {len(projects)} repositories")


def stop_display(tracker: ProgressTracker) -> None:
    """Stop and cleanup display components."""
    if tracker._live and RICH_AVAILABLE:
        try:
            tracker._live.stop()
        except Exception as e:
            logger.debug(f"Error stopping live display: {e}")
        finally:
            tracker._live = None


def update_progress_count(tracker: ProgressTracker) -> None:
    """Update the progress bar with current completion count and elapsed time."""
    if (
        tracker._main_task
        and tracker._progress
        and RICH_AVAILABLE
        and hasattr(tracker._progress, "update")
    ):
        summary = tracker._get_summary_unsafe()
        tracker._progress.update(
            tracker._main_task,
            completed=summary["completed"],
            elapsed=format_elapsed(tracker._start_time),
        )


def update_display(tracker: ProgressTracker) -> None:
    """Update the display based on current mode."""
    if tracker._mode == ProgressMode.RICH_PERIODIC and tracker._live and RICH_AVAILABLE:
        # Use Live display for real-time updates
        try:
            tracker._live.update(create_display(tracker))
            tracker._last_update_time = datetime.now(UTC)
        except Exception as e:
            logger.debug(f"Error updating live display: {e}")
            # If Live display fails, fall back to simple mode to prevent further issues
            fall_back_to_simple(tracker)
    elif (
        tracker._mode == ProgressMode.RICH_PERIODIC
        and tracker.console
        and RICH_AVAILABLE
    ):
        # Fallback to periodic console updates for RICH_PERIODIC without Live
        now = datetime.now(UTC)
        if (
            now - tracker._last_update_time
        ).total_seconds() >= tracker._update_interval:
            try:
                tracker.console.print(create_display(tracker))
                tracker._last_update_time = now
            except Exception as e:
                logger.warning(f"Error updating periodic display: {e}")
    elif (
        tracker._mode == ProgressMode.RICH_SIMPLE
        and tracker._progress
        and RICH_AVAILABLE
        and tracker._main_task
    ):
        # Handle RICH_SIMPLE mode - just update the progress bar, no custom display
        summary = tracker._get_summary_unsafe()
        try:
            tracker._progress.update(tracker._main_task, completed=summary["completed"])
        except Exception as e:
            logger.debug(f"Error updating simple progress: {e}")


def fall_back_to_simple(tracker: ProgressTracker) -> None:
    """Tear down a failed Live display and downgrade to the simple progress bar."""
    if tracker._live is not None:
        with contextlib.suppress(Exception):
            tracker._live.stop()
    tracker._live = None
    tracker._mode = ProgressMode.RICH_SIMPLE
