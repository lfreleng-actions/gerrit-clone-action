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
import subprocess
import threading
from concurrent.futures import ThreadPoolExecutor
from typing import TYPE_CHECKING, NamedTuple

from gerrit_clone.logging import get_logger
from gerrit_clone.process_groups import (
    process_group,
    stop_and_drain,
    stop_leftovers,
    terminate_all,
)

if TYPE_CHECKING:
    from collections.abc import Generator, Mapping, Sequence
    from concurrent.futures import Future
    from pathlib import Path

logger = get_logger(__name__)


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
#: Reentrant because the SIGTERM handler takes it from whichever thread
#: the signal interrupted, which is the main one -- and that thread may
#: already hold it, registering a child or abandoning a generation.  A
#: plain lock would deadlock there, and deadlocking is the one outcome
#: worse than acting on a momentarily half-updated view: the handler
#: exists precisely to stop the children before the process dies.
_tracked_lock = threading.RLock()
#: Set once the process is on its way out, so that the children stopped
#: by :func:`terminate_tracked_children` are the last ones there are.
#: A snapshot alone would not do: the termination spends a grace period
#: waiting, and a worker between subprocesses could start another in the
#: meantime -- detached, and so surviving the signal that prompted all
#: this.  Unlike abandonment this is not scoped to a generation, there
#: being no batch left to scope it to.
_terminating = threading.Event()
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
    return generation_abandoned(generation)


def generation_abandoned(generation: int) -> bool:
    """Whether *generation* has been abandoned, asked from any thread.

    :func:`batch_abandoned` answers for the calling thread's own batch;
    this answers for a batch the caller only holds a record of, such as
    the owner of a directory being considered for removal.

    Args:
        generation: Batch identity from :func:`new_generation`.

    Returns:
        True if that batch has been abandoned.
    """
    with _tracked_lock:
        return generation in _abandoned_generations


def refuse_generation(generation: int) -> None:
    """Mark *generation* abandoned without yet stopping its children.

    The first half of :func:`abandon_generation`, for a caller with work
    to do between the two: once this returns, a worker of that batch is
    refused any reservation it asks for and any child it tries to start.

    Args:
        generation: Batch identity from :func:`new_generation`.
    """
    with _tracked_lock:
        _abandoned_generations.add(generation)


@contextlib.contextmanager
def unless_abandoned(action: str) -> Generator[None, None, None]:
    """Do a short filesystem write for this thread's batch, or refuse it.

    Checked and done under the lock abandoning a batch takes, so the
    write lands wholly before the abandon or not at all.  Launches and
    the SIGTERM handler wait on that lock too: keep it short and silent.

    Raises:
        ProcessAbandonedError: If the batch has already been abandoned.
    """
    with _tracked_lock:
        if batch_abandoned():
            raise ProcessAbandonedError(f"{action} refused: batch abandoned")
        yield


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
        refuse_generation(generation)
        groups = [
            (process, tracked.group)
            for process, tracked in _tracked.items()
            if tracked.generation == generation
        ]

    if not groups:
        return 0

    terminate_all(groups)
    logger.debug(f"Terminated {len(groups)} running git process(es)")
    return len(groups)


#: Every launch runs here rather than on the caller's thread.  A Python
#: signal handler runs on the main thread, so a launch made there could
#: be interrupted between creating the child and registering it -- and
#: because the registry lock is reentrant, the handler would walk
#: straight through it, snapshot without that child, and end the process
#: having never seen it.  Off the main thread no handler can interrupt a
#: launch, so the lock does its ordinary work: a terminator waits for
#: the launch to finish before it can take its snapshot.  A single
#: worker also keeps launches serialised, which the lock already
#: required.  The thread is created on first use, not at import.
_launcher = ThreadPoolExecutor(max_workers=1, thread_name_prefix="gerrit-clone-launch")


def terminate_tracked_children() -> int:
    """Stop every tracked child, whichever batch it belongs to.

    Children run in their own session so that terminating one reaches
    the helpers git spawns.  That also detaches them from the parent's
    process group, so a signal sent to the CLI -- or to the group it
    started in -- no longer reaches them on its way past.  Anything that
    ends the process without unwinding therefore has to stop them here,
    or they outlive it.

    Returns:
        Number of child processes that were signalled.
    """
    with _tracked_lock:
        # Marked and snapshotted together.  A launch holds this lock
        # from before it creates its child until after it registers it,
        # so there is no moment at which a child exists but cannot be
        # seen here.
        _terminating.set()
        groups = [(process, tracked.group) for process, tracked in _tracked.items()]

    if not groups:
        return 0

    terminate_all(groups)
    logger.debug(f"Terminated {len(groups)} running child process(es)")
    return len(groups)


