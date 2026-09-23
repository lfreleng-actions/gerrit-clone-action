# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""Resolution of filesystem conflicts discovered before a clone starts.

``check_path_conflicts`` classifies what is already sitting at a target path;
this module decides what to do about each classification.  Every handler either
finishes the :class:`CloneResult` and reports that the caller should stop, or
clears the obstruction and reports that the clone may proceed.
"""

from __future__ import annotations

import contextlib
import shutil
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

from gerrit_clone.clone_reservations import (
    TargetOwnedError,
    claim_target_path,
    reservation_beneath,
)
from gerrit_clone.logging import get_logger
from gerrit_clone.models import CloneStatus
from gerrit_clone.pathing import move_conflicting_path
from gerrit_clone.subprocess_tracking import (
    ProcessAbandonedError,
    current_generation,
    unless_abandoned,
)

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

    # Clearing takes the whole tree, and a clone nested beneath this one
    # in the same batch -- the supported arrangement -- holds its own
    # reservation there.  Removing it would destroy that clone, finished
    # or not, so the clearing is refused instead.
    generation = current_generation()
    nested = (
        reservation_beneath(target_path, (generation, project_name))
        if generation is not None
        else None
    )
    if nested is not None:
        result.status = CloneStatus.FAILED
        result.error_message = (
            f"Refusing to clear {target_path}: {nested} beneath it is "
            f"reserved by another clone"
        )
        _finalize(result, started_at)
        logger.error(f"{project_name}: {result.error_message}")
        return True

    if result.nested_under:
        logger.debug(
            f"🧹 Replacing parent repository content with nested repository for {project_name}"
        )
    else:
        logger.warning(f"🧹 Cleaning up incomplete clone for {project_name}")

    try:
        # Taken out of the workspace atomically with the abandonment
        # check, then deleted outside it.  Removing it in place instead
        # would let a batch give up between a check and the removal and
        # leave this worker destroying content after the timeout path
        # has returned -- and holding the lock for a delete of any size
        # would stall every launch and the SIGTERM handler behind it.
        with unless_abandoned(f"Clearing {target_path}"):
            doomed = Path(
                tempfile.mkdtemp(
                    prefix=f".{target_path.name}.clearing-", dir=target_path.parent
                )
            )
            try:
                target_path.rename(doomed / target_path.name)
            except OSError:
                with contextlib.suppress(OSError):
                    doomed.rmdir()
                raise
    except ProcessAbandonedError:
        raise
    except Exception as cleanup_error:
        result.status = CloneStatus.FAILED
        result.error_message = f"Failed to cleanup incomplete clone: {cleanup_error}"
        _finalize(result, started_at)
        logger.error(f"Cleanup failed for {project_name}: {cleanup_error}")
        return True

    # The destination is clear from here.  A delete that fails puts what
    # is left back where it came from -- still reserved, so still this
    # clone's -- and fails the clone: left under its generated name it
    # would be known to nothing, whereas at the destination the next run
    # classifies it and clears it like any other incomplete clone.
    # Undoing this worker's own move, the restore is not refused after
    # abandonment; the timeout path treats the destination as this
    # clone's either way.
    try:
        shutil.rmtree(doomed)
    except OSError as exc:
        restored = False
        with contextlib.suppress(OSError):
            (doomed / target_path.name).rename(target_path)
            restored = True
            doomed.rmdir()
        where = "" if restored else f"; what is left is in {doomed}"
        result.status = CloneStatus.FAILED
        result.error_message = f"Failed to cleanup incomplete clone: {exc}{where}"
        _finalize(result, started_at)
        logger.error(f"Cleanup failed for {project_name}: {result.error_message}")
        return True
    logger.debug(f"✓ Cleaned up incomplete clone directory: {target_path}")

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

    def reserve_backup(candidate: Path) -> bool:
        # The obstruction is moved aside rather than deleted, so where
        # it lands is a destination like any other: finding the name
        # free says nothing about another clone being about to use it,
        # and this rename would then replace that clone's work.  A name
        # that cannot be taken is passed over for the next one, and a
        # name that is taken is given up with the rest at batch end.
        # A project whose own destination is the chosen name is refused
        # its clone rather than quietly overwritten, which is the same
        # answer the registry gives anywhere else.
        try:
            claim_target_path(candidate, result.project.name)
        except TargetOwnedError:
            return False
        return True

    try:
        # Try to move the conflicting file/directory
        if move_conflicting_path(
            result.path,
            _is_nested_repo=True,
            reserve=reserve_backup,
            # The rename alone, atomically with the abandonment check;
            # the backup name is reserved outside it.
            guard=lambda: unless_abandoned(f"Moving aside {result.path}"),
        ):
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
    except ProcessAbandonedError:
        raise
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

    Raises:
        ProcessAbandonedError: If the batch was abandoned before the
            obstruction could be cleared, which is then left untouched.
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
