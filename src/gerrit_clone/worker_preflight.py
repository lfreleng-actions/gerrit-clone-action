# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""Deciding whether a clone may go ahead, and preparing its destination.

Everything here happens before git is invoked: working out whether the
project sits inside another one, settling what is already on disk,
taking the reservation, and creating the parent directories.  Each of
those can end the clone before it starts, so the outcome is either a
finished result or permission to continue.

Order matters throughout, and the reasons are recorded against the
steps themselves.  The reservation in particular is taken before
anything is written, so that a losing worker cannot disturb a
destination -- or an ancestor of one -- that another clone owns.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, NamedTuple

from gerrit_clone.clone_conflicts import resolve_path_conflict
from gerrit_clone.clone_nesting import (
    annotate_nested_parent,
    apply_nested_protection,
    find_project_git_ancestor,
    reject_nested_clone,
)
from gerrit_clone.clone_reservations import claim_new_target, reserved_by_other
from gerrit_clone.logging import get_logger
from gerrit_clone.pathing import check_path_conflicts
from gerrit_clone.subprocess_tracking import unless_abandoned

if TYPE_CHECKING:
    from datetime import datetime
    from pathlib import Path

    from gerrit_clone.models import CloneResult, Config, Project

logger = get_logger(__name__)


class Preflight(NamedTuple):
    """What the pre-clone checks decided.

    Attributes:
        finished: A result to return immediately, or ``None`` to go on
            and clone.
        ancestor: The cloned project this one sits inside, if any.
        allow_nested: Whether nesting is permitted by configuration,
            carried through because the caller reports on it once the
            clone has succeeded.
    """

    finished: CloneResult | None
    ancestor: Path | None
    allow_nested: bool


class PreflightRefused(RuntimeError):
    """Raised when another clone holds the destination.

    Distinct from the ordinary failure path: nothing is wrong with this
    project, it simply must not write where somebody else is writing.
    """


def run_preflight(
    config: Config,
    project: Project,
    target_path: Path,
    result: CloneResult,
    project_index: set[str],
    started_at: datetime,
) -> Preflight:
    """Settle everything that must be true before git runs.

    Args:
        config: Active configuration.
        project: Project being cloned.
        target_path: Where it will be cloned to.
        result: Result being built, annotated in place.
        project_index: Every project name in the run, so an ancestor is
            only believed when it is really being cloned.
        started_at: When this clone began, for an early result.

    Returns:
        The decision, with a finished result when the clone must stop.

    Raises:
        PreflightRefused: If another clone holds the destination.
    """
    depth = project.name.count("/")

    ancestor_repo = find_project_git_ancestor(target_path, config.path, project_index)

    # Handle nested repositories (always clone both parent and children)
    allow_nested = getattr(config, "allow_nested_git", False)
    nested_protection = getattr(config, "nested_protection", False)

    if ancestor_repo and not allow_nested:
        return Preflight(
            reject_nested_clone(result, ancestor_repo, started_at),
            ancestor_repo,
            allow_nested,
        )

    if ancestor_repo and allow_nested:
        annotate_nested_parent(result, ancestor_repo, config.path, project.name)
    elif depth > 0:
        logger.debug(
            f"No early ancestor detected for candidate nested project "
            f"{project.name} (depth={depth})"
        )

    is_nested = result.nested_under is not None

    # Asked before what is on disk is classified.  While another clone
    # is working here its directory is in flux -- git creates .git
    # before transferring anything -- so a conflict verdict could
    # describe a repository whose owner's cleanup is about to remove it.
    _refuse_if_contested(target_path, project.name)

    conflict = check_path_conflicts(target_path, is_nested_repo=is_nested)
    if conflict is not None:
        # Asked again now that something has been found.  A clone always
        # reserves before it creates anything, so a directory put there
        # by a rival has a reservation taken strictly earlier -- which
        # this second look finds and the first could not, it having run
        # while the path was still absent.  Accepting the verdict
        # instead would report a clone in progress as a finished
        # repository.
        _refuse_if_contested(target_path, project.name)

        if resolve_path_conflict(
            conflict,
            result,
            started_at,
            getattr(config, "move_conflicting", True),
        ):
            return Preflight(result, ancestor_repo, allow_nested)

    # Reserved before anything is written: mkdir(parents=True) can bring
    # into being an ancestor another batch reserved, and nested
    # protection writes into a parent repository.  Re-reserving is how a
    # retry continues, so the attempt later asking again for a path it
    # already holds is answered rather than refused.
    claim_new_target(target_path, project.name)

    # Ensure parent directories exist (safe due to dependency batching).
    # Created atomically with the abandonment check: a batch giving up
    # between a check and the write would leave a directory behind it.
    with unless_abandoned(f"Creating the parent of {project.name}"):
        target_path.parent.mkdir(parents=True, exist_ok=True)

    # If nested and protection enabled, add child path to parent exclude
    if ancestor_repo and allow_nested and nested_protection:
        apply_nested_protection(
            ancestor_repo, target_path, project.name, result.nested_under
        )

    # Instrumentation: a project that looks nested but has no parent yet.
    # The worker re-checks immediately before the clone subprocess, by
    # which time the parent's batch may have finished.
    if depth > 0 and result.nested_under is None:
        logger.debug(
            f"Nested candidate (no parent yet): {project.name} "
            f"(will re-check before clone subprocess)"
        )

    return Preflight(None, ancestor_repo, allow_nested)


def _refuse_if_contested(target_path: Path, project_name: str) -> None:
    """Stand down if another clone holds *target_path*.

    Args:
        target_path: Destination being considered.
        project_name: Project asking for it.

    Raises:
        PreflightRefused: If somebody else holds it.
    """
    contested = reserved_by_other(target_path, project_name)
    if contested is not None:
        raise PreflightRefused(f"{contested} is already being cloned")
