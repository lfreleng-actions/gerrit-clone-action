# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""Content-filter git children are tracked, so an abandoned batch stops them.

Content filtering runs inside the refresh pool, on a staged copy of a
mirror.  Its ``git filter-repo``, ``git log`` and worktree children used
to be launched untracked, so abandoning the pool left them running for
their whole timeout -- and a scan the abandon cut short could pass for a
repository with no secrets in it.
"""

from __future__ import annotations

import contextlib
import os
import signal
import subprocess
import sys
import threading
import time
from typing import TYPE_CHECKING, Any
from unittest.mock import patch

import pytest

from source_clone import content_filter
from source_clone.concurrent_utils import interruptible_executor
from source_clone.content_filter import apply_content_filters
from source_clone.content_git import run_content_git
from source_clone.content_redaction import _run_replace_text
from source_clone.content_removal import (
    _check_git_filter_repo,
    _list_tree_files,
    _remove_files_filter_repo,
)
from source_clone.content_scan import is_shallow_repository, scan_repo_for_secrets
from source_clone.content_worktree import (
    _add_worktree,
    _cleanup_worktree,
    _commit_removal,
    _git_rm_files,
    _list_branch_heads,
)
from source_clone.subprocess_streaming import popen_tracked
from source_clone.subprocess_tracking import (
    ProcessAbandonedError,
    _thread_state,
    _tracked,
    _tracked_lock,
    enter_generation,
    new_generation,
    refuse_generation,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Generator
    from pathlib import Path

#: Generous ceiling for "returned promptly" on a loaded CI machine.
PROMPT_SECONDS = 15

#: Built at runtime so that no credential-shaped literal sits in the
#: source for secret scanners to flag.
TOKEN = "ghp_" + "A1b2C3d4E5" * 4

#: Stands in for a long ``git log -p``: it starts a helper in its own
#: process group, as git's textconv or pager children would be, marks
#: that it is streaming, then emits hunk lines slowly.  Bounded, so a
#: run that never stops it still ends.
SLOW_LOG = """
import pathlib, subprocess, sys, time
subprocess.Popen(["sleep", "20"])
print("commit 0")
print("@@ -0,0 +1 @@", flush=True)
pathlib.Path(sys.argv[1]).touch()
for _ in range(400):
    print("+nothing to see here", flush=True)
    time.sleep(0.05)
"""

#: Stands in for a ``git log -p`` whose helper keeps git's pipes open --
#: only stderr, or stdout too -- for longer than the test allows.  It
#: records its group, emits one hunk line, then exits or stalls.
HELD_PIPES = """
import os, pathlib, subprocess, sys, time
pathlib.Path(sys.argv[1]).write_text(str(os.getpgid(0)))
held = subprocess.DEVNULL if sys.argv[2] == "stderr" else None
subprocess.Popen(["sleep", "40"], stdout=held)
print("commit 0")
print("@@ -0,0 +1 @@")
print("+nothing to see here", flush=True)
if sys.argv[3] == "stall":
    time.sleep(40)
"""


def _git(*args: str, cwd: Path | None = None) -> str:
    result = subprocess.run(
        [
            "git",
            "-c",
            "commit.gpgsign=false",
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.com",
            *args,
        ],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout


def _mirror(tmp_path: Path) -> Path:
    """A bare mirror holding a file to remove and a secret to redact."""
    source = tmp_path / "source"
    _git("init", "-q", "-b", "main", str(source))
    (source / ".github").mkdir()
    (source / ".github" / "dependabot.yml").write_text("version: 2\n")
    (source / "settings.py").write_text(f'TOKEN = "{TOKEN}"\n')
    _git("add", ".", cwd=source)
    _git("commit", "-q", "-m", "one", cwd=source)
    mirror = tmp_path / "mirror.git"
    _git("clone", "-q", "--mirror", str(source), str(mirror))
    return mirror


def _refs(repo: Path) -> str:
    return _git("for-each-ref", "--format=%(refname) %(objectname)", cwd=repo)


def _tracked_group(generation: int) -> int:
    """Wait for *generation*'s child to be registered; its group."""
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
    raise AssertionError("the scan's git child was never tracked")


