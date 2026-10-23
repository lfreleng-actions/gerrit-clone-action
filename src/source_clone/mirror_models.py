# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""Result and status types for Gerrit-to-GitHub mirror operations.

Holds the serialisable value objects produced by the mirror manager:
the per-repository :class:`MirrorResult`, the batch-level
:class:`MirrorBatchResult`, and the :class:`MirrorStatus` enumeration
they share.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from pathlib import Path

    from source_clone.models import Project


class MirrorStatus(StrEnum):
    """Status values for mirror operations."""

    PENDING = "pending"
    CLONING = "cloning"
    PUSHING = "pushing"
    SUCCESS = "success"
    FAILED = "failed"
    SKIPPED = "skipped"
    ALREADY_EXISTS = "already_exists"


@dataclass
class MirrorResult:
    """Result of mirroring a single repository."""

    project: Project
    github_name: str
    github_url: str
    status: str
    local_path: Path
    duration_seconds: float = 0.0
    error_message: str | None = None
    started_at: datetime | None = None
    completed_at: datetime | None = None
    attempts: int = 1

    @property
    def success(self) -> bool:
        """Check if mirror was successful."""
        return self.status in (MirrorStatus.SUCCESS, MirrorStatus.SKIPPED)

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary for JSON serialization."""
        return {
            "gerrit_project": self.project.name,
            "github_name": self.github_name,
            "github_url": self.github_url,
            "status": self.status,
            "local_path": str(self.local_path),
            "duration_s": round(self.duration_seconds, 3),
            "error": self.error_message,
            "started_at": self.started_at.isoformat() if self.started_at else None,
            "completed_at": self.completed_at.isoformat()
            if self.completed_at
            else None,
            "attempts": self.attempts,
        }


@dataclass
class MirrorBatchResult:
    """Results of a batch mirror operation."""

    results: list[MirrorResult]
    started_at: datetime
    completed_at: datetime | None = None
    github_org: str | None = None
    gerrit_host: str | None = None

    @property
    def total_count(self) -> int:
        """Total number of projects processed."""
        return len(self.results)

    @property
    def success_count(self) -> int:
        """Number of successful mirrors."""
        return sum(1 for r in self.results if r.success)

    @property
    def failed_count(self) -> int:
        """Number of failed mirrors."""
        return sum(1 for r in self.results if r.status == MirrorStatus.FAILED)

    @property
    def skipped_count(self) -> int:
        """Number of skipped mirrors."""
        return sum(1 for r in self.results if r.status == MirrorStatus.SKIPPED)

    @property
    def duration_seconds(self) -> float:
        """Total duration of batch operation."""
        if self.completed_at is None:
            return 0.0
        return (self.completed_at - self.started_at).total_seconds()

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary for JSON serialization."""
        return {
            "version": "1.0",
            "generated_at": (self.completed_at or datetime.now(UTC)).isoformat(),
            "github_org": self.github_org,
            "gerrit_host": self.gerrit_host,
            "total": self.total_count,
            "succeeded": self.success_count,
            "failed": self.failed_count,
            "skipped": self.skipped_count,
            "duration_s": round(self.duration_seconds, 3),
            "results": [r.to_dict() for r in self.results],
        }
