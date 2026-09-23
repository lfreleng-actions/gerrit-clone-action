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
import time
from typing import TYPE_CHECKING

from gerrit_clone.logging import get_logger

if TYPE_CHECKING:
    import subprocess
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


def terminate_all(groups: Sequence[tuple[subprocess.Popen[str], int | None]]) -> None:
    """Signal *groups*, wait out one shared grace period, then kill.

    Escalation is decided on each child's whole process group, not on
    the group leader: an ``ssh`` or ``git-remote-https`` helper that
    ignored SIGTERM would otherwise outlive the batch even though its
    parent exited.
    """
    for process, group in groups:
        _stop_group(process, group)

    deadline = time.monotonic() + _TERMINATE_GRACE_SECONDS
    while time.monotonic() < deadline:
        if not any(_group_is_alive(process, group) for process, group in groups):
            return
        time.sleep(_POLL_INTERVAL_SECONDS)

    for process, group in groups:
        if _group_is_alive(process, group):
            logger.debug(f"Killing git process group {group or process.pid}")
            _kill_group(process, group)

    # Signal delivery is asynchronous, so a queued SIGKILL is not proof
    # the group has gone.  Returning here would let the caller start
    # deleting directories the group is still writing to.
    deadline = time.monotonic() + _KILL_CONFIRM_SECONDS
    survivors = groups
    while survivors:
        survivors = [
            (process, group)
            for process, group in survivors
            if _group_is_alive(process, group)
        ]
        if not survivors or time.monotonic() >= deadline:
            break
        time.sleep(_POLL_INTERVAL_SECONDS)

    if survivors:
        logger.warning(
            f"{len(survivors)} git process group(s) outlived SIGKILL confirmation"
        )


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

    # Already gone; nothing left to signal.
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.killpg(group, signal.SIGTERM)


def _kill_group(process: subprocess.Popen[str], group: int | None) -> None:
    """Force the child's process group to exit."""
    if group is None:
        if process.poll() is None:
            with contextlib.suppress(OSError):
                process.kill()
        return

    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.killpg(group, signal.SIGKILL)
