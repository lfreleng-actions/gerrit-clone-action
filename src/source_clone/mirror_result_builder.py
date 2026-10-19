# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""Translation of clone results into mirror results during the push phase.

Turns each :class:`~source_clone.models.CloneResult` into a
:class:`~source_clone.mirror_models.MirrorResult` by pushing the local
clone to its GitHub counterpart, and reports progress across the batch.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from source_clone.github_api import transform_gerrit_name_to_github
from source_clone.logging import get_logger
from source_clone.mirror_models import MirrorResult, MirrorStatus
from source_clone.models import CloneStatus

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from source_clone.github_api import GitHubRepo

logger = get_logger(__name__)


@dataclass(frozen=True)
class MirrorPushContext:
    """Settings and collaborators used to push one clone to GitHub."""

    github_org: str
    recreate: bool
    push: Callable[[Path, GitHubRepo], tuple[bool, str | None]]


def _failed_result(
    clone_result: Any,
    github_name: str,
    started_at: datetime,
    error_message: str,
    github_url: str = "",
) -> MirrorResult:
    """Build a FAILED MirrorResult ending at the current time."""
    completed_at = datetime.now(UTC)
    return MirrorResult(
        project=clone_result.project,
        github_name=github_name,
        github_url=github_url,
        status=MirrorStatus.FAILED,
        local_path=clone_result.path,
        duration_seconds=(completed_at - started_at).total_seconds(),
        error_message=error_message,
        started_at=started_at,
        completed_at=completed_at,
    )


def _skipped_result(
    clone_result: Any,
    github_name: str,
    started_at: datetime,
    github_url: str,
) -> MirrorResult:
    """Build a SKIPPED MirrorResult ending at the current time."""
    completed_at = datetime.now(UTC)
    return MirrorResult(
        project=clone_result.project,
        github_name=github_name,
        github_url=github_url,
        status=MirrorStatus.SKIPPED,
        local_path=clone_result.path,
        duration_seconds=(completed_at - started_at).total_seconds(),
        started_at=started_at,
        completed_at=completed_at,
    )


def _push_existing_repo(
    clone_result: Any,
    github_name: str,
    started_at: datetime,
    ctx: MirrorPushContext,
    github_repo: GitHubRepo,
) -> MirrorResult:
    """Push a clone to an already-resolved GitHub repository."""
    push_success, push_error = ctx.push(clone_result.path, github_repo)
    if not push_success:
        return _failed_result(
            clone_result,
            github_name,
            started_at,
            f"Push failed: {push_error}",
            github_url=github_repo.html_url,
        )

    completed_at = datetime.now(UTC)
    return MirrorResult(
        project=clone_result.project,
        github_name=github_name,
        github_url=github_repo.html_url,
        status=MirrorStatus.SUCCESS,
        local_path=clone_result.path,
        duration_seconds=(completed_at - started_at).total_seconds(),
        started_at=started_at,
        completed_at=completed_at,
    )


def build_mirror_result(
    clone_result: Any,
    repos_lookup: dict[str, GitHubRepo],
    ctx: MirrorPushContext,
) -> MirrorResult:
    """Convert a CloneResult to MirrorResult by pushing to GitHub.

    This optimized version uses pre-fetched data to avoid individual API calls.

    Args:
        clone_result: Result from CloneManager clone operation
        repos_lookup: Map of repo names to GitHubRepo objects (created/reused)
        ctx: Target organisation, recreate flag and push callable

    Returns:
        MirrorResult with GitHub push status
    """
    started_at = datetime.now(UTC)
    github_name = transform_gerrit_name_to_github(clone_result.project.name)

    # If clone failed, return failed mirror result
    if not clone_result.success:
        return _failed_result(
            clone_result,
            github_name,
            started_at,
            f"Clone failed: {clone_result.error_message}",
        )

    # If clone was skipped, mark as skipped
    if clone_result.status == CloneStatus.ALREADY_EXISTS and not ctx.recreate:
        logger.info(
            f"Repository already exists: {clone_result.project.name}, "
            f"skipping GitHub push (use --recreate to update)"
        )
        return _skipped_result(
            clone_result,
            github_name,
            started_at,
            f"https://github.com/{ctx.github_org}/{github_name}",
        )

    try:
        # Get GitHub repo from lookup (was created/reused in batch)
        github_repo = repos_lookup.get(github_name)
        if not github_repo:
            # This shouldn't happen, but handle gracefully
            error_msg = (
                f"Repository {github_name} not found in lookup after batch operations"
            )
            logger.error(error_msg)
            return _failed_result(clone_result, github_name, started_at, error_msg)

        # Push to GitHub
        return _push_existing_repo(
            clone_result, github_name, started_at, ctx, github_repo
        )

    except Exception as e:
        logger.error(f"Mirror failed for {clone_result.project.name}: {e}")
        return _failed_result(clone_result, github_name, started_at, str(e))


def _log_push_outcome(
    mirror_result: MirrorResult,
    index: int,
    total: int,
) -> None:
    """Report the outcome of a single push with a status icon."""
    if mirror_result.status == MirrorStatus.SUCCESS:
        logger.info(
            "✅ [%d/%d] Pushed %s -> %s (%.1fs)",
            index,
            total,
            mirror_result.project.name,
            mirror_result.github_name,
            mirror_result.duration_seconds,
        )
    elif mirror_result.status == MirrorStatus.SKIPPED:
        logger.info(
            "⏭️  [%d/%d] Skipped %s",
            index,
            total,
            mirror_result.project.name,
        )
    else:
        logger.warning(
            "❌ [%d/%d] Failed %s: %s",
            index,
            total,
            mirror_result.project.name,
            mirror_result.error_message or "unknown error",
        )


def run_push_phase(
    clone_results: list[Any],
    total: int,
    push_one: Callable[[Any], MirrorResult],
) -> list[MirrorResult]:
    """Push every clone in order, reporting per-item and periodic progress.

    Args:
        clone_results: Results from the clone phase, in push order
        total: Number of projects in the batch (used for progress counts)
        push_one: Callable converting one clone result to a mirror result

    Returns:
        Mirror results in the same order as *clone_results*.
    """
    logger.info("📤 Pushing repositories to GitHub...")
    mirror_results: list[MirrorResult] = []
    push_success = 0
    push_failed = 0
    push_skipped = 0
    report_every = max(1, len(clone_results) // 10)

    for clone_result in clone_results:
        mirror_result = push_one(clone_result)
        mirror_results.append(mirror_result)

        # Track and report per-item status with clear icons
        idx = len(mirror_results)
        if mirror_result.status == MirrorStatus.SUCCESS:
            push_success += 1
        elif mirror_result.status == MirrorStatus.SKIPPED:
            push_skipped += 1
        else:
            push_failed += 1
        _log_push_outcome(mirror_result, idx, total)

        # Periodic summary so long-running batches show aggregate progress
        if idx % report_every == 0 and idx < total:
            logger.info(
                "📊 Push progress: %d/%d completed "
                "(%d succeeded, %d failed, %d skipped)",
                idx,
                total,
                push_success,
                push_failed,
                push_skipped,
            )

    logger.info(
        "📊 Push complete: %d/%d succeeded, %d failed, %d skipped",
        push_success,
        total,
        push_failed,
        push_skipped,
    )
    return mirror_results
