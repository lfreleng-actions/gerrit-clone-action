# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""Resolution of filesystem conflicts discovered before a clone starts.

``check_path_conflicts`` classifies what is already sitting at a target path;
this module decides what to do about each classification.  Every handler either
finishes the :class:`CloneResult` and reports that the caller should stop, or
clears the obstruction and reports that the clone may proceed.
"""

from __future__ import annotations

import shutil
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from gerrit_clone.clone_timeout import TargetOwnedError, claim_target_path
from gerrit_clone.logging import get_logger
from gerrit_clone.models import CloneStatus
from gerrit_clone.pathing import move_conflicting_path

if TYPE_CHECKING:
    from gerrit_clone.models import CloneResult

logger = get_logger(__name__)


def _finalize(result: CloneResult, started_at: datetime) -> None:
    """Stamp completion time and duration onto a terminal result.

    Args:
        result: Result being finished
        started_at: Time the clone attempt began
    """
    completed_at = datetime.now(UTC)
    result.completed_at = completed_at
    result.duration_seconds = (completed_at - started_at).total_seconds()


def _reserve_or_refuse(result: CloneResult, started_at: datetime) -> bool:
    """Reserve the destination before anything at it is disturbed.

    Clearing a path destroys what is there, so the reservation comes
    first.  Only one batch can hold a destination; the ones refused have
    lost the race and leave the winner's clone exactly as they found it,
    rather than deleting it and discovering the loss afterwards.

    Args:
        result: Result to update if the reservation is refused
        started_at: Time the clone attempt began

    Returns:
        True if another batch owns the path and the caller should stop
    """
    try:
        claim_target_path(result.path, result.project.name)
    except TargetOwnedError as exc:
        result.status = CloneStatus.FAILED
        result.error_message = str(exc)
        _finalize(result, started_at)
        logger.error(
            f"Refusing to clear {result.path} for {result.project.name}: {exc}"
        )
        return True
    return False


def _mark_already_exists(result: CloneResult, started_at: datetime) -> bool:
    """Record that the repository was already cloned.

    Args:
        result: Result to update
        started_at: Time the clone attempt began

    Returns:
        True, since there is nothing left to clone
    """
    result.status = CloneStatus.ALREADY_EXISTS
    _finalize(result, started_at)
    logger.debug(f"✓ Repository {result.project.name} already exists - skipped")
    return True


def _clean_incomplete_clone(result: CloneResult, started_at: datetime) -> bool:
    """Remove leftover content so the clone can be retried cleanly.

    The same conflict code covers two situations: a genuinely half-finished
    clone, and a directory that a parent repository legitimately populated at
    the path a nested child now needs.

    Args:
        result: Result to update on failure
        started_at: Time the clone attempt began

    Returns:
        True if cleanup failed and the caller should stop, False to continue
    """
    target_path = result.path
    project_name = result.project.name

    # Whatever occupies this destination from here is this clone's, so a
    # timeout must be able to discard it.  Taken before the removal, so
    # a losing batch never touches the winner's directory.
    if _reserve_or_refuse(result, started_at):
        return True

    if result.nested_under:
        logger.debug(
            f"🧹 Replacing parent repository content with nested repository for {project_name}"
        )
    else:
        logger.warning(f"🧹 Cleaning up incomplete clone for {project_name}")

    try:
        shutil.rmtree(target_path)
        logger.debug(f"✓ Cleaned up incomplete clone directory: {target_path}")
    except Exception as cleanup_error:
        result.status = CloneStatus.FAILED
        result.error_message = f"Failed to cleanup incomplete clone: {cleanup_error}"
        _finalize(result, started_at)
        logger.error(f"Cleanup failed for {project_name}: {cleanup_error}")
        return True

    # Continue with normal clone after cleanup
    return False


def _skip_nested_file_conflict(
    result: CloneResult, started_at: datetime, reason: str, detail: str
) -> bool:
    """Skip a nested repository blocked by a file in its parent.

    Args:
        result: Result to update
        started_at: Time the clone attempt began
        reason: Error message recorded on the result
        detail: Trailing clause appended to the operator-facing warning

    Returns:
        True, since the project is being skipped
    """
    result.status = CloneStatus.SKIPPED
    result.error_message = reason
    _finalize(result, started_at)
    parent_name = result.nested_under or "parent"
    logger.warning(
        f"⚠️ Skipping nested repository [project]{result.project.name}[/project]: "
        f"Parent repository '{parent_name}' contains a file that conflicts with nested directory structure{detail}"
    )
    return True


def _resolve_nested_file_conflict(
    result: CloneResult, started_at: datetime, move_conflicting_enabled: bool
) -> bool:
    """Try to move a parent-owned file out of a nested repository's way.

    Args:
        result: Result to update
        started_at: Time the clone attempt began
        move_conflicting_enabled: Whether moving the conflicting path is allowed

    Returns:
        True if the project should be skipped, False to continue cloning
    """
    if not move_conflicting_enabled:
        # Move conflicting disabled, skip gracefully
        return _skip_nested_file_conflict(
            result,
            started_at,
            "Skipped due to file conflict with parent repository",
            "",
        )

    # Moving the obstruction is destructive too, so the destination is
    # reserved before it happens, as above.
    if _reserve_or_refuse(result, started_at):
        return True

    try:
        # Try to move the conflicting file/directory
        if move_conflicting_path(result.path, _is_nested_repo=True):
            parent_name = result.nested_under or "parent"
            logger.warning(
                f"⚠️ Moved conflicting content in parent repository '{parent_name}' to allow cloning of nested repository [project]{result.project.name}[/project]"
            )
            # Continue with normal clone after moving conflict
            return False

        # Move failed, skip gracefully
        return _skip_nested_file_conflict(
            result,
            started_at,
            "Skipped due to file conflict with parent repository (move failed)",
            " (could not move)",
        )
    except Exception as move_error:
        # Move failed with exception, skip gracefully
        return _skip_nested_file_conflict(
            result,
            started_at,
            f"Skipped due to file conflict with parent repository (move error: {move_error})",
            f" (move failed: {move_error})",
        )


def _fail_unknown_conflict(
    result: CloneResult, started_at: datetime, conflict: str
) -> bool:
    """Fail the clone for a conflict this module does not know how to clear.

    Args:
        result: Result to update
        started_at: Time the clone attempt began
        conflict: Conflict code reported by ``check_path_conflicts``

    Returns:
        True, since the clone cannot proceed
    """
    result.status = CloneStatus.FAILED
    result.error_message = f"Path conflict: {conflict}"
    _finalize(result, started_at)
    logger.error(
        f"Path conflict for [project]{result.project.name}[/project]: {conflict}"
    )
    return True


def resolve_path_conflict(
    conflict: str,
    result: CloneResult,
    started_at: datetime,
    move_conflicting_enabled: bool,
) -> bool:
    """Handle whatever is already occupying the clone target path.

    Args:
        conflict: Conflict code reported by ``check_path_conflicts``
        result: Result to update (mutated in place for terminal outcomes)
        started_at: Time the clone attempt began
        move_conflicting_enabled: Whether moving a conflicting path is allowed

    Returns:
        True if the caller should return *result* immediately, False if the
        obstruction was cleared and the clone should proceed
    """
    if conflict == "already_cloned":
        return _mark_already_exists(result, started_at)
    if conflict == "incomplete_clone":
        return _clean_incomplete_clone(result, started_at)
    if conflict == "nested_file_conflict":
        return _resolve_nested_file_conflict(
            result, started_at, move_conflicting_enabled
        )
    return _fail_unknown_conflict(result, started_at, conflict)
