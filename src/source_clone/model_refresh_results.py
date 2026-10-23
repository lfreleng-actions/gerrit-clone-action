# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""Result records for refresh operations.

Holds the per-repository :class:`RefreshResult` and the aggregate
:class:`RefreshBatchResult`, including their JSON manifest serialization.
Depends only on :mod:`source_clone.model_enums`, keeping it a leaf of the
model layer.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from source_clone.model_enums import RefreshStatus

if TYPE_CHECKING:
    from pathlib import Path


@dataclass
class RefreshResult:
    """Result of a refresh operation for a single repository."""

    path: Path
    project_name: str
    status: RefreshStatus
    started_at: datetime

    # Git state tracking
    was_behind: bool = False
    commits_pulled: int = 0
    files_changed: int = 0
    current_branch: str | None = None
    remote_url: str | None = None

    # Operation metadata
    attempts: int = 0
    duration_seconds: float = 0.0
    error_message: str | None = None
    completed_at: datetime | None = None

    # Conflict/warning tracking
    had_uncommitted_changes: bool = False
    stash_created: bool = False
    stash_popped: bool = False
    # Branch the stash was created on. Used to avoid auto-popping a stash onto a
    # different branch (e.g. after force-mode switches from a feature branch to
    # the default branch), which could apply changes to the wrong branch.
    stash_branch: str | None = None
    hard_reset: bool = False
    detached_head: bool = False
    # Whether the refresh already re-applied the run's content filters,
    # as a content-filtered mirror is refreshed (see
    # source_clone.refresh_filtered), so they need not run again.
    content_filtered: bool = False

    # Retry tracking
    first_started_at: datetime | None = None
    retry_count: int = 0
    last_attempt_duration: float = 0.0

    @property
    def success(self) -> bool:
        """Check if refresh was successful."""
        return self.status in (RefreshStatus.SUCCESS, RefreshStatus.UP_TO_DATE)

    @property
    def failed(self) -> bool:
        """Check if refresh failed."""
        return self.status == RefreshStatus.FAILED

    @property
    def skipped(self) -> bool:
        """Check if refresh was skipped."""
        return self.status in (
            RefreshStatus.SKIPPED,
            RefreshStatus.NOT_GIT_REPO,
            RefreshStatus.NOT_GERRIT_REPO,
            RefreshStatus.UNCOMMITTED_CHANGES,
        )

    @property
    def has_conflicts(self) -> bool:
        """Check if refresh resulted in conflicts."""
        return self.status == RefreshStatus.CONFLICTS

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary for JSON serialization."""
        data = {
            "project": self.project_name,
            "path": str(self.path),
            "status": self.status.value,
            "attempts": self.attempts,
            "duration_s": round(self.duration_seconds, 3),
            "error": self.error_message,
            "started_at": self.started_at.isoformat() if self.started_at else None,
            "completed_at": self.completed_at.isoformat()
            if self.completed_at
            else None,
            "was_behind": self.was_behind,
            "commits_pulled": self.commits_pulled,
            "files_changed": self.files_changed,
            "current_branch": self.current_branch,
            "remote_url": self.remote_url,
            "had_uncommitted_changes": self.had_uncommitted_changes,
            "stash_created": self.stash_created,
            "stash_popped": self.stash_popped,
            "stash_branch": self.stash_branch,
            "hard_reset": self.hard_reset,
            "detached_head": self.detached_head,
            "first_started_at": self.first_started_at.isoformat()
            if self.first_started_at
            else None,
            "retry_count": self.retry_count,
            "last_attempt_duration_s": round(self.last_attempt_duration, 3),
        }
        return data


@dataclass
class RefreshBatchResult:
    """Results of a batch refresh operation."""

    base_path: Path
    results: list[RefreshResult]
    started_at: datetime
    completed_at: datetime | None = None

    @property
    def total_count(self) -> int:
        """Total number of repositories processed."""
        return len(self.results)

    @property
    def success_count(self) -> int:
        """Number of successful refreshes."""
        return sum(1 for r in self.results if r.success)

    @property
    def up_to_date_count(self) -> int:
        """Number of repositories already up-to-date."""
        return sum(1 for r in self.results if r.status == RefreshStatus.UP_TO_DATE)

    @property
    def updated_count(self) -> int:
        """Number of repositories actually updated."""
        return sum(
            1
            for r in self.results
            if r.status == RefreshStatus.SUCCESS and r.was_behind
        )

    @property
    def failed_count(self) -> int:
        """Number of failed refreshes."""
        return sum(1 for r in self.results if r.failed)

    @property
    def skipped_count(self) -> int:
        """Number of skipped refreshes."""
        return sum(1 for r in self.results if r.skipped)

    @property
    def conflicts_count(self) -> int:
        """Number of repositories with conflicts."""
        return sum(1 for r in self.results if r.has_conflicts)

    @property
    def duration_seconds(self) -> float:
        """Total duration of batch operation."""
        if self.completed_at is None:
            return 0.0
        return (self.completed_at - self.started_at).total_seconds()

    @property
    def success_rate(self) -> float:
        """Success rate as a percentage."""
        if self.total_count == 0:
            return 0.0
        return (self.success_count / self.total_count) * 100

    @property
    def total_commits_pulled(self) -> int:
        """Total commits pulled across all repositories."""
        return sum(r.commits_pulled for r in self.results)

    @property
    def total_files_changed(self) -> int:
        """Total files changed across all repositories."""
        return sum(r.files_changed for r in self.results)

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary for JSON serialization."""
        return {
            "version": "1.0",
            "operation": "refresh",
            "generated_at": (self.completed_at or datetime.now(UTC)).isoformat(),
            "base_path": str(self.base_path),
            "total": self.total_count,
            "succeeded": self.success_count,
            "up_to_date": self.up_to_date_count,
            "updated": self.updated_count,
            "failed": self.failed_count,
            "skipped": self.skipped_count,
            "conflicts": self.conflicts_count,
            "success_rate": round(self.success_rate, 2),
            "duration_s": round(self.duration_seconds, 3),
            "total_commits_pulled": self.total_commits_pulled,
            "total_files_changed": self.total_files_changed,
            "results": [result.to_dict() for result in self.results],
        }