def _wait_for(path: Path) -> None:
    deadline = time.monotonic() + PROMPT_SECONDS
    while not path.exists():
        if time.monotonic() > deadline:
            raise AssertionError(f"{path} never appeared")
        time.sleep(0.05)


def _group_gone(group: int) -> bool:
    try:
        os.killpg(group, 0)
    except ProcessLookupError:
        return True
    return False


def _tracked_for(generation: int) -> int:
    with _tracked_lock:
        return sum(1 for t in _tracked.values() if t.generation == generation)


def _scan_promptly(
    repo: Path, held: str, end: str, timeout: int = 60
) -> dict[str, object]:
    """Scan *repo* with :data:`HELD_PIPES` standing in for git.

    Returns the scan's ``result`` or ``error``, having failed the test if
    it did not return promptly.  The helper is killed either way.
    """
    group_file = repo / "group"
    cmd = [sys.executable, "-c", HELD_PIPES, str(group_file), held, end]
    outcome: dict[str, object] = {}

    def scan() -> None:
        try:
            outcome["result"] = scan_repo_for_secrets(repo, timeout=timeout)
        except BaseException as exc:
            outcome["error"] = exc

    thread = threading.Thread(target=scan, daemon=True)
    try:
        with patch("source_clone.content_scan._build_scan_command", return_value=cmd):
            thread.start()
            thread.join(PROMPT_SECONDS)
        assert not thread.is_alive(), "the scan waited on a helper holding its pipes"
        assert _group_gone(int(group_file.read_text())), "a helper outlived the scan"
    finally:
        with contextlib.suppress(FileNotFoundError, ValueError, ProcessLookupError):
            os.killpg(int(group_file.read_text()), signal.SIGKILL)
    return outcome


@pytest.fixture
def batch() -> Generator[int, None, None]:
    """Bind this thread to a live batch."""
    generation = new_generation()
    enter_generation(generation)
    try:
        yield generation
    finally:
        _thread_state.generation = None


@pytest.fixture
def refused(batch: int) -> int:
    """Bind this thread to a batch that has already been abandoned."""
    refuse_generation(batch)
    return batch


#: Every content-filter helper that launches git.  Each is given a
#: directory holding ``file.txt``, so ``git rm`` has something to remove.
HELPERS: dict[str, Callable[[Path], object]] = {
    "check_git_filter_repo": lambda _: _check_git_filter_repo(),
    "remove_files_filter_repo": lambda p: _remove_files_filter_repo(p, ["*.pyc"]),
    "list_tree_files": lambda p: _list_tree_files(p, "main"),
    "run_replace_text": lambda p: _run_replace_text(p, str(p / "map.txt"), 1, 30),
    "is_shallow_repository": is_shallow_repository,
    "scan_repo_for_secrets": scan_repo_for_secrets,
    "list_branch_heads": lambda p: _list_branch_heads(p, 30),
    "add_worktree": lambda p: _add_worktree(p, str(p / "worktree"), "main", 30),
    "git_rm_files": lambda p: _git_rm_files(str(p), ["file.txt"], "main", "r", 30),
    "commit_removal": lambda p: _commit_removal(str(p), "main", "r", 30),
    "cleanup_worktree": lambda p: _cleanup_worktree(p, str(p / "worktree"), 30),
}

#: The helpers that run git to completion, rather than streaming it.
RUN_HELPERS = {k: v for k, v in HELPERS.items() if k != "scan_repo_for_secrets"}


@pytest.mark.usefixtures("refused")
@pytest.mark.parametrize("helper", HELPERS.values(), ids=HELPERS.keys())
def test_a_refused_helper_launches_nothing(
    tmp_path: Path, helper: Callable[[Path], object]
) -> None:
    """Refused, not answered: no default, and no untracked git child."""
    (tmp_path / "file.txt").write_text("content\n")

    with (
        patch("subprocess.Popen", wraps=subprocess.Popen) as popen,
        pytest.raises(ProcessAbandonedError),
    ):
        helper(tmp_path)

    assert not popen.called


