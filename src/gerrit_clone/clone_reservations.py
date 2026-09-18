# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""Which clone owns which destination.

A batch that is abandoned has to decide what it may remove, and a
worker has to decide whether it may write at all.  Both turn on a
single idea: a destination is *reserved*, not inferred.  Ownership is
recorded when a worker takes a path, so no later check has to guess
whether the directory it found was its own.

The reporting that acts on these reservations lives in
:mod:`gerrit_clone.clone_timeout`.
"""

from __future__ import annotations

import threading
from typing import TYPE_CHECKING

from gerrit_clone.logging import get_logger
from gerrit_clone.subprocess_tracking import batch_abandoned, current_generation

if TYPE_CHECKING:
    from collections.abc import Callable
    from concurrent.futures import Future
    from pathlib import Path

    from gerrit_clone.models import CloneResult

logger = get_logger(__name__)


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
#: worker's own completion gives them up.  See :func:`hold_back_claim`
#: for why the handover is recorded here rather than returned.
_lingering: set[Path] = set()

_claimed_lock = threading.Lock()


class TargetOwnedError(RuntimeError):
    """Raised when another batch has already reserved a destination.

    Cloning anyway would have two batches writing to one path, and
    whichever finished second would decide what the other's timeout
    cleanup found there.
    """


def _nests(a: Path, b: Path) -> bool:
    """Whether *a* and *b* are the same path, or one contains the other."""
    return a == b or a.is_relative_to(b) or b.is_relative_to(a)


def _foreign_overlap(path: Path, generation: int) -> Path | None:
    """A reservation from another batch that nests with *path*.

    Exact equality is not the only way two reservations collide.
    Cleanup removes a whole directory tree, so a batch holding an
    ancestor of *path* would take this clone with it when it timed out,
    and one holding a descendant would lose its clone to the cleanup
    here.  Nested repositories make both reachable.  Why only other
    batches count is set out in :func:`reserved_by_other`.

    The caller must hold :data:`_claimed_lock`.

    Args:
        path: Destination being reserved.
        generation: Batch asking for it.

    Returns:
        The conflicting reservation, or ``None`` if there is none.
    """
    for held, (held_generation, _project) in _owned_paths.items():
        if held_generation != generation and _nests(held, path):
            return held
    return None


def paths_owned_by(owner: _Owner) -> list[Path]:
    """Every destination *owner* currently holds.

    A clone can hold more than the one it writes to: conflict
    resolution also reserves wherever it moves an obstruction aside.

    Args:
        owner: Reservation being asked about.

    Returns:
        The paths it holds, in no particular order.
    """
    with _claimed_lock:
        return [path for path, held_by in _owned_paths.items() if held_by == owner]


def reservation_beneath(
    path: Path,
    owner: _Owner,
    exempt: frozenset[Path] | set[Path] | None = None,
) -> Path | None:
    """A reservation other than *owner*'s sitting inside *path*.

    Args:
        path: Directory tree about to be removed.
        owner: Reservation doing the removing, which does not count
            against itself.
        exempt: Reservations being discarded by the same pass, which
            therefore do not protect what they sit inside.

    Returns:
        The reservation that would be destroyed, or ``None``.
    """
    spared = exempt or frozenset()
    with _claimed_lock:
        for held, owned_by in _owned_paths.items():
            if held == path or owned_by == owner or held in spared:
                continue
            if held.is_relative_to(path):
                return held
    return None


def reserved_by_other(path: Path, project: str) -> Path | None:
    """A reservation someone else holds on *path*, or one nesting with it.

    Asked before a worker classifies what is on disk.  While another
    clone is working there the directory is in flux: it may be a
    half-written clone that looks complete -- ``git clone`` creates
    ``.git`` before transferring anything -- and it may be removed by
    its owner's cleanup.  Reporting it as an existing repository would
    describe something that is about to disappear.

    Within a batch the two directions differ.  An *exact* path held by
    another project is a collision -- name sanitisation is not
    injective, so ``repo`` and ``repo.`` arrive here as two projects for
    one destination -- and the second must be refused rather than shown
    the first one's clone in progress.  Ancestor and descendant
    reservations in the same batch are the supported nested arrangement
    and stay allowed.

    A caller outside a batch is answered ``None``, matching the rest of
    the registry: with no generation there is nothing to tell apart.

    Args:
        path: Destination being considered.
        project: Name of the project considering it, which does not
            count against itself when it re-checks on a retry.

    Returns:
        The conflicting reservation, or ``None`` if there is none.
    """
    generation = current_generation()
    if generation is None:
        return None
    with _claimed_lock:
        held = _owned_paths.get(path)
        if held is not None and held != (generation, project):
            return path
        return _foreign_overlap(path, generation)


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
        TargetOwnedError: If another clone holds the reservation,
            another batch holds one that nests with it, or this batch
            was abandoned as the reservation was taken.
    """
    generation = current_generation()
    if generation is None:
        return
    owner = (generation, project)
    with _claimed_lock:
        held = _owned_paths.get(path)
        if held is None:
            # A path is refused not only when it is already held, but
            # when another batch holds one that nests with it: cleanup
            # works on directory trees, so an overlap destroys just as
            # surely as an exact collision.
            straddling = _foreign_overlap(path, generation)
            if straddling is not None:
                raise TargetOwnedError(
                    f"{path} overlaps {straddling}, which another batch "
                    f"is already cloning"
                )
            _owned_paths[path] = owner
    if held is None:
        _refuse_if_abandoned(path, owner)
        return
    # First reservation wins, and is handed back to every later caller
    # so it can stand down rather than write to a path it does not own.
    if held == owner:
        return
    if held[0] == generation:
        raise TargetOwnedError(
            f"{path} is already being cloned for {held[1]} in this batch"
        )
    raise TargetOwnedError(f"{path} is already being cloned by another batch")


