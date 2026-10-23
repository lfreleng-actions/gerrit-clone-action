# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""Running the secret scan's ``git log`` with every wait bounded.

Stopping git's whole process group normally ends the scan: its pipes
close, the readers reach EOF and git is reaped.  A process can outlive
even SIGKILL, though -- stuck in uninterruptible I/O -- and one holding
a pipe would leave the stdout read, the wait for git, or the stderr
drain blocked for good.  So the stdout read runs on a thread of its own
too, and every wait here has a deadline; whatever outlives it is given
up on, with a warning, and the scan fails closed.

The pipes of such a process are left open rather than closed: a thread
blocked reading one holds the buffer's lock, so closing it would block
too, and closing the descriptor underneath would let a reused number
feed the stuck reader some other file.
"""

from __future__ import annotations

import subprocess
import threading
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from source_clone.logging import get_logger
from source_clone.process_groups import (
    escalation_seconds,
    process_group,
    stop_leftovers,
    terminate_all,
)

if TYPE_CHECKING:
    from collections.abc import Callable

logger = get_logger(__name__)


@dataclass
class ScanRun:
    """How a bounded scan ended."""

    discovered: list[str] = field(default_factory=list)
    #: ``None`` if git outlived every attempt to stop it.
    returncode: int | None = None
    stderr: str = ""
    timed_out: bool = False
    #: Raised by the stdout reader, for the caller to raise in turn.
    error: BaseException | None = None


def stop_scan(proc: subprocess.Popen[str]) -> None:
    """Stop *proc* and everything in its process group.

    Not ``proc.kill()``, which reaches git alone: a helper that inherited
    git's pipes would hold them open, and whatever reads them would wait
    for it.
    """
    terminate_all([(proc, process_group(proc))])


def _drain_stderr_async(
    proc: subprocess.Popen[str],
) -> tuple[threading.Thread, list[str]]:
    """Start a daemon thread draining *proc*'s stderr into a buffer.

    Left unread until stdout finished, a full stderr pipe would block
    git's writes and stall the scan.
    """
    chunks: list[str] = []

    def drain() -> None:
        if proc.stderr is not None:
            for line in proc.stderr:
                chunks.append(line)

    thread = threading.Thread(target=drain, daemon=True)
    thread.start()
    return thread, chunks


def _start_watchdog(
    proc: subprocess.Popen[str], timeout: int
) -> tuple[threading.Timer, threading.Event]:
    """Arm a watchdog that stops *proc* once *timeout* seconds elapse.

    It fires whether or not git is producing output: a git that stalls
    silently would otherwise leave the stdout read blocked, re-checking
    its deadline only when a line arrives.
    """
    timed_out = threading.Event()

    def on_timeout() -> None:
        timed_out.set()
        stop_scan(proc)

    watchdog = threading.Timer(timeout, on_timeout)
    watchdog.start()
    return watchdog, timed_out


def _wait(proc: subprocess.Popen[str], seconds: float) -> int | None:
    try:
        return proc.wait(timeout=max(0.0, seconds))
    except subprocess.TimeoutExpired:
        return None


def run_bounded(
    proc: subprocess.Popen[str],
    consume: Callable[[threading.Event], list[str]],
    timeout: int,
) -> ScanRun:
    """Read *proc*'s output through *consume*, never waiting unbounded.

    The watchdog stops git after *timeout* seconds; each wait after that
    allows one escalation's worth of time more.

    Args:
        proc: The running scan.
        consume: Reads ``proc.stdout`` to EOF, given the event the
            watchdog sets, and returns what it found.
        timeout: Seconds the scan may run.
    """
    run = ScanRun()
    stderr_thread, stderr_chunks = _drain_stderr_async(proc)
    watchdog, timed_out = _start_watchdog(proc, timeout)

    def read() -> None:
        try:
            run.discovered = consume(timed_out)
        except BaseException as exc:
            run.error = exc

    reader = threading.Thread(target=read, daemon=True)
    reader.start()
    limit = time.monotonic() + timeout + escalation_seconds()
    try:
        reader.join(max(0.0, limit - time.monotonic()))
        if run.error is not None or reader.is_alive():
            # Nothing reads stdout now, or it is stuck: git could block
            # on a full pipe, or never close it.
            stop_scan(proc)
        run.returncode = _wait(proc, limit - time.monotonic())
        if run.returncode is None:
            stop_scan(proc)
            run.returncode = _wait(proc, escalation_seconds())
    finally:
        watchdog.cancel()
    # A helper still holding stderr would put off its EOF.
    stop_leftovers(proc, process_group(proc))
    stderr_thread.join(escalation_seconds())
    reader.join(escalation_seconds())
    run.timed_out = timed_out.is_set()
    run.stderr = "".join(stderr_chunks)
    stuck = run.returncode is None or reader.is_alive() or stderr_thread.is_alive()
    if stuck:
        logger.warning(
            f"Secret scan git process {proc.pid} outlived SIGKILL; giving up on it"
        )
        run.returncode = None
    return run
