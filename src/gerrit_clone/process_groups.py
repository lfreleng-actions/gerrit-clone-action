# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""Stopping a child process together with everything it spawned.

Clone children are started in their own session, so signalling the
group reaches the ``ssh`` and ``git-remote-https`` helpers git spawns
rather than only git itself.  The escalation here is shared: a whole
set is signalled, given one grace period between them, and only then
are the survivors killed.

Which children to stop, and on whose behalf, is decided in
:mod:`gerrit_clone.subprocess_tracking`.
"""

from __future__ import annotations

import contextlib
import os
import signal
import subprocess
import threading
import time
import weakref
from typing import TYPE_CHECKING

from gerrit_clone.logging import get_logger

if TYPE_CHECKING:
    from collections.abc import Sequence

logger = get_logger(__name__)

#: How long terminated children are given, *in total*, to exit before
#: the survivors are killed.  ``git clone`` removes its partially
#: written target directory when it receives SIGTERM, so the grace
#: period is what makes that cleanup possible.  It is shared across the
#: whole set rather than spent per child: waiting on each in turn would
#: make a batch of unresponsive clones take N times as long, defeating
#: the prompt return this exists to provide.
_TERMINATE_GRACE_SECONDS = 5.0

#: How often the grace period checks whether the children have gone.
_POLL_INTERVAL_SECONDS = 0.05

#: How long to confirm that SIGKILL actually took effect.  Signal
#: delivery is asynchronous, so returning as soon as the signal is
#: queued would let the caller start deleting directories the process
#: group is still writing to.  A process that survives this is
#: unkillable (uninterruptible I/O), and waiting longer would not help.
_KILL_CONFIRM_SECONDS = 2.0

#: Children whose group any escalation has seen go.  Two can run for
#: the same child at once -- an abandon and the SIGTERM handler, or a
#: per-call timeout and either -- and an empty group's id is free for
#: reuse, so what one of them saw must stop the others probing it too.
#: Keyed on the ``Popen``, which is never reused, rather than on the id,
#: which is; weak, so a finished child is forgotten along with it.
_gone: weakref.WeakSet[subprocess.Popen[str]] = weakref.WeakSet()
#: Reentrant for the reason the tracker's lock is: the SIGTERM handler
#: can interrupt an escalation on the main thread and start its own.
#: Held across each probe and the one signal it permits, so no
#: escalation can act on a group another has just marked gone.  Never
#: held while waiting, and nothing logs under it: the handler would
#: otherwise wait here behind a thread waiting on a logging lock.
_gone_lock = threading.RLock()


def terminate_all(groups: Sequence[tuple[subprocess.Popen[str], int | None]]) -> None:
    """Signal *groups*, wait out one shared grace period, then kill.

    Escalation is decided on each child's whole process group, not on
    the group leader: an ``ssh`` or ``git-remote-https`` helper that
    ignored SIGTERM would otherwise outlive the batch even though its
    parent exited.  Only the groups still present when the grace period
    ends are killed.

    A group is signalled by id, and nothing can make that atomic with
    knowing the group is still the one recorded; ``Popen.send_signal``
    documents the same residual race for a single pid.  What is avoided
    is acting on an id already known to be stale: every signal, the
    first SIGTERM included, follows straight on a probe that found its
    group present, and a group that this or a concurrent escalation has
    seen go is never probed or signalled again.
    """
    for process, group in groups:
        with _gone_lock:
            if _present(process, group):
                _stop_group(process, group)

    # A group is dropped the moment it is seen to have gone: an empty
    # group's id is free for reuse, so a later probe could find an
    # unrelated group there and kill it as a survivor.
    survivors = _still_alive(groups)
    deadline = time.monotonic() + _TERMINATE_GRACE_SECONDS
    while survivors and time.monotonic() < deadline:
        time.sleep(_POLL_INTERVAL_SECONDS)
        survivors = _still_alive(survivors)

    if not survivors:
        return

    logger.debug(f"Killing {len(survivors)} git process group(s)")
    for process, group in survivors:
        with _gone_lock:
            if _present(process, group):
                _kill_group(process, group)

    # Signal delivery is asynchronous, so a queued SIGKILL is not proof
    # the group has gone.  Returning here would let the caller start
    # deleting directories the group is still writing to.
    deadline = time.monotonic() + _KILL_CONFIRM_SECONDS
    survivors = _still_alive(survivors)
    while survivors and time.monotonic() < deadline:
        time.sleep(_POLL_INTERVAL_SECONDS)
        survivors = _still_alive(survivors)

    if survivors:
        logger.warning(
            f"{len(survivors)} git process group(s) outlived SIGKILL confirmation"
        )


def stop_leftovers(process: subprocess.Popen[str], group: int | None) -> None:
    """Stop anything still in the group of a child that has exited.

    The leader exiting and its output reaching EOF do not prove the
    group empty: a helper that let go of the inherited pipes can outlive
    git there, and once the child is untracked nothing could reach it.
    Nearly always the first probe finds the group gone, and nothing is
    signalled.
    """
    if group is not None:
        terminate_all([(process, group)])


def stop_and_drain(process: subprocess.Popen[str], group: int | None) -> None:
    """Stop one child whose caller is no longer waiting for it.

    Escalated as :func:`terminate_all` does, then drained -- only to
    bound the cleanup.  A process that outlived SIGKILL, stuck in
    uninterruptible I/O, would otherwise hold an unbounded
    ``communicate()`` open forever and stop a timeout being reported at
    all, which is the one thing that path exists to do.
    """
    terminate_all([(process, group)])
    try:
        process.communicate(timeout=_KILL_CONFIRM_SECONDS)
    except subprocess.TimeoutExpired:
        logger.warning(
            f"git process {process.pid} outlived SIGKILL; giving up on its output"
        )
        # Closed explicitly: the pipes are unreachable now, and leaving
        # them open would leak descriptors for the life of the run.
        for pipe in (process.stdout, process.stderr):
            if pipe is not None:
                with contextlib.suppress(OSError):
                    pipe.close()


def _still_alive(
    groups: Sequence[tuple[subprocess.Popen[str], int | None]],
) -> list[tuple[subprocess.Popen[str], int | None]]:
    """The members of *groups* that have not yet gone."""
    alive: list[tuple[subprocess.Popen[str], int | None]] = []
    for process, group in groups:
        with _gone_lock:
            if _present(process, group):
                alive.append((process, group))
    return alive


def _present(process: subprocess.Popen[str], group: int | None) -> bool:
    """Probe *process*'s group, recording it for everyone once it has gone.

    One already recorded is not probed at all.  The caller holds
    ``_gone_lock`` across this and whatever signal it permits.
    """
    if process in _gone:
        return False
    if _group_is_alive(process, group):
        return True
    _gone.add(process)
    return False


def process_group(process: subprocess.Popen[str]) -> int | None:
    """Process group of a freshly launched *process*.

    ``start_new_session=True`` makes the child its own session and group
    leader, so its group id is its pid.  Read at launch and stored,
    because ``os.getpgid`` stops working once the leader is reaped --
    exactly when a surviving helper still needs to be reachable.

    Returns ``None`` on Windows, which has no process group to signal.
    """
    if not hasattr(os, "killpg"):
        return None
    return process.pid


def _group_is_alive(process: subprocess.Popen[str], group: int | None) -> bool:
    """Whether anything in the child's process group is still running.

    The leader is reaped first, non-blockingly.  Without that it lingers
    as a zombie until whichever thread owns it calls ``communicate()``,
    and a zombie is still a group member -- so a child that exited
    politely on SIGTERM would keep answering "alive" and burn the whole
    grace period before drawing a false survivor warning.  Any real
    helper still running keeps the group alive regardless.
    """
    # Reap the leader if it has exited, without blocking.
    process.poll()
    if group is None:
        return process.returncode is None
    try:
        os.killpg(group, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        # Something is there; we simply may not signal it.
        return True
    return True


def _stop_group(process: subprocess.Popen[str], group: int | None) -> None:
    """Ask the child's process group to exit."""
    if group is None:
        # No process group to signal: Windows, or a child already
        # reaped.  ``terminate`` is the portable spelling of "ask
        # nicely"; ``signal.SIGTERM`` would do here but its SIGKILL
        # counterpart does not exist on Windows, so both escalation
        # steps use the Popen methods for symmetry.
        #
        # This reaches the leader only.  On POSIX that is a child
        # already gone, so there is nothing else to reach; on Windows a
        # surviving ``ssh`` helper would need a Job Object to catch, and
        # this project targets Linux runners, so the fallback is
        # deliberately best-effort there.
        if process.poll() is None:
            with contextlib.suppress(OSError):
                process.terminate()
        return

    _send(process, group, signal.SIGTERM)


def _kill_group(process: subprocess.Popen[str], group: int | None) -> None:
    """Force the child's process group to exit."""
    if group is None:
        if process.poll() is None:
            with contextlib.suppress(OSError):
                process.kill()
        return

    _send(process, group, signal.SIGKILL)


def _send(process: subprocess.Popen[str], group: int, sig: int) -> None:
    """Signal *group*, recording it as gone if it has already emptied.

    The group can empty between the probe and the signal, and its id is
    free for reuse from then on: no escalation may probe it again.  The
    caller holds ``_gone_lock``.
    """
    try:
        os.killpg(group, sig)
    except ProcessLookupError:
        _gone.add(process)
    except PermissionError:
        # Something is there; we simply may not signal it.
        pass
