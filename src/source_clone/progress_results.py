# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""Result bookkeeping for the clone progress tracker.

Creates the initial pending records, rebuilds a record for a retry attempt
while preserving its history, and applies the start/completion timestamps and
derived durations. Leaf module over the shared model types.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from source_clone.model_enums import CloneStatus
from source_clone.models import CloneResult

if TYPE_CHECKING:
    from datetime import datetime
    from pathlib import Path

    from source_clone.models import Project

# Statuses that mark the end of a clone attempt.
_TERMINAL_STATUSES = (
    CloneStatus.SUCCESS,
    CloneStatus.FAILED,
    CloneStatus.SKIPPED,
    CloneStatus.ALREADY_EXISTS,
)


def create_initial_results(
    projects: list[Project], base_path: Path
) -> dict[str, CloneResult]:
    """Create the pending result records for a run, keyed by project name."""
    results: dict[str, CloneResult] = {}
    for project in projects:
        target_path = base_path / project.name
        results[project.name] = CloneResult(
            project=project,
            status=CloneStatus.PENDING,
            path=target_path,
            started_at=None,
            completed_at=None,
            error_message=None,
        )
    return results


def build_retry_result(project: Project, existing_result: CloneResult) -> CloneResult:
    """Reset a result to pending for a retry, preserving its attempt history."""
    # Update existing result to pending status for retry
    # Preserve attempts count and nested_under info for accurate retry tracking
    return CloneResult(
        project=project,
        status=CloneStatus.PENDING,
        path=existing_result.path,
        started_at=None,
        completed_at=None,
        error_message=None,
        attempts=existing_result.attempts,  # Preserve attempt count for accurate retry metrics
        nested_under=existing_result.nested_under,  # Preserve nested dependency information
        first_started_at=existing_result.first_started_at
        or existing_result.started_at,  # Preserve original start time
        retry_count=existing_result.retry_count + 1,  # Increment retry counter
        last_attempt_duration=existing_result.last_attempt_duration,  # Preserve last attempt duration
    )


def apply_status_timestamps(
    result: CloneResult, status: CloneStatus, now: datetime
) -> None:
    """Record the timestamps and durations implied by a status transition.

    Durations are measured from ``first_started_at`` when a retry history
    exists so the reported figure covers the whole attempt sequence, while
    ``last_attempt_duration`` keeps the cost of the final attempt alone.
    """
    if status == CloneStatus.CLONING and not result.started_at:
        result.started_at = now
        # Set first_started_at if this is the very first attempt
        if not result.first_started_at:
            result.first_started_at = now
    elif status in _TERMINAL_STATUSES and not result.completed_at:
        result.completed_at = now
        if result.started_at:
            # Calculate duration from first attempt to completion
            if result.first_started_at:
                result.duration_seconds = (
                    result.completed_at - result.first_started_at
                ).total_seconds()
            else:
                result.duration_seconds = (
                    result.completed_at - result.started_at
                ).total_seconds()
            # Track duration of just this final attempt
            result.last_attempt_duration = (
                result.completed_at - result.started_at
            ).total_seconds()
