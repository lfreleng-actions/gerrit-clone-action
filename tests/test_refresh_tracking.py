# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""Refresh git children are tracked, so an abandoned batch stops them.

The refresh pool runs under ``interruptible_executor``, whose abandon on
Ctrl+C or SIGTERM can only stop children launched with ``run_tracked``.
A refresh that launched git any other way left a fetch updating refs
after the command had returned (#306).
"""

from __future__ import annotations

import contextlib
import os
import signal
import subprocess
import time
from datetime import UTC, datetime
from typing import TYPE_CHECKING
from unittest.mock import patch

import pytest

from source_clone.concurrent_utils import interruptible_executor
from source_clone.models import RefreshResult, RefreshStatus, RetryPolicy
from source_clone.refresh_git_env import run_git
from source_clone.refresh_worker import RefreshWorker
from source_clone.subprocess_tracking import (
    ProcessAbandonedError,
    _thread_state,
    _tracked,
    _tracked_lock,
    enter_generation,
    new_generation,
    refuse_generation,
    run_tracked,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Generator
    from pathlib import Path

#: Generous ceiling for "returned promptly" on a loaded CI machine.
PROMPT_SECONDS = 15
#: Longer than any test should take, so only a stop can end the fetch.
HANG_SECONDS = 60


def _git(*args: str, cwd: Path | None = None) -> None:
    subprocess.run(
        ["git", "-c", "commit.gpgsign=false", *args],
        cwd=cwd,
        capture_output=True,
        check=True,
    )


def _hanging_remote(repo: Path) -> None:
    """Point *repo*'s origin at a transport that never answers.

    ``ext::`` runs the command as git's transport helper, inside git's
    own process group, so it stands in for the ``ssh`` a Gerrit fetch
    spawns and that the abandon has to reach as well.
    """
    _git("config", "protocol.ext.allow", "always", cwd=repo)
    _git("remote", "add", "origin", f"ext::sleep {HANG_SECONDS}", cwd=repo)


def _hanging_mirror(tmp_path: Path) -> Path:
    repo = tmp_path / "mirror.git"
    _git("init", "-q", "--bare", str(repo))
    _hanging_remote(repo)
    return repo


def _hanging_checkout(tmp_path: Path) -> Path:
    repo = tmp_path / "checkout"
    _git("init", "-q", "-b", "main", str(repo))
    (repo / "file.txt").write_text("one\n")
    _git("add", "file.txt", cwd=repo)
    _git("commit", "-q", "-m", "one", cwd=repo)
    _hanging_remote(repo)
    _git("config", "branch.main.remote", "origin", cwd=repo)
    _git("config", "branch.main.merge", "refs/heads/main", cwd=repo)
    return repo


def _worker() -> RefreshWorker:
    return RefreshWorker(
        retry_policy=RetryPolicy(max_attempts=3, base_delay=0.1),
        timeout=HANG_SECONDS,
        ssh_jitter_seconds=0,
    )


def _result(repo: Path) -> RefreshResult:
    return RefreshResult(
        path=repo,
        project_name=repo.name,
        status=RefreshStatus.REFRESHING,
        started_at=datetime.now(UTC),
    )


def _tracked_group(generation: int) -> int:
    """Wait for *generation*'s git child to be registered; its group."""
    deadline = time.monotonic() + PROMPT_SECONDS
    while time.monotonic() < deadline:
        with _tracked_lock:
            groups = [
                tracked.group
                for tracked in _tracked.values()
                if tracked.generation == generation and tracked.group
            ]
        if groups:
            return int(groups[0])
        time.sleep(0.05)
    raise AssertionError("the refresh git child was never tracked")


@pytest.fixture
def refused() -> Generator[int, None, None]:
    """Bind this thread to a batch that has already been abandoned."""
    generation = new_generation()
    enter_generation(generation)
    refuse_generation(generation)
    try:
        yield generation
    finally:
        _thread_state.generation = None


@pytest.mark.parametrize(
    ("make_repo", "bare"),
    [(_hanging_mirror, True), (_hanging_checkout, False)],
    ids=["fetch", "pull"],
)
def test_abandoning_the_pool_stops_a_running_refresh(
    tmp_path: Path, make_repo: Callable[[Path], Path], bare: bool
) -> None:
    """The abandon reaches the git child and its helper, and is not retried."""
    repo = make_repo(tmp_path)
    worker = _worker()
    result = _result(repo)
    group = None
    try:
        with interruptible_executor(max_workers=1) as executor:
            future = executor.submit(
                worker._execute_adaptive_refresh, repo, result, bare=bare
            )
            group = _tracked_group(executor.generation)
            # Long enough for git to have started its transport helper,
            # so the check below covers a descendant, not just git.
            time.sleep(0.5)
            stopped_at = time.monotonic()
            executor.abandon()

            with pytest.raises(ProcessAbandonedError):
                future.result(timeout=PROMPT_SECONDS)

        assert time.monotonic() - stopped_at < PROMPT_SECONDS
        with pytest.raises(ProcessLookupError):
            os.killpg(group, 0)
        assert result.attempts == 1
        assert result.retry_count == 0
        assert result.error_message is None
    finally:
        if group is not None:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(group, signal.SIGKILL)


@pytest.mark.usefixtures("refused")
@pytest.mark.parametrize(
    ("operation", "bare"),
    [("_execute_git_fetch", True), ("_execute_git_pull", False)],
    ids=["fetch", "pull"],
)
def test_a_refused_refresh_propagates_without_a_retry(
    tmp_path: Path, operation: str, bare: bool
) -> None:
    worker = _worker()
    result = _result(tmp_path)

    with (
        patch.object(worker, operation, wraps=getattr(worker, operation)) as launched,
        patch("source_clone.refresh_execution.time.sleep") as slept,
        pytest.raises(ProcessAbandonedError),
    ):
        worker._execute_adaptive_refresh(tmp_path, result, bare=bare)

    assert launched.call_count == 1
    assert not slept.called
    assert result.retry_count == 0
    assert result.error_message is None


#: Every probe and repair step that launches git, each of which used to
#: answer a failure with a default -- "not bare", "no stash", "no default
#: branch" -- that sent the refresh on as though git had run.
PROBES: dict[str, Callable[[RefreshWorker, Path, RefreshResult], object]] = {
    "is_bare_repository": lambda w, p, _: w._is_bare_repository(p),
    "bare_refresh_obstacle": lambda w, p, _: w._bare_refresh_obstacle(p),
    "check_repository_state": lambda w, p, _: w._check_repository_state(p),
    "is_on_meta_config": lambda w, p, _: w._is_on_meta_config(p),
    "is_meta_only_repo": lambda w, p, _: w._is_meta_only_repo(p),
    "stash_changes": lambda w, p, _: w._stash_changes(p),
    "pop_stash": lambda w, p, _: w._pop_stash(p),
    "get_remote_url": lambda w, p, _: w._get_remote_url(p),
    "has_gerrit_remote": lambda w, p, _: w._has_gerrit_remote(p),
    "get_default_branch": lambda w, p, _: w._get_default_branch(p),
    "get_default_branch_local": lambda w, p, _: w._get_default_branch_local(p),
    "fix_detached_head": lambda w, p, r: w._fix_detached_head(p, r),
    "switch_to_default_branch": lambda w, p, _: w._switch_to_default_branch(p, "main"),
    "fix_upstream_tracking": lambda w, p, r: w._fix_upstream_tracking(p, r),
    "reset_to_upstream": lambda w, p, r: w._reset_to_upstream(p, r),
}


@pytest.mark.usefixtures("refused")
@pytest.mark.parametrize("probe", PROBES.values(), ids=PROBES.keys())
def test_a_refused_probe_is_not_answered(
    tmp_path: Path,
    probe: Callable[[RefreshWorker, Path, RefreshResult], object],
) -> None:
    result = _result(tmp_path)
    result.current_branch = "main"

    with pytest.raises(ProcessAbandonedError):
        probe(_worker(), tmp_path, result)


def test_a_failure_outside_an_abandoned_batch_is_returned(tmp_path: Path) -> None:
    """Only the tracker's verdict turns a nonzero exit into an abandon."""
    _git("init", "-q", str(tmp_path))

    result = run_git(
        ["git", "rev-parse", "--verify", "refs/heads/missing"], tmp_path, timeout=10
    )

    assert result.returncode != 0


@pytest.mark.usefixtures("refused")
def test_a_whole_refresh_in_an_abandoned_batch_reports_abandonment(
    tmp_path: Path,
) -> None:
    """Not an "Unexpected error": the batch gave up, and no git ran."""
    repo = _hanging_mirror(tmp_path)

    with patch("source_clone.refresh_git_env.run_tracked", wraps=run_tracked) as launch:
        result = RefreshWorker(
            filter_gerrit_only=False, ssh_jitter_seconds=0
        ).refresh_repository(repo)

    assert result.status == RefreshStatus.FAILED
    assert result.error_message == "Refresh abandoned before it finished"
    assert result.completed_at is not None
    assert launch.called
    with _tracked_lock:
        assert not _tracked