def _start_tracked_child(
    cmd: list[str],
    generation: int | None,
    env: Mapping[str, str] | None,
    cwd: str | Path | None,
    encoding: str,
    errors: str,
) -> tuple[subprocess.Popen[str], int | None]:
    """Create the child and register it, on the launcher thread.

    The abandon check, the launch and the registration are one atomic
    step. Otherwise an abandon landing between them would snapshot an
    empty set, the child would start regardless, and it would register
    too late to be terminated.  ``abandon_generation`` only holds this
    lock to take its snapshot, so it cannot deadlock against the launch.

    Args:
        cmd: Command to run.
        generation: Batch of the *calling* thread, read there because
            this thread carries none of its own.
        env: Environment for the child.
        cwd: Working directory for the child.
        encoding: Text encoding for the captured output.
        errors: Decoding error policy for the captured output.

    Returns:
        The child and the process group recorded for it.

    Raises:
        ProcessAbandonedError: If the batch was abandoned, or the
            process is terminating, before the launch.
    """
    with _tracked_lock:
        if _terminating.is_set():
            # The process is on its way out and the children have
            # already been signalled.  A child started now would be
            # detached from the group the signal was sent to, and so
            # would outlive the very termination that is underway.
            raise ProcessAbandonedError(
                "Process is terminating; refusing to start a child"
            )
        if generation is not None and generation in _abandoned_generations:
            raise ProcessAbandonedError(
                "Clone abandoned before the git process started"
            )

        process = subprocess.Popen(
            cmd,
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
        group = process_group(process)
        _tracked[process] = _Tracked(generation, group)

    return process, group


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
    exactly as ``subprocess.run`` does; as there, a wait cut short by
    that or by anything else stops the child before the exception
    propagates -- or, if it is still being launched, as soon as it
    exists.  The differences are that the child is registered for the
    duration, so :func:`abandon_generation` can stop it, that it runs in
    its own process group so the helpers git spawns are stopped with it,
    and that nothing it leaves in that group survives the call.

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
        ProcessAbandonedError: If this thread's batch has been abandoned,
            or the process itself is terminating.
        subprocess.TimeoutExpired: If *timeout* elapses.
    """
    generation = current_generation()

    # Launched on the launcher thread rather than this one.  A signal
    # handler runs on the main thread, and a launch interrupted between
    # creating the child and registering it would leave the handler
    # ending the process having never seen that child -- detached, and
    # so surviving it.  Moving the launch off the main thread removes
    # the interruption entirely and lets a termination wait for a launch
    # in flight instead.  The generation is read here, on the calling
    # thread, because the launcher carries none of its own.
    launch = _launcher.submit(
        _start_tracked_child,
        list(cmd),
        generation,
        env,
        cwd,
        encoding,
        errors,
    )

    process: subprocess.Popen[str] | None = None
    group: int | None = None
    try:
        process, group = launch.result()
        stdout, stderr = process.communicate(timeout=timeout)
    except BaseException:
        # The child is stopped whatever cut the wait short, as
        # subprocess.run does.  On the call's own timeout the escalation
        # is the abandon path's, so a helper that ignores SIGTERM is
        # killed rather than left running.  On the KeyboardInterrupt a
        # Ctrl+C raises on the main thread it matters more: this child
        # is in its own session, where the terminal's SIGINT does not
        # reach it.  Worker threads receive no interrupt; their children
        # are stopped by the batch's abandon.  The exception is
        # re-raised rather than rebuilt, so a timeout's ``output`` and
        # ``stderr`` keep subprocess.run's types -- bytes, or None for a
        # stream that was never read -- rather than the decoded text
        # returned on success.
        if process is None:
            _stop_when_launched(launch)
        else:
            stop_and_drain(process, group)
        raise
    else:
        # Still tracked while this runs, so an abandon or SIGTERM
        # arriving meanwhile can reach whatever the child left behind.
        stop_leftovers(process, group)
        return subprocess.CompletedProcess(
            process.args, process.returncode, stdout, stderr
        )
    finally:
        if process is not None:
            with _tracked_lock:
                _tracked.pop(process, None)


def _stop_when_launched(
    launch: Future[tuple[subprocess.Popen[str], int | None]],
) -> None:
    """Stop the child of a launch its caller stopped waiting for.

    The interrupt can land while the launch is queued, under way, or
    just finished.  The launch itself cannot be cut short -- that is why
    it runs on a thread of its own -- so it is either cancelled before
    it starts or left to finish, and the child it creates is stopped and
    untracked as it appears.  Otherwise nothing would remain to do
    either: the caller has already unwound.
    """
    if launch.cancel():
        return
    launch.add_done_callback(_stop_launched)


def _stop_launched(launch: Future[tuple[subprocess.Popen[str], int | None]]) -> None:
    """Stop and untrack the child *launch* created, if it created one.

    Run on the launcher thread when the launch outlasts the interrupt,
    so it holds up later launches for at most one escalation.
    """
    if launch.cancelled() or launch.exception() is not None:
        return
    process, group = launch.result()
    try:
        stop_and_drain(process, group)
    finally:
        with _tracked_lock:
            _tracked.pop(process, None)
