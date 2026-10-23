# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""Refresh stage helpers for repositories that already exist locally.

Decides whether a checkout is already at the remote SHA and drives the
per-repository refresh loop (with its Rich progress display).  The
pieces that resolve ``RefreshWorker`` / ``get_current_commit_sha`` stay
in :mod:`source_clone.clone_manager` so those remain patchable there.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

from rich.console import Console
from rich.progress import (
    BarColumn,
    MofNCompleteColumn,
    Progress,
    SpinnerColumn,
    TextColumn,
    TimeElapsedColumn,
)

from source_clone.clone_results import (
    build_refresh_failure_result,
    build_refresh_result,
)
from source_clone.logging import get_logger

if TYPE_CHECKING:
    from pathlib import Path

    from source_clone.models import CloneResult, Config, Project
    from source_clone.refresh_worker import RefreshWorker

logger = get_logger(__name__)


def decide_refresh_from_shas(
    project_name: str, local_sha: str | None, remote_sha: str | None
) -> bool:
    """Decide whether a repository needs refreshing from its SHAs.

    Args:
        project_name: Project name, used only for log messages.
        local_sha: SHA of the local checkout, if it could be read.
        remote_sha: SHA recorded in the project metadata, if available.

    Returns:
        ``True`` when the repository needs a refresh.
    """
    if not remote_sha and not local_sha:
        # Both None: Empty repository with no commits
        # No refresh needed since there's nothing to pull
        logger.debug(f"✓ {project_name}: up-to-date (empty repository, no commits)")
        return False

    if remote_sha and local_sha and local_sha == remote_sha:
        # SHAs match: Repository is up to date
        # Safe to slice here because we know local_sha is not None
        logger.debug(f"✓ {project_name}: up-to-date ({local_sha[:8]})")
        return False

    # SHAs differ or one is missing: Needs refresh
    if not remote_sha:
        logger.debug(f"↻ {project_name}: needs refresh (no remote SHA available)")
    elif not local_sha:
        logger.debug(f"↻ {project_name}: needs refresh (no local SHA available)")
    else:
        logger.debug(
            f"↻ {project_name}: needs refresh (local: {local_sha[:8]}, remote: {remote_sha[:8]})"
        )
    return True


def _refresh_one(
    refresh_worker: RefreshWorker,
    project: Project,
    target_path: Path,
    refresh_start: datetime,
) -> CloneResult:
    """Refresh a single repository, converting any failure to a result."""
    try:
        # Refresh single repository using the worker
        refresh_result = refresh_worker.refresh_repository(target_path)
        # Convert RefreshResult to CloneResult
        return build_refresh_result(project, target_path, refresh_result, refresh_start)
    except Exception as e:
        # If refresh fails, create a failed result with clear context
        logger.warning(f"Failed to refresh {project.name}: {e}")
        return build_refresh_failure_result(project, target_path, refresh_start, e)


def run_refresh_with_progress(
    config: Config,
    refresh_worker: RefreshWorker,
    repos_needing_refresh: list[Project],
) -> list[CloneResult]:
    """Refresh each repository with a progress display.

    Args:
        config: Active configuration (supplies the output path).
        refresh_worker: Worker performing the individual refreshes.
        repos_needing_refresh: Repositories that need updating.

    Returns:
        One :class:`CloneResult` per refreshed repository, in order.
    """
    results: list[CloneResult] = []
    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(bar_width=40),
        MofNCompleteColumn(),
        TextColumn("•"),
        TimeElapsedColumn(),
        console=Console(stderr=True),
        transient=False,
    ) as progress:
        task = progress.add_task(
            "Refreshing repositories...",
            total=len(repos_needing_refresh),
        )

        for project in repos_needing_refresh:
            target_path = config.path / project.filesystem_path
            refresh_start = datetime.now(UTC)

            progress.update(task, description=f"Refreshing {project.name}")
            results.append(
                _refresh_one(refresh_worker, project, target_path, refresh_start)
            )

            # Advance progress
            progress.update(task, advance=1)

    return results
