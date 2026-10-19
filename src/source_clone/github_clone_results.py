# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""Construction of GitHub clone results.

Every clone attempt finishes by recording its outcome and elapsed time;
this module centralises that so the clone paths only decide the status
and the error message.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

from source_clone.models import CloneResult

if TYPE_CHECKING:
    from pathlib import Path

    from source_clone.models import CloneStatus, Project


def build_clone_result(
    project: Project,
    target_path: Path,
    started_at: datetime,
    status: CloneStatus,
    error_message: str | None = None,
) -> CloneResult:
    """Build a clone result that completes at the current time.

    Args:
        project: Project that was cloned
        target_path: Final clone path
        started_at: When the clone attempt began
        status: Outcome of the attempt
        error_message: Failure detail, when the attempt did not succeed

    Returns:
        CloneResult with ``completed_at`` and ``duration_seconds`` set.
    """
    completed_at = datetime.now(UTC)
    return CloneResult(
        project=project,
        status=status,
        path=target_path,
        error_message=error_message,
        started_at=started_at,
        completed_at=completed_at,
        duration_seconds=(completed_at - started_at).total_seconds(),
    )
