# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""Factories for the :class:`~source_clone.models.CloneResult` records.

Clone, refresh and retry stages all report their outcome as a
``CloneResult``.  Centralising construction here keeps the status and
timing conventions consistent across those stages.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

from source_clone.models import CloneResult, CloneStatus, RefreshStatus

if TYPE_CHECKING:
    from pathlib import Path

    from source_clone.models import Config, Project, RefreshResult


#: Every :class:`RefreshStatus` mapped to the ``CloneStatus`` the clone
#: pipeline reports for it.  Deliberately exhaustive rather than an
#: ``if``/``else`` chain: the previous chain treated only ``FAILED`` as
#: unsuccessful, so a conflicted, detached-HEAD or skipped repository
#: fell through and was reported as ``VERIFIED``, indistinguishable from
#: one confirmed up to date.  A new ``RefreshStatus`` member must be
#: added here (``test_clone_results`` asserts the table is complete)
#: rather than silently inheriting a success status.
_REFRESH_TO_CLONE_STATUS: dict[RefreshStatus, CloneStatus] = {
    # Refresh completed. Refined to REFRESHED below when commits arrived.
    RefreshStatus.SUCCESS: CloneStatus.VERIFIED,
    RefreshStatus.UP_TO_DATE: CloneStatus.VERIFIED,
    # Deliberately left as found.
    RefreshStatus.SKIPPED: CloneStatus.SKIPPED,
    # Left un-refreshed by something the operator has to act on.
    RefreshStatus.FAILED: CloneStatus.FAILED,
    RefreshStatus.CONFLICTS: CloneStatus.FAILED,
    RefreshStatus.NOT_GIT_REPO: CloneStatus.FAILED,
    RefreshStatus.NOT_GERRIT_REPO: CloneStatus.FAILED,
    RefreshStatus.UNCOMMITTED_CHANGES: CloneStatus.FAILED,
    RefreshStatus.DETACHED_HEAD: CloneStatus.FAILED,
    # Non-terminal: reaching here means the refresh never ran to completion.
    RefreshStatus.PENDING: CloneStatus.FAILED,
    RefreshStatus.REFRESHING: CloneStatus.FAILED,
}

#: Human-readable reason for each unsuccessful refresh status, so the
#: reported error names what actually stopped the refresh.
_REFRESH_FAILURE_REASONS: dict[RefreshStatus, str] = {
    RefreshStatus.CONFLICTS: "merge conflicts",
    RefreshStatus.NOT_GIT_REPO: "not a git repository",
    RefreshStatus.NOT_GERRIT_REPO: "not a Gerrit repository",
    RefreshStatus.UNCOMMITTED_CHANGES: "uncommitted local changes",
    RefreshStatus.DETACHED_HEAD: "detached HEAD",
    RefreshStatus.PENDING: "refresh never started",
    RefreshStatus.REFRESHING: "refresh never completed",
}


def build_failure_result(
    config: Config, project: Project, error_message: str
) -> CloneResult:
    """Build a FAILED result for a project that produced no result of its own.

    Used when a worker raised before returning, or when the overall
    operation timed out and the future was cancelled.
    """
    now = datetime.now(UTC)
    return CloneResult(
        project=project,
        status=CloneStatus.FAILED,
        path=config.path / project.name,
        attempts=0,
        error_message=error_message,
        started_at=now,
        completed_at=now,
        first_started_at=now,
    )


def build_verified_result(
    config: Config, project: Project, started_at: datetime
) -> CloneResult:
    """Build a VERIFIED result for a repository already at the remote SHA."""
    return CloneResult(
        project=project,
        status=CloneStatus.VERIFIED,
        path=config.path / project.filesystem_path,
        started_at=started_at,
        completed_at=datetime.now(UTC),
        duration_seconds=0.0,
        was_refreshed=False,
        refresh_had_updates=False,
        refresh_commits_pulled=0,
    )


def build_already_exists_result(
    config: Config, project: Project, started_at: datetime
) -> CloneResult:
    """Build an ALREADY_EXISTS result for a repository left untouched."""
    return CloneResult(
        project=project,
        status=CloneStatus.ALREADY_EXISTS,
        path=config.path / project.filesystem_path,
        started_at=started_at,
        completed_at=datetime.now(UTC),
        duration_seconds=0.0,
    )


def _refresh_error_message(refresh_result: RefreshResult) -> str:
    """Describe an unsuccessful refresh, naming the status responsible.

    Args:
        refresh_result: Refresh outcome that did not succeed.

    Returns:
        Error text for the reported ``CloneResult``.
    """
    status = refresh_result.status
    if status is RefreshStatus.FAILED:
        # Wording preserved from before the explicit mapping existed.
        return f"Refresh failed: {refresh_result.error_message}"

    reason = _REFRESH_FAILURE_REASONS.get(status, "unrecognised refresh status")
    message = f"Refresh did not complete ({status.value}: {reason})"
    if refresh_result.error_message:
        message = f"{message}: {refresh_result.error_message}"
    return message


def build_refresh_result(
    project: Project,
    target_path: Path,
    refresh_result: RefreshResult,
    refresh_start: datetime,
) -> CloneResult:
    """Convert a :class:`RefreshResult` into a ``CloneResult``."""
    # An unmapped status defaults to FAILED rather than inheriting a
    # success status, so an unrecognised outcome is reported loudly.
    clone_status = _REFRESH_TO_CLONE_STATUS.get(
        refresh_result.status, CloneStatus.FAILED
    )

    # ``was_behind`` records what the refresh set out to do, not what it
    # achieved, so only a completed refresh may claim to have moved the
    # checkout on.
    updated = clone_status is CloneStatus.VERIFIED and refresh_result.was_behind

    error_message: str | None = None
    if clone_status is CloneStatus.FAILED:
        error_message = _refresh_error_message(refresh_result)
    elif clone_status is CloneStatus.SKIPPED:
        # Not an error, but the reason the repository was left alone is
        # worth carrying through to the manifest.
        error_message = refresh_result.error_message
    elif updated:
        # Refresh completed and brought the checkout forward.
        clone_status = CloneStatus.REFRESHED

    return CloneResult(
        project=project,
        status=clone_status,
        path=target_path,
        started_at=refresh_start,
        completed_at=datetime.now(UTC),
        duration_seconds=refresh_result.duration_seconds,
        was_refreshed=updated,
        refresh_had_updates=updated,
        refresh_commits_pulled=refresh_result.commits_pulled,
        content_filtered=refresh_result.content_filtered,
        error_message=error_message,
    )


def build_refresh_failure_result(
    project: Project,
    target_path: Path,
    refresh_start: datetime,
    error: Exception,
) -> CloneResult:
    """Build a FAILED result for a repository whose refresh raised."""
    return CloneResult(
        project=project,
        status=CloneStatus.FAILED,
        path=target_path,
        started_at=refresh_start,
        completed_at=datetime.now(UTC),
        duration_seconds=(datetime.now(UTC) - refresh_start).total_seconds(),
        error_message=f"Refresh failed for {project.name}: {error}",
    )
