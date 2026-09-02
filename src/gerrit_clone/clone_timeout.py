# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""Reporting for a clone batch that exceeded its overall timeout.

The batch abandons its executor first -- see
:meth:`gerrit_clone.concurrent_utils._TrackedThreadPoolExecutor.abandon`
-- and then comes here to account for the work that never finished.
"""

from __future__ import annotations

import shutil
import threading
from concurrent.futures import wait as wait_for_futures
from functools import partial
from typing import TYPE_CHECKING

from gerrit_clone.clone_results import build_failure_result
from gerrit_clone.logging import get_logger
from gerrit_clone.models import SourceType
from gerrit_clone.pathing import get_project_path
from gerrit_clone.subprocess_tracking import current_generation

if TYPE_CHECKING:
    from concurrent.futures import Future
    from pathlib import Path

    from gerrit_clone.models import CloneResult, Config, Project

logger = get_logger(__name__)

#: How long to let abandoned workers fall out of their post-clone work
#: before their directories are touched.  Their git children have
#: already been terminated, so this is milliseconds in practice; the
#: bound is there so a wedged worker cannot hold the batch, which is the
#: whole point of the timeout path.
_SETTLE_TIMEOUT_SECONDS = 5.0

#: Who holds a reservation: the batch, and the project within it.  The
#: batch alone would not be enough.  Name sanitisation is not injective
#: -- ``repo`` and ``repo.`` resolve to one directory -- while projects
#: are de-duplicated by name, so a single batch can carry two clones for
#: one destination.  Were they to share a reservation, a timeout in
#: either would discard the other's finished repository.
_Owner = tuple[int, str]

#: Destinations reserved by a batch: the ones a worker is about to
#: create, or has cleared through conflict resolution and is about to
#: clone into.  Only these may be discarded when a batch is abandoned.
#:
#: Ownership is positive and exclusive.  Absence before the batch is not
#: proof that the batch created what is there now -- two batches, or an
#: unrelated process, can create a destination between the check and the
#: clone -- so the *first* reservation wins and a worker that cannot
#: take one refuses to clone.  Exactly one clone therefore ever writes
#: to a destination, and it is the only one that can remove it.
#:
#: The registry is per-process, which is the scope this tool works in:
#: a workspace shared with another ``gerrit-clone`` process has no
#: locking of any kind, so guarding one dictionary would not make that
#: arrangement safe.
_owned_paths: dict[Path, _Owner] = {}

#: Reservations handed over to a worker that outlasted its batch's
#: settle wait.  They belong to that worker rather than to the batch
#: from here, so a batch-wide release steps over them and only the
#: worker's own completion gives them up.  Holding this here, rather
#: than passing a set back to the caller, is deliberate: the batch
#: releases its reservations from a ``finally`` that knows nothing about
#: the timeout path, and must not be able to undo this by omission.
_lingering: set[Path] = set()

_claimed_lock = threading.Lock()


class TargetOwnedError(RuntimeError):
    """Raised when another batch has already reserved a destination.

    Cloning anyway would have two batches writing to one path, and
    whichever finished second would decide what the other's timeout
    cleanup found there.
    """


def claim_target_path(path: Path, project: str) -> None:
    """Reserve *path* for this clone, or refuse it to the caller.

    Called where a worker has just cleared a destination through
    conflict resolution and is about to clone into it.

    Re-reserving is how a retry continues, so the same project asking
    again for a path it already holds is answered rather than refused.

    A caller outside a batch reserves nothing and is not refused: with
    no generation there is no batch to attribute the reservation to, so
    the conservative answer is simply that the path is not ours to
    delete.

    Args:
        path: Destination the worker is about to clone into.
        project: Name of the project being cloned there.

    Raises:
        TargetOwnedError: If another clone holds the reservation.
    """
    generation = current_generation()
    if generation is None:
        return
    owner = (generation, project)
    with _claimed_lock:
        # First reservation wins, and is handed back to every later
        # caller so it can stand down rather than write to a path it
        # does not own.
        held = _owned_paths.setdefault(path, owner)
    if held == owner:
        return
    if held[0] == generation:
        raise TargetOwnedError(
            f"{path} is already being cloned for {held[1]} in this batch"
        )
    raise TargetOwnedError(f"{path} is already being cloned by another batch")


def claim_new_target(path: Path, project: str) -> None:
    """Reserve *path* when the caller is about to create it.

    Reached only once the conflict checks have passed, so the
    destination was absent a moment ago.  Finding it there now means it
    appeared during this run: either this clone created it on an earlier
    attempt, in which case it carries on, or somebody else took the
    destination in between and this clone stands down.  Returning
    quietly on an existing path would have the caller clone over
    whatever is now there.

    Conflict resolution reserves through :func:`claim_target_path`
    instead, having just cleared what was in the way.

    Args:
        path: Destination the worker is about to clone into.
        project: Name of the project being cloned there.

    Raises:
        TargetOwnedError: If the destination is not this clone's.
    """
    generation = current_generation()
    if generation is None:
        return
    try:
        occupied = path.exists()
    except OSError as exc:
        # A destination that cannot be inspected cannot be shown to be
        # ours, so it is left alone.
        logger.debug(f"Could not inspect {path} before reserving it: {exc}")
        return
    if not occupied:
        claim_target_path(path, project)
        return
    with _claimed_lock:
        held = _owned_paths.get(path)
    if held == (generation, project):
        # This clone's own work, from an earlier attempt.
        return
    raise TargetOwnedError(
        f"{path} appeared after the destination was checked, so it is not "
        f"this clone's to write to"
    )


def release_claims(generation: int | None) -> set[Path]:
    """Take and forget the paths owned by *generation*.

    Destinations held back for a worker that outlasted the settle wait
    are stepped over: that worker may still be writing to them, and it
    releases them itself once it stops.

    Args:
        generation: Batch whose ownership is being given up.

    Returns:
        The paths that batch owned, now unregistered.
    """
    if generation is None:
        return set()
    with _claimed_lock:
        owned = {
            path
            for path, owner in _owned_paths.items()
            if owner[0] == generation and path not in _lingering
        }
        for path in owned:
            del _owned_paths[path]
        return owned


def _release_on_completion(
    path: Path, owner: _Owner, _future: Future[CloneResult]
) -> None:
    """Release *path* once the worker still writing to it has stopped.

    The reservation is given up only if it is still the one that was
    held back.  This callback runs whenever the worker finally gets
    round to finishing, by which point the destination may belong to
    somebody else, and taking it from them would be worse than the leak.

    Args:
        path: Destination the worker holds.
        owner: Reservation that was handed over to that worker.
        _future: The worker's future, supplied by ``add_done_callback``.
    """
    with _claimed_lock:
        _lingering.discard(path)
        if _owned_paths.get(path) == owner:
            del _owned_paths[path]


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
    with _claimed_lock:
        if _owned_paths.get(target_path) != (generation, project.name):
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


def discard_partial_clone(project: Project, target_path: Path | None) -> None:
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
    """
    if target_path is None or not target_path.is_dir():
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
) -> None:
    """Keep a still-running worker's destination reserved for it.

    Args:
        project: Project whose worker outlasted the settle wait
        target_path: Destination this clone reserved, or ``None`` if it
            holds no reservation and so has nothing to keep
        generation: Batch the reservation belongs to
        future: The worker's future, which releases the reservation once
            it completes
    """
    logger.warning(f"Leaving {project.name} in place; its worker is still running")
    if target_path is None or generation is None:
        return
    with _claimed_lock:
        _lingering.add(target_path)
    owner = (generation, project.name)
    future.add_done_callback(partial(_release_on_completion, target_path, owner))


