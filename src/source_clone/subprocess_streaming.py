# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""Tracked launch of a child whose output is read as it arrives.

:func:`~source_clone.subprocess_tracking.run_tracked` holds its child's
whole output until it exits, which a secret scan cannot afford: ``git
log -p`` over a full history is read line by line instead.  This starts
the child the same way -- refused once its batch is abandoned, in its
own process group, registered so that an abandon stops that group --
and leaves the reading to the caller.

It goes through the tracker's private launch path rather than a copy of
it.  The abandon check, the launch and the registration have to stay a
single step under the tracker's lock, run on its launcher thread, and a
second implementation of that step could drift from the first.
"""

from __future__ import annotations

import contextlib
from typing import TYPE_CHECKING

from source_clone.process_groups import stop_and_drain, stop_leftovers
from source_clone.subprocess_tracking import (
    _launcher,
    _start_tracked_child,
    _stop_when_launched,
    _tracked,
    _tracked_lock,
    current_generation,
)

if TYPE_CHECKING:
    import subprocess
    from collections.abc import Generator, Mapping, Sequence
    from pathlib import Path


@contextlib.contextmanager
def popen_tracked(
    cmd: Sequence[str],
    *,
    env: Mapping[str, str] | None = None,
    cwd: str | Path | None = None,
    encoding: str = "utf-8",
    errors: str = "replace",
) -> Generator[subprocess.Popen[str], None, None]:
    """Start *cmd* with piped text output, tracked until the block exits.

    The caller reads ``stdout`` and ``stderr`` and waits for the child,
    and any thread it reads them on must be finished before the block
    exits: after an exception the pipes are drained here.  Leaving the
    block stops whatever is still in the child's process
    group and untracks it: after a normal exit that is only stragglers
    the child left behind, while after an exception it is the child too,
    as :func:`~source_clone.subprocess_tracking.run_tracked` does when
    its wait is cut short.

    Args:
        cmd: Command to run.
        env: Environment for the child.
        cwd: Working directory for the child.
        encoding: Text encoding for the piped output.
        errors: Decoding error policy for the piped output.

    Yields:
        The running child.

    Raises:
        ProcessAbandonedError: If this thread's batch has been abandoned,
            or the process itself is terminating, before the launch.
        OSError: If the child could not be started.
    """
    # The generation is read here because the launcher thread carries
    # none of its own.
    launch = _launcher.submit(
        _start_tracked_child,
        list(cmd),
        current_generation(),
        env,
        cwd,
        encoding,
        errors,
    )

    process: subprocess.Popen[str] | None = None
    group: int | None = None
    try:
        process, group = launch.result()
        yield process
    except BaseException:
        if process is None:
            _stop_when_launched(launch)
        else:
            stop_and_drain(process, group)
        raise
    else:
        # Still tracked while this runs, so an abandon arriving
        # meanwhile can reach whatever the child left behind.
        stop_leftovers(process, group)
    finally:
        if process is not None:
            with _tracked_lock:
                _tracked.pop(process, None)
