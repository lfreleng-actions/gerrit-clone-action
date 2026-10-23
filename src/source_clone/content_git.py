# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""Tracked git execution for the content filters.

Content filtering runs inside the refresh pool, on a staged copy of a
mirror, so its children have to be stoppable like the refresh's own.
Launched untracked, a ``git filter-repo`` would go on rewriting that
copy for the rest of its timeout after the pool was abandoned.
"""

from __future__ import annotations

import subprocess
from typing import TYPE_CHECKING

from source_clone.subprocess_tracking import (
    ProcessAbandonedError,
    batch_abandoned,
    run_tracked,
)

if TYPE_CHECKING:
    from collections.abc import Sequence


def raise_if_abandoned(returncode: int, cmd: Sequence[str]) -> None:
    """Raise instead of reporting a failure the batch itself caused.

    A child the batch terminated exits nonzero exactly like git failing
    on its own.  The filters would report that as a filtering failure,
    and the probes that fail closed would return it as an answer; only
    the tracker can tell the two apart.

    Args:
        returncode: Exit status of the finished child.
        cmd: Command it ran, for the message.

    Raises:
        ProcessAbandonedError: If the child failed and the calling
            thread's batch has been abandoned.
    """
    if returncode != 0 and batch_abandoned():
        raise ProcessAbandonedError(f"{' '.join(cmd[:4])} was abandoned")


def run_content_git(
    cmd: Sequence[str],
    *,
    timeout: float,
    check: bool = False,
) -> subprocess.CompletedProcess[str]:
    """Run a content-filter git command as a tracked child of this batch.

    Args:
        cmd: Command to run.
        timeout: Seconds to wait before killing the child and raising.
        check: Raise on a nonzero exit, as ``subprocess.run`` does.

    Returns:
        The completed process, with text stdout and stderr.

    Raises:
        ProcessAbandonedError: If the calling thread's batch was abandoned
            before or while the command ran.  Checked ahead of *check*,
            so an abandon is never reported as git failing.
        subprocess.CalledProcessError: If *check* is set and the command
            exited nonzero.
        subprocess.TimeoutExpired: If *timeout* elapses.
    """
    result = run_tracked(cmd, timeout=timeout)
    raise_if_abandoned(result.returncode, cmd)
    if check and result.returncode != 0:
        raise subprocess.CalledProcessError(
            result.returncode, result.args, result.stdout, result.stderr
        )
    return result