def record_timeout_results(
    config: Config,
    future_to_project: dict[Future[CloneResult], Project],
    results: list[CloneResult],
    overall_timeout: int,
    generation: int | None = None,
) -> None:
    """Cancel outstanding clones and synthesise their timeout results.

    Args:
        config: Active configuration, supplying the workspace root
        future_to_project: Every future submitted for this batch
        results: Results recorded so far, appended to in place
        overall_timeout: Timeout that expired, named in the error
        generation: Batch identity, used to discard only the
            destinations this batch owns.  Omitted by default, so a
            caller that does not supply it removes nothing.
    """
    logger.error(f"Clone operations timed out after {overall_timeout}s")

    try:
        # Outstanding work is derived from what has actually been recorded,
        # never from future state. Two separate races make future.done()
        # unreliable here: cancel() succeeds only while a future is queued
        # and then reports done(), and a future can finish after
        # as_completed() raised without ever having been yielded to us.
        # Either way the project has no result, so filtering on done()
        # would silently drop it from the report.
        recorded = {result.project.name for result in results}
        outstanding = [
            (future, project)
            for future, project in future_to_project.items()
            if project.name not in recorded
        ]

        for future, project in outstanding:
            future.cancel()
            logger.warning(f"Cancelled clone for {project.name}")

        # Wait, briefly, for the abandoned workers to stop before touching
        # their directories.  A worker whose git child was killed can still
        # be in its post-clone work -- switching the remote to SSH, or
        # finalizing an atomic clone -- and deleting the path underneath it
        # would either fail that work or let it recreate the directory after
        # this report was written.
        pending = [future for future, _ in outstanding if not future.done()]
        running: set[Future[CloneResult]] = set()
        if pending:
            _, unfinished = wait_for_futures(pending, timeout=_SETTLE_TIMEOUT_SECONDS)
            running = set(unfinished)
            if running:
                logger.warning(
                    f"{len(running)} clone worker(s) still running after "
                    f"{_SETTLE_TIMEOUT_SECONDS}s; reporting without waiting further"
                )

        for future, project in outstanding:
            reserved = _reserved_target(project, config, generation)
            if future in running:
                # The wait is bounded, so a worker can outlast it. Its
                # directory is left alone: removing it would race work
                # the worker is still doing, and it could recreate the
                # destination after this report was written. The
                # reservation is held back from every batch-wide
                # release and given up when the worker stops, so nothing
                # else can take a path still being written to.
                _hold_back(project, reserved, generation, future)
            elif not _completed_successfully(future):
                # A clone that in fact finished is left on disk, even
                # though nothing recorded its result in time to report.
                discard_partial_clone(project, reserved)
            results.append(
                build_failure_result(
                    config,
                    project,
                    f"Operation timed out after {overall_timeout}s",
                )
            )

        # Don't raise exception, return partial results
        logger.warning(f"Returning {len(results)} partial results due to timeout")
    finally:
        # Released only once the cleanup above has finished.  While
        # the batch still holds them no other batch can reserve
        # these destinations, so nothing new can appear under a
        # path this report is about to remove.
        release_claims(generation)
