# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""Reporting for a clone batch that exceeded its overall timeout.

The batch abandons its executor first -- see
:meth:`gerrit_clone.concurrent_utils._TrackedThreadPoolExecutor.abandon`
-- and then comes here to account for the work that never finished.

What becomes of each destination is decided in
:mod:`gerrit_clone.clone_cleanup`; this module decides which projects
are in that position and what the run reports for them.
"""

from __future__ import annotations

from concurrent.futures import wait as wait_for_futures
from typing import TYPE_CHECKING, NamedTuple

from gerrit_clone.clone_cleanup import (
    _completed_successfully,
    _hand_over,
    _reserved_target,
    discard_partial_clone,
    hold_back_running_claims,
)
from gerrit_clone.clone_reservations import release_claims
from gerrit_clone.clone_results import build_failure_result
from gerrit_clone.logging import get_logger

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


class _Outcome(NamedTuple):
    """How one outstanding project was judged, decided once.

    Attributes:
        project: The project being accounted for.
        reserved: Destination it holds, or ``None`` if it holds none.
        held_back: Whether its worker was still running.
        succeeded: Whether its clone in fact finished successfully.
        future: Its future, needed to hand the reservation over.
    """

    project: Project
    reserved: Path | None
    held_back: bool
    succeeded: bool
    future: Future[CloneResult]


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

        # Decided in one pass, acted on in the next.  Asking twice
        # could classify the same worker two different ways: one judged
        # failed while the discard set was built and successful in the
        # loop would have its finished clone taken by an ancestor's
        # removal while escaping its own.  Every future not still
        # running has by now finished, so these verdicts are final.
        outcomes = [
            _Outcome(
                project,
                _reserved_target(project, config, generation),
                held_back=future in running and not future.done(),
                succeeded=_completed_successfully(future),
                future=future,
            )
            for future, project in outstanding
        ]

        # Destinations this pass is about to remove.  A reservation
        # beneath one being discarded normally protects it, but not when
        # it is itself being discarded here: a project nested inside a
        # timed-out parent would otherwise keep the parent's partial
        # clone alive, and nothing revisits the parent once the child
        # has gone.  Removing the parent takes the child with it, which
        # was wanted in any case.
        discarding = {
            outcome.reserved
            for outcome in outcomes
            if outcome.reserved is not None
            and not outcome.held_back
            and not outcome.succeeded
        }

        # Discarded before anything is handed over.  A worker handed over
        # can finish at once and give its reservation back, and a parent
        # asked after that would no longer see the clone nested beneath
        # it -- so every nesting question here, as in _hand_over, is
        # answered while no reservation can yet be released.
        for outcome in outcomes:
            if not outcome.held_back and not outcome.succeeded:
                # A clone that in fact finished is left on disk, even
                # though nothing recorded its result in time to report.
                discard_partial_clone(
                    outcome.project, outcome.reserved, generation, discarding
                )
            results.append(
                build_failure_result(
                    config,
                    outcome.project,
                    f"Operation timed out after {overall_timeout}s",
                )
            )

        # The wait is bounded, so a worker can outlast it. Its directory
        # is left alone for now: removing it would race work the worker
        # is still doing. The reservation is held back from every
        # batch-wide release, and the worker's own completion discards a
        # failed clone before giving it up.
        _hand_over(
            [
                (outcome.project, outcome.reserved, outcome.future)
                for outcome in outcomes
                if outcome.held_back
            ],
            generation,
            discarding,
        )

        # Don't raise exception, return partial results
        logger.warning(f"Returning {len(results)} partial results due to timeout")
    finally:
        # Released only once the cleanup above has finished.  While the
        # batch still holds them no other batch can reserve these
        # destinations, so nothing new can appear under a path this
        # report is about to remove.
        #
        # An exceptional exit -- Ctrl+C during the bounded settle wait --
        # can reach here before the loop handed the still-running
        # workers their reservations, so that is done first.  Releasing
        # a path a live worker is finalizing would let a later batch
        # take it mid-clone.
        hold_back_running_claims(config, future_to_project, generation)
        release_claims(generation)
