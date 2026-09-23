# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""What becomes of a destination when its batch gives up.

Two answers, and the choice between them: remove what a killed clone
left behind, or keep the destination reserved for a worker that is
still writing to it.  Both turn on the reservations in
:mod:`gerrit_clone.clone_reservations`, never on the state of the
directory -- ``git clone`` creates ``.git`` before it transfers
anything, so a clone killed moments after starting looks complete.

The reporting that drives these decisions is in
:mod:`gerrit_clone.clone_timeout`.
"""

from __future__ import annotations

import shutil
from typing import TYPE_CHECKING

from gerrit_clone.clone_reservations import (
    hold_back_claim,
    holds_reservation,
    paths_owned_by,
    reservation_beneath,
)
from gerrit_clone.logging import get_logger
from gerrit_clone.models import SourceType
from gerrit_clone.pathing import get_project_path

if TYPE_CHECKING:
    from concurrent.futures import Future
    from pathlib import Path

    from gerrit_clone.models import CloneResult, Config, Project

logger = get_logger(__name__)


def _reserved_target(
    project: Project, config: Config, generation: int | None
) -> Path | None:
    """The destination *project* reserved, if it still holds it.

    Resolving the path does not establish ownership.  Two projects in a
    batch can resolve to one directory -- ``repo`` and ``repo.`` do --
    and only the one that took the reservation may have its directory
    removed or held back for it.  The other has already been refused its
    clone, and its future finishing says nothing about the owner.

    Args:
        project: Project being accounted for
        config: Active configuration, supplying the workspace root
        generation: Batch the reservation would belong to

    Returns:
        The reserved destination, or ``None`` if this clone holds none.
    """
    if generation is None:
        return None
    target_path = _target_path(project, config)
    if target_path is None:
        return None
    if not holds_reservation(target_path, (generation, project.name)):
        return None
    return target_path


def _target_path(project: Project, config: Config) -> Path | None:
    """Resolve a project's clone destination, or ``None`` if it is invalid.

    Resolved the same way the worker that writes it does.  The GitHub
    path clones to ``config.path / project.filesystem_path`` while the
    Gerrit path sanitises the name first, so assuming one of them would
    have this inspecting a directory nothing was ever written to.
    """
    try:
        if config.source_type == SourceType.GITHUB:
            return config.path / project.filesystem_path
        return get_project_path(project.name, config.path)
    except Exception as exc:
        logger.debug(f"Could not resolve path for {project.name}: {exc}")
        return None


def _completed_successfully(future: Future[CloneResult]) -> bool:
    """Whether *future* finished with a successful clone.

    A future can complete after ``as_completed`` gave up on it, so a
    project can look outstanding while its clone in fact succeeded.  The
    future is the authority on that; the state of the directory is not.
    ``git clone`` creates ``.git`` before it has transferred anything,
    so a clone killed moments after starting looks like a repository.
    """
    if not future.done() or future.cancelled():
        return False
    try:
        return future.result(timeout=0).success
    except Exception:
        return False


def discard_partial_clone(
    project: Project,
    target_path: Path | None,
    generation: int | None = None,
    also_discarding: frozenset[Path] | set[Path] | None = None,
) -> None:
    """Remove a half-written clone left by an abandoned worker.

    ``git clone`` removes its own target directory when it is
    terminated, and the GitHub path clones through a temporary
    directory, so this normally finds nothing.  It matters when the
    child had to be killed outright: a leftover directory would make the
    next run report "directory exists but is not a git repository" for
    that project, or worse, be mistaken for a complete clone.

    The path must be one this batch owns -- created by a worker, or
    cleared through conflict resolution and cloned into.  Ownership is
    recorded when the destination is taken, not inferred from it having
    been absent earlier: another batch, or an unrelated process, can
    create a destination between such a check and the clone.

    Args:
        project: Project whose clone was abandoned
        target_path: Destination this clone reserved, or ``None`` if it
            holds no reservation and so may remove nothing
        generation: Batch the reservation belongs to, used to tell this
            clone's own reservation from the ones it must not destroy.
            Omitted by default, which only weakens the nesting check.
        also_discarding: Destinations the same pass is removing, which
            therefore do not protect anything they sit inside.  Omitted
            by default, which only makes the check more cautious.
    """
    if target_path is None or not target_path.is_dir():
        return

    # Removal takes the whole tree, so a reservation held by anything
    # else beneath this one would be destroyed with it.  A batch can
    # legitimately hold both a project and a project nested inside it,
    # and the inner clone may well have succeeded while the outer one
    # timed out.  Reservations outlive this cleanup precisely so that
    # they can still be seen here.
    if generation is not None:
        nested = reservation_beneath(
            target_path, (generation, project.name), also_discarding
        )
        if nested is not None:
            logger.warning(
                f"Not removing partial clone for {project.name}: {nested} "
                f"beneath it is reserved by another clone"
            )
            return

    try:
        shutil.rmtree(target_path)
        logger.debug(f"Removed partial clone for {project.name}")
    except OSError as exc:
        logger.warning(f"Could not remove partial clone for {project.name}: {exc}")


def _hold_back(
    project: Project,
    target_path: Path | None,
    generation: int | None,
    future: Future[CloneResult],
    also_held_back: set[Path] | None = None,
) -> None:
    """Keep a still-running worker's destinations reserved for it.

    Every destination the clone holds is kept, not only the one it
    writes to: conflict resolution also reserves wherever it moved an
    obstruction aside, and releasing that while the rename is still to
    come would let a later batch take a path this worker can still
    write to.  Only the clone target is ever discarded, and only when
    the clone failed: a backup holds content moved out of the way, not a
    partial clone.

    Discarding on completion is what makes the handover safe against a
    worker finishing just as it is handed over: the callback fires
    immediately in that case, and the cleanup the batch would otherwise
    have skipped still happens.

    A destination containing another held-back one is not discarded at
    all.  These workers finish independently and cannot see each other's
    outcome, and a nested clone that succeeds releases its reservation
    as it goes -- so an ancestor failing afterwards would find nothing
    beneath it and take that finished repository with it.  The ancestor
    gives up its own cleanup instead, leaving a directory the next run
    classifies as incomplete and clears.

    Args:
        project: Project whose worker outlasted the settle wait
        target_path: Destination this clone reserved, or ``None`` if it
            holds none and so has nothing to keep
        generation: Batch the reservation belongs to
        future: The worker's future, which releases the reservations
            once it completes
        also_held_back: Destinations of the batch's other still-running
            workers, whose outcomes are not yet known
    """
    if generation is None:
        return
    owner = (generation, project.name)
    others = [path for path in paths_owned_by(owner) if path != target_path]
    if target_path is None and not others:
        return

    logger.warning(f"Leaving {project.name} in place; its worker is still running")
    for path in others:
        hold_back_claim(path, owner, future)
    if target_path is None:
        return

    nested = next(
        (
            path
            for path in also_held_back or ()
            if path != target_path and path.is_relative_to(target_path)
        ),
        None,
    )
    if nested is not None:
        logger.warning(
            f"Not scheduling cleanup for {project.name}: {nested} beneath it "
            f"is still being cloned, and may yet succeed"
        )
        hold_back_claim(target_path, owner, future)
        return

    def discard_if_failed(completed: Future[CloneResult]) -> None:
        # Runs while the reservation is still held, so the destination
        # cannot have been taken by anybody else in the meantime.
        if not _completed_successfully(completed):
            discard_partial_clone(project, target_path, generation)

    hold_back_claim(target_path, owner, future, before_release=discard_if_failed)


def hold_back_running_claims(
    config: Config,
    future_to_project: dict[Future[CloneResult], Project],
    generation: int | None,
) -> None:
    """Keep reservations for workers this batch has not seen stop.

    A batch normally gives its reservations back as it leaves, but it
    can leave through a non-waiting shutdown -- the abandon that follows
    a timeout, or the ``KeyboardInterrupt`` that
    :func:`gerrit_clone.concurrent_utils.interruptible_executor`
    re-raises.  Workers outlive both, and may still be finalizing an
    atomic clone or rewriting a remote.

    Releasing those destinations on the way out would let a batch
    started by a caller that caught the interrupt reserve a path another
    worker is still writing to.  They are held back instead, and each is
    given up by its own worker's completion callback.

    Args:
        config: Active configuration, supplying the workspace root
        future_to_project: Every future submitted for this batch
        generation: Batch whose reservations are being kept
    """
    if generation is None:
        return
    running = [
        (future, project)
        for future, project in future_to_project.items()
        if not future.done()
    ]
    held_back = {
        reserved
        for _future, project in running
        if (reserved := _reserved_target(project, config, generation)) is not None
    }
    for future, project in running:
        _hold_back(
            project,
            _reserved_target(project, config, generation),
            generation,
            future,
            held_back,
        )