@pytest.mark.parametrize("helper", RUN_HELPERS.values(), ids=RUN_HELPERS.keys())
def test_a_child_the_abandon_stopped_is_not_a_git_failure(
    tmp_path: Path, batch: int, helper: Callable[[Path], object]
) -> None:
    """A child terminated by the abandon is not reported, or answered, as git failing."""
    (tmp_path / "file.txt").write_text("content\n")

    def stopped(cmd: list[str], **_: Any) -> subprocess.CompletedProcess[str]:
        refuse_generation(batch)
        return subprocess.CompletedProcess(cmd, -signal.SIGTERM, "", "")

    with (
        patch("source_clone.content_git.run_tracked", side_effect=stopped),
        pytest.raises(ProcessAbandonedError),
    ):
        helper(tmp_path)


def test_a_failure_outside_an_abandoned_batch_is_still_checked(
    tmp_path: Path,
) -> None:
    """``check=True`` keeps ``subprocess.run``'s error, stderr included."""
    _git("init", "-q", str(tmp_path))

    with pytest.raises(subprocess.CalledProcessError) as caught:
        run_content_git(
            ["git", "-C", str(tmp_path), "rev-parse", "--verify", "refs/heads/no"],
            timeout=10,
            check=True,
        )

    assert caught.value.returncode != 0
    assert isinstance(caught.value.stderr, str)
    assert caught.value.stderr


@pytest.mark.usefixtures("refused")
def test_a_refused_cleanup_still_deletes_the_checkout(tmp_path: Path) -> None:
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    (worktree / "file.txt").write_text("content\n")

    with pytest.raises(ProcessAbandonedError):
        _cleanup_worktree(tmp_path, str(worktree), 30)

    assert not worktree.exists()


FILTERS: dict[str, dict[str, Any]] = {
    "remove": {"remove_patterns": [".github/dependabot.yml"]},
    "replace": {"git_filter_projects": {"example/repo": [TOKEN]}},
    "redact": {"redact_secrets": True},
}


@pytest.mark.parametrize("filters", FILTERS.values(), ids=FILTERS.keys())
def test_filters_abandoned_once_started_propagate_and_rewrite_nothing(
    tmp_path: Path, batch: int, filters: dict[str, Any]
) -> None:
    """Not a ``(False, message)`` result, and the mirror left as it was.

    The batch is abandoned once the policy has been recorded, so the
    refusal meets the filters themselves rather than the recording.
    """
    mirror = _mirror(tmp_path)
    refs_before = _refs(mirror)
    run_filters = content_filter._run_filters
    launched_at_abandon = 0

    with patch("subprocess.Popen", wraps=subprocess.Popen) as popen:

        def abandoned_first(*args: Any) -> list[str]:
            nonlocal launched_at_abandon
            refuse_generation(batch)
            launched_at_abandon = popen.call_count
            return run_filters(*args)

        with (
            patch.object(content_filter, "_run_filters", side_effect=abandoned_first),
            pytest.raises(ProcessAbandonedError),
        ):
            apply_content_filters(mirror, "example/repo", **filters)

    assert popen.call_count == launched_at_abandon
    assert _refs(mirror) == refs_before


def test_abandoning_the_batch_stops_a_running_scan(tmp_path: Path) -> None:
    """The scan's child is tracked, and an abandon stops its whole group."""
    started = tmp_path / "started"
    slow_log = [sys.executable, "-c", SLOW_LOG, str(started)]
    group = None
    try:
        with (
            patch(
                "source_clone.content_scan._build_scan_command", return_value=slow_log
            ),
            interruptible_executor(max_workers=1) as executor,
        ):
            future = executor.submit(scan_repo_for_secrets, tmp_path, timeout=60)
            group = _tracked_group(executor.generation)
            # Streaming, and with its helper started, so the check below
            # covers a descendant and not just the child.
            _wait_for(started)
            stopped_at = time.monotonic()
            executor.abandon()

            with pytest.raises(ProcessAbandonedError):
                future.result(timeout=PROMPT_SECONDS)

        assert time.monotonic() - stopped_at < PROMPT_SECONDS
        assert _group_gone(group)
        assert _tracked_for(executor.generation) == 0
    finally:
        if group is not None:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(group, signal.SIGKILL)


def test_a_scan_that_times_out_stops_a_helper_holding_its_pipes(
    tmp_path: Path,
) -> None:
    """The timeout stops git's whole group, or stdout would never close."""
    outcome = _scan_promptly(tmp_path, "both", "stall", timeout=1)

    assert isinstance(outcome.get("error"), RuntimeError)
    assert "timed out" in str(outcome["error"])


