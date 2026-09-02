# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""Tracking of live git child processes so a batch can stop them.

``Future.cancel()`` cannot reach a task that is already inside
``subprocess.run``, and ``shutdown(wait=False)`` only stops the executor
*waiting*: the worker thread and its git child both keep running.  A
batch that gives up -- on its overall timeout, or on Ctrl+C -- therefore
has to terminate the child itself, or the process tree outlives the run.

Children are started in their own process group so that terminating one
also reaches the helpers git spawns (``ssh``, ``git-remote-https``),
which would otherwise be reparented and left behind.

Abandonment is scoped to a **generation**, one per thread pool.  A
worker thread carries the generation of the pool that created it, so an
abandoned batch cannot stop a later batch's clones, and a straggler from
an abandoned batch cannot start a child once a later batch has begun.
Generations are never reused, so there is nothing to reset.
"""

from __future__ import annotations

import contextlib
import itertools
import os
import signal
import subprocess
import threading
import time
from typing import TYPE_CHECKING, NamedTuple

from gerrit_clone.logging import get_logger

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence
    from pathlib import Path

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


class _Tracked(NamedTuple):
    """What is remembered about a running child.

    The process group is recorded at launch, not looked up on demand:
    ``os.getpgid`` fails once the leader has been reaped, and that is
    precisely when a surviving helper still needs to be reachable.
    ``start_new_session=True`` makes the child its own group leader, so
    the group id is its pid.
    """

    generation: int | None
    group: int | None


_tracked: dict[subprocess.Popen[str], _Tracked] = {}
_abandoned_generations: set[int] = set()
_tracked_lock = threading.Lock()
_generations = itertools.count(1)
_thread_state = threading.local()


class ProcessAbandonedError(RuntimeError):
    """Raised when a tracked subprocess is refused after an abandon.

    Once a batch has given up, a worker that has not yet started its git
    child must not start one: it would outlive the batch that asked for
    it, and on a retrying worker it would do so repeatedly.
    """


def new_generation() -> int:
    """Allocate an identity for a new batch of clone work."""
    return next(_generations)


def enter_generation(generation: int) -> None:
    """Bind the calling thread to *generation*.

    Used as a thread-pool initializer, so every worker in a pool
    inherits that pool's generation for its lifetime.
    """
    _thread_state.generation = generation


def current_generation() -> int | None:
    """Generation of the calling thread, or ``None`` outside a batch."""
    generation: int | None = getattr(_thread_state, "generation", None)
    return generation


def batch_abandoned() -> bool:
    """Whether the calling thread's batch has been abandoned.

    A command a batch gave up on comes back looking like any other
    failure -- terminated by a signal, so a negative return code -- and
    post-clone work that treats it as one reports a clone as finished
    when its follow-up work never ran.

    Returns:
        True if this thread belongs to a batch that has been abandoned.
    """
    generation = current_generation()
    if generation is None:
        return False
    with _tracked_lock:
        return generation in _abandoned_generations


def abandon_generation(generation: int) -> int:
    """Stop *generation*'s children and refuse it any new ones.

    All of them are signalled first, then share a single grace period,
    then the survivors are killed.  Terminating them one at a time would
    spend the grace period once per child.

    Args:
        generation: Batch identity from :func:`new_generation`.

    Returns:
        Number of child processes that were signalled.
    """
    with _tracked_lock:
        _abandoned_generations.add(generation)
        groups = [
            (process, tracked.group)
            for process, tracked in _tracked.items()
            if tracked.generation == generation
        ]

    if not groups:
        return 0

    _terminate_all(groups)
    logger.debug(f"Terminated {len(groups)} running git process(es)")
    return len(groups)


def _terminate_all(groups: Sequence[tuple[subprocess.Popen[str], int | None]]) -> None:
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


def _process_group(process: subprocess.Popen[str]) -> int | None:
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


def run_tracked(
    cmd: Sequence[str],
    *,
    timeout: float | None = None,
    env: Mapping[str, str] | None = None,
    cwd: str | Path | None = None,
    encoding: str = "utf-8",
    errors: str = "replace",
) -> subprocess.CompletedProcess[str]:
    """Run *cmd* like ``subprocess.run``, tracking the child while it runs.

    Output is captured and decoded as text, and
    ``subprocess.TimeoutExpired`` is raised on the call's own timeout
    exactly as ``subprocess.run`` does.  The difference is that the
    child is registered for the duration, so :func:`abandon_generation`
    can stop it, and that it runs in its own process group so the
    helpers git spawns are stopped with it.

    Args:
        cmd: Command to run.
        timeout: Seconds to wait before killing the child and raising.
        env: Environment for the child.
        cwd: Working directory for the child.
        encoding: Text encoding for the captured output.
        errors: Decoding error policy for the captured output.

    Returns:
        The completed process, with captured stdout and stderr.

    Raises:
        ProcessAbandonedError: If this thread's batch has been abandoned.
        subprocess.TimeoutExpired: If *timeout* elapses.
    """
    generation = current_generation()

    # The abandon check, the launch and the registration are one atomic
    # step. Otherwise an abandon landing between them would snapshot an
    # empty set, the child would start regardless, and it would register
    # too late to be terminated -- outliving the batch that gave up on
    # it. abandon_generation() only holds this lock to take its
    # snapshot, so it cannot deadlock against the launch.
    with _tracked_lock:
        if generation is not None and generation in _abandoned_generations:
            raise ProcessAbandonedError(
                "Clone abandoned before the git process started"
            )

        process = subprocess.Popen(
            list(cmd),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding=encoding,
            errors=errors,
            env=dict(env) if env is not None else None,
            cwd=cwd,
            # Own process group, so terminating the child reaches ssh and
            # git-remote-https too.  It also detaches from the controlling
            # terminal, which is why the interrupt path signals children
            # explicitly rather than relying on the shell to do it.
            start_new_session=True,
        )
        _tracked[process] = _Tracked(generation, _process_group(process))

    try:
        stdout, stderr = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        # Same escalation as the abandon path, so a helper that ignores
        # SIGTERM is killed rather than left running once communicate()
        # returns.
        with _tracked_lock:
            group = _tracked[process].group
        _terminate_all([(process, group)])
        stdout, stderr = _drain(process)
        raise subprocess.TimeoutExpired(
            process.args, timeout or 0, output=stdout, stderr=stderr
        ) from None
    finally:
        with _tracked_lock:
            _tracked.pop(process, None)

    return subprocess.CompletedProcess(process.args, process.returncode, stdout, stderr)


def _drain(process: subprocess.Popen[str]) -> tuple[str, str]:
    """Collect a terminated child's output without waiting indefinitely.

    A process that outlived SIGKILL -- stuck in uninterruptible I/O --
    would otherwise hold an unbounded ``communicate()`` open forever and
    stop the timeout being reported at all, which is the one thing this
    path exists to do.
    """
    try:
        return process.communicate(timeout=_KILL_CONFIRM_SECONDS)
    except subprocess.TimeoutExpired:
        logger.warning(
            f"git process {process.pid} outlived SIGKILL; "
            f"reporting the timeout without its output"
        )
        # Closed explicitly: the pipes are unreachable now, and leaving
        # them open would leak descriptors for the life of the run.
        for pipe in (process.stdout, process.stderr):
            if pipe is not None:
                with contextlib.suppress(OSError):
                    pipe.close()
        return ("", "")