def _refuse_if_abandoned(path: Path, owner: _Owner) -> None:
    """Give *path* straight back if the batch was abandoned as it was taken.

    A worker can reach its reservation after the batch has already been
    abandoned -- a timeout, or the Ctrl+C that leaves through the same
    exit -- and the batch-wide release has already run.  Nothing would
    then be left to give the reservation up, so the destination would
    stay blocked for the rest of the process.

    Checking *after* publishing is what makes this safe in either order.
    If the release runs after the insert, it takes the entry with the
    rest of the generation; if it ran before, the flag is already set
    and the entry is removed here.  One of the two always sees it.

    The clone is refused either way: it would be launched into an
    abandoned batch, which :func:`run_tracked` declines to do.

    Args:
        path: Destination just reserved.
        owner: Reservation that was recorded for it.

    Raises:
        TargetOwnedError: If the batch was abandoned.
    """
    if not batch_abandoned():
        return
    with _claimed_lock:
        if _owned_paths.get(path) == owner:
            del _owned_paths[path]
        _lingering.discard(path)
    raise TargetOwnedError(f"{path} was reserved for a batch that has been abandoned")


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
        TargetOwnedError: If the destination is not this clone's, or
            cannot be inspected to find out.
    """
    generation = current_generation()
    if generation is None:
        return
    try:
        occupied = path.exists()
    except OSError as exc:
        # Without an answer here the destination cannot be reserved,
        # and cloning unreserved would put this worker outside the
        # exclusion the registry exists to provide: nothing would stop
        # a second batch taking the same path, and the partial
        # directory left behind would not be this batch's to clear.
        # Refusing is the conservative answer, and a destination that
        # cannot be inspected is one the clone would likely fail on.
        raise TargetOwnedError(
            f"{path} could not be inspected, so it cannot be reserved: {exc}"
        ) from exc
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


def holds_reservation(path: Path, owner: _Owner) -> bool:
    """Whether the registry records exactly *owner* as holding *path*.

    Args:
        path: Destination being asked about.
        owner: Reservation claiming to hold it.

    Returns:
        Whether that reservation is the current one.
    """
    with _claimed_lock:
        return _owned_paths.get(path) == owner


def hold_back_claim(
    path: Path,
    owner: _Owner,
    future: Future[CloneResult],
    before_release: Callable[[Future[CloneResult]], None] | None = None,
) -> None:
    """Keep *path* reserved for the worker that is still writing to it.

    The reservation stops belonging to the batch here: a batch-wide
    release steps over it, and only the worker's own completion gives it
    up.  Recording that here rather than handing a set back to the
    caller is deliberate -- the batch releases its reservations from a
    ``finally`` that knows nothing of this, and must not be able to undo
    it by omission.

    Holding a path back twice is a no-op.  The timeout path hands over
    the workers it saw still running and then, on its way out, whatever
    an exceptional exit left behind; the two overlap, and a second
    callback would have the worker release a reservation that may by
    then belong to somebody else.

    Args:
        path: Destination the worker holds.
        owner: Reservation being handed to that worker.
        future: The worker's future, which releases it on completion.
        before_release: Run against that future once it completes, while
            the reservation is still held -- the only moment the
            destination can be acted on safely, the worker perhaps still
            writing before it and somebody else perhaps owning the path
            after.  Its failure cannot strand the reservation.
    """
    with _claimed_lock:
        if path in _lingering:
            return
        _lingering.add(path)

    def _on_done(completed: Future[CloneResult]) -> None:
        try:
            if before_release is not None:
                before_release(completed)
        finally:
            _release_on_completion(path, owner, completed)

    # Fires at once if the worker has already finished, which is the
    # point: the caller cannot tell whether it has, and the handover has
    # to be safe either way.
    future.add_done_callback(_on_done)