def test_a_finished_scan_stops_a_helper_holding_stderr(tmp_path: Path) -> None:
    """Git exiting does not close stderr while its helper holds it open."""
    outcome = _scan_promptly(tmp_path, "stderr", "exit")

    assert outcome == {"result": []}


def test_a_scan_that_raises_stops_a_helper_holding_its_pipes(
    tmp_path: Path,
) -> None:
    """Not only git: its helper would hold stderr open for the join."""

    def broken(proc: subprocess.Popen[str], *_: object) -> list[str]:
        # The helper is started before the first line is written.
        assert proc.stdout is not None
        proc.stdout.readline()
        raise KeyError("stop reading")

    with patch("source_clone.content_scan._consume_scan_stream", broken):
        outcome = _scan_promptly(tmp_path, "both", "stall")

    assert isinstance(outcome.get("error"), KeyError)


def test_a_scan_gives_up_on_a_git_that_outlives_sigkill(tmp_path: Path) -> None:
    """Uninterruptible I/O: no signal lands, and still no wait is unbounded.

    Signals are made to go nowhere, so every stop gives up exactly as it
    would on a process stuck in the kernel, with git holding its pipes.
    """
    group_file = tmp_path / "group"
    cmd = [sys.executable, "-c", HELD_PIPES, str(group_file), "both", "stall"]
    outcome: dict[str, object] = {}

    def scan() -> None:
        try:
            outcome["result"] = scan_repo_for_secrets(tmp_path, timeout=1)
        except BaseException as exc:
            outcome["error"] = exc

    thread = threading.Thread(target=scan, daemon=True)
    try:
        with (
            patch("source_clone.content_scan._build_scan_command", return_value=cmd),
            patch("source_clone.process_groups._send"),
            patch("source_clone.process_groups._TERMINATE_GRACE_SECONDS", 0.2),
            patch("source_clone.process_groups._KILL_CONFIRM_SECONDS", 0.2),
        ):
            thread.start()
            thread.join(PROMPT_SECONDS)
        assert not thread.is_alive(), "the scan waited on a git it could not stop"
        assert isinstance(outcome.get("error"), RuntimeError)
        assert "could not be stopped" in str(outcome["error"])
    finally:
        with contextlib.suppress(FileNotFoundError, ValueError, ProcessLookupError):
            os.killpg(int(group_file.read_text()), signal.SIGKILL)


def test_an_exception_while_streaming_stops_the_child() -> None:
    """Leaving the block early stops and untracks the child it started."""
    process: subprocess.Popen[str] | None = None
    group = None
    raised = False
    try:
        try:
            with popen_tracked(["sleep", "20"]) as process:
                group = process.pid
                raise KeyError("stop reading")
        except KeyError:
            raised = True
        assert raised
        assert process is not None
        assert group is not None
        assert _group_gone(group)
        with _tracked_lock:
            assert process not in _tracked
    finally:
        if group is not None:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(group, signal.SIGKILL)


def test_a_finished_stream_leaves_nothing_in_its_group() -> None:
    """A helper the child left behind does not outlive the block."""
    group = None
    try:
        with popen_tracked(
            [
                sys.executable,
                "-c",
                "import subprocess; subprocess.Popen(['sleep', '20'])",
            ]
        ) as process:
            group = process.pid
            assert process.wait(timeout=PROMPT_SECONDS) == 0
            with _tracked_lock:
                assert process in _tracked
            assert not _group_gone(group)
        assert _group_gone(group)
        with _tracked_lock:
            assert process not in _tracked
    finally:
        if group is not None:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(group, signal.SIGKILL)


def test_filters_in_a_live_batch_still_filter(tmp_path: Path, batch: int) -> None:
    """Tracked, the filters still remove, redact and leave nothing running."""
    mirror = _mirror(tmp_path)

    success, error = apply_content_filters(
        mirror,
        "example/repo",
        remove_patterns=[".github/dependabot.yml"],
        redact_secrets=True,
    )

    assert (success, error) == (True, None)
    history = _git("log", "--all", "-p", cwd=mirror)
    assert TOKEN not in history
    assert "REDACTED_" in history
    assert "dependabot.yml" not in _git(
        "ls-tree", "-r", "--name-only", "main", cwd=mirror
    )
    assert _tracked_for(batch) == 0
