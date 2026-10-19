# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""Result records for clone operations.

Holds the per-project :class:`CloneResult` and the aggregate
:class:`BatchResult`, including their JSON manifest serialization. Imports
:class:`~source_clone.models.Config` only for type checking so this module
stays a leaf of the model layer.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from source_clone.model_enums import CloneStatus

if TYPE_CHECKING:
    from pathlib import Path

    from source_clone.model_project import Project
    from source_clone.models import Config


@dataclass
class CloneResult:
    """Result of a clone operation for a single project."""

    project: Project
    status: CloneStatus
    path: Path
    attempts: int = 0
    duration_seconds: float = 0.0
    error_message: str | None = None
    started_at: datetime | None = None
    completed_at: datetime | None = None
    # Name of ancestor (parent) project if this repository was cloned nested under a parent
    nested_under: str | None = None
    # Retry tracking fields for complete attempt history
    first_started_at: datetime | None = None
    retry_count: int = 0
    last_attempt_duration: float = 0.0
    # Refresh tracking fields
    was_refreshed: bool = False
    refresh_had_updates: bool = False
    refresh_commits_pulled: int = 0
    # Whether the refresh already re-applied the run's content filters.
    content_filtered: bool = False

    @property
    def success(self) -> bool:
        """Check if clone operation was successful.

        Returns True for all non-error statuses:
        - SUCCESS: Newly cloned repository
        - ALREADY_EXISTS: Repository existed, no changes
        - REFRESHED: Repository existed and was updated (pulled new commits)
        - VERIFIED: Repository existed and was verified as up-to-date (no changes)

        Note: For detailed statistics, use refresh_had_updates to distinguish
        between repos that were updated (REFRESHED with updates) vs merely
        verified as current (VERIFIED or REFRESHED without updates).
        """
        return self.status in (
            CloneStatus.SUCCESS,
            CloneStatus.ALREADY_EXISTS,
            CloneStatus.REFRESHED,
            CloneStatus.VERIFIED,
        )

    @property
    def failed(self) -> bool:
        """Check if clone failed."""
        return self.status == CloneStatus.FAILED

    @property
    def skipped(self) -> bool:
        """Check if clone was skipped."""
        return self.status == CloneStatus.SKIPPED

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary for JSON serialization."""
        data = {
            "project": self.project.name,
            "path": str(self.path),
            "status": self.status.value,
            "attempts": self.attempts,
            "duration_s": round(self.duration_seconds, 3),
            "error": self.error_message,
            "started_at": self.started_at.isoformat() if self.started_at else None,
            "completed_at": self.completed_at.isoformat()
            if self.completed_at
            else None,
            "nested_under": self.nested_under,
            "first_started_at": self.first_started_at.isoformat()
            if self.first_started_at
            else None,
            "retry_count": self.retry_count,
            "last_attempt_duration_s": round(self.last_attempt_duration, 3),
        }
        if self.nested_under:
            data["nested_under"] = self.nested_under
        return data


@dataclass
class BatchResult:
    """Results of a batch clone operation."""

    config: Config
    results: list[CloneResult]
    started_at: datetime
    completed_at: datetime | None = None

    @property
    def total_count(self) -> int:
        """Total number of projects processed."""
        return len(self.results)

    @property
    def success_count(self) -> int:
        """Number of successful operations (aggregate of all non-error statuses).

        This includes:
        - Newly cloned repositories (SUCCESS)
        - Already existing repositories (ALREADY_EXISTS)
        - Refreshed repositories that pulled changes (REFRESHED)
        - Verified repositories that were up-to-date (VERIFIED)

        For more granular statistics, use the individual count properties:
        - already_exists_count: repos that existed, not refreshed
        - refreshed_count: repos that were refreshed (pulled changes)
        - verified_count: repos verified as up-to-date (no changes)
        """
        return sum(1 for r in self.results if r.success)

    @property
    def already_exists_count(self) -> int:
        """Number of repositories that already existed (not refreshed)."""
        return sum(1 for r in self.results if r.status == CloneStatus.ALREADY_EXISTS)

    @property
    def refreshed_count(self) -> int:
        """Number of repositories that were refreshed (pulled changes)."""
        return sum(1 for r in self.results if r.status == CloneStatus.REFRESHED)

    @property
    def verified_count(self) -> int:
        """Number of repositories that were verified as up-to-date."""
        return sum(1 for r in self.results if r.status == CloneStatus.VERIFIED)

    @property
    def failed_count(self) -> int:
        """Number of failed clones."""
        return sum(1 for r in self.results if r.failed)

    @property
    def skipped_count(self) -> int:
        """Number of skipped clones."""
        return sum(1 for r in self.results if r.skipped)

    @property
    def duration_seconds(self) -> float:
        """Total duration of batch operation."""
        if self.completed_at is None:
            return 0.0
        return (self.completed_at - self.started_at).total_seconds()

    @property
    def success_rate(self) -> float:
        """Success rate as a percentage (includes already existing, refreshed, and verified repos)."""
        if self.total_count == 0:
            return 0.0
        return (self.success_count / self.total_count) * 100

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary for JSON serialization."""
        return {
            "version": "1.0",
            "generated_at": (self.completed_at or datetime.now(UTC)).isoformat(),
            "host": self.config.host,
            "port": self.config.port if self.config.port is not None else "N/A",
            "source_type": self.config.source_type.value,
            "clone_config": {
                "use_https": self.config.use_https,
                "use_gh_cli": self.config.use_gh_cli,
                "depth": self.config.depth,
                "branch": self.config.branch,
                "discovery_method": (
                    self.config.discovery_method.value
                    if self.config.discovery_method
                    else None
                ),
            },
            "total": self.total_count,
            "succeeded": self.success_count,
            "already_exists": self.already_exists_count,
            "refreshed": self.refreshed_count,
            "verified": self.verified_count,
            "failed": self.failed_count,
            "skipped": self.skipped_count,
            "success_rate": round(self.success_rate, 2),
            "duration_s": round(self.duration_seconds, 3),
            "config": {
                "skip_archived": self.config.skip_archived,
                "threads": self.config.effective_threads,
                "depth": self.config.depth,
                "branch": self.config.branch,
                "strict_host_checking": self.config.strict_host_checking,
                "path": str(self.config.path),
            },
            "results": [result.to_dict() for result in self.results],
        }
