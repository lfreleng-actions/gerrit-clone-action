# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""Aggregate statistics and text-mode reporting for clone progress.

Computes the per-status counts shown in the progress panel and emits the
periodic and final summaries used when no Rich display is active. Pure
functions over the tracked results, so this module stays a leaf.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

from source_clone.logging import get_logger
from source_clone.model_enums import CloneStatus
from source_clone.progress_display import format_duration

if TYPE_CHECKING:
    from collections.abc import Mapping

    from source_clone.models import CloneResult

logger = get_logger(__name__)


def compute_summary(
    results: Mapping[str, CloneResult],
    start_time: datetime | None,
    end_time: datetime | None,
) -> dict[str, Any]:
    """Compute summary statistics for the tracked results.

    Args:
        results: Current results keyed by project name.
        start_time: When tracking started, if it has started.
        end_time: When tracking finished, if it has finished.

    Returns:
        Dictionary with per-status counts and the elapsed duration.
    """
    success = sum(1 for r in results.values() if r.status == CloneStatus.SUCCESS)
    failed = sum(1 for r in results.values() if r.status == CloneStatus.FAILED)
    skipped = sum(1 for r in results.values() if r.status == CloneStatus.SKIPPED)
    already_exists = sum(
        1 for r in results.values() if r.status == CloneStatus.ALREADY_EXISTS
    )
    cloning = sum(1 for r in results.values() if r.status == CloneStatus.CLONING)
    pending = sum(1 for r in results.values() if r.status == CloneStatus.PENDING)

    total = len(results)
    completed = success + failed + skipped + already_exists

    # Calculate duration
    if start_time and end_time:
        duration = end_time - start_time
    elif start_time:
        duration = datetime.now(UTC) - start_time
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


def log_periodic_summary(summary: dict[str, Any]) -> None:
    """Log periodic summary in text mode."""
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


def log_final_summary(
    summary: dict[str, Any], results: Mapping[str, CloneResult]
) -> None:
    """Log final summary."""
    duration = format_duration(summary["duration"])

    logger.debug("=== Clone Summary ===")
    logger.debug(f"Duration: {duration}")
    logger.debug(f"Total: {summary['total']}")
    logger.debug(f"Success: {summary['success']}")
    logger.debug(f"Failed: {summary['failed']}")
    logger.debug(f"Skipped: {summary['skipped']}")
    logger.debug(f"Already exists: {summary['already_exists']}")

    if summary["failed"] > 0:
        logger.debug("Failed projects:")
        for result in results.values():
            if result.status == CloneStatus.FAILED:
                error_msg = result.error_message or "Unknown error"
                logger.debug(f"  - {result.project.name}: {error_msg}")
