# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""Tests for abandoning a batch without waiting for running work.

``Future.cancel()`` cannot reach a task already inside
``subprocess.run``, and ``shutdown(wait=False)`` only stops the executor
*waiting*.  A batch that gives up therefore has to terminate the child
process itself, or it blocks until the slowest clone finishes and the
process tree outlives the run.
"""

from __future__ import annotations

import contextlib
import os
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import TYPE_CHECKING
from unittest.mock import patch

import pytest

from gerrit_clone.concurrent_utils import interruptible_executor
from gerrit_clone.subprocess_tracking import (
    _TERMINATE_GRACE_SECONDS,
    ProcessAbandonedError,
    _thread_state,
    _tracked,
    _tracked_lock,
    abandon_generation,
    enter_generation,
    new_generation,
    run_tracked,
)

if TYPE_CHECKING:
    from collections.abc import Generator
    from pathlib import Path

#: Long enough that a waiting shutdown would visibly block the test.
SLEEP_SECONDS = 30
#: Generous ceiling for "returned promptly" on a loaded CI machine.
PROMPT_SECONDS = 15
#: Mirrors the module's own grace period, for the bounded-wait assertion.
GRACE_SECONDS = _TERMINATE_GRACE_SECONDS


@pytest.fixture
def generation() -> Generator[int, None, None]:
    """A batch identity bound to the calling thread, torn down after."""
    gen = new_generation()
    enter_generation(gen)
    try:
        yield gen
    finally:
        abandon_generation(gen)
        _thread_state.generation = None


def _sleep_command(seconds: int = SLEEP_SECONDS) -> list[str]:
    """A child that will not exit on its own within the test."""
    return [sys.executable, "-c", f"import time; time.sleep({seconds})"]


def _ignores_sigterm_command(seconds: int = SLEEP_SECONDS) -> list[str]:
    """A child that survives SIGTERM, so only SIGKILL ends it."""
    return [
        sys.executable,
        "-c",
        "import signal, time; "
        "signal.signal(signal.SIGTERM, signal.SIG_IGN); "
        f"time.sleep({seconds})",
    ]


def _stubborn_helper_command(pid_file: Path, seconds: int = SLEEP_SECONDS) -> list[str]:
    """A leader that exits on SIGTERM, leaving a helper that ignores it.

    Stands in for ``git`` exiting while the ``ssh`` it spawned does not.
    The helper records its pid so a test can check the *descendant*
    rather than the leader.
    """
    helper = (
        "import os, signal, time; "
        "signal.signal(signal.SIGTERM, signal.SIG_IGN); "
        f"open({str(pid_file)!r}, 'w').write(str(os.getpid())); "
        f"time.sleep({seconds})"
    )
    leader = (
        "import subprocess, sys, time; "
        f"subprocess.Popen([sys.executable, '-c', {helper!r}]); "
        f"time.sleep({seconds})"
    )
    return [sys.executable, "-c", leader]


class TestRunTracked:
    """It must behave like subprocess.run, plus the tracking."""

    def test_output_is_captured_as_text(self) -> None:
        result = run_tracked([sys.executable, "-c", "print('hello')"])

        assert result.returncode == 0
        assert result.stdout.strip() == "hello"
        assert result.stderr == ""

    def test_a_failure_is_returned_not_raised(self) -> None:
        result = run_tracked(
            [sys.executable, "-c", "import sys; sys.stderr.write('bad'); sys.exit(3)"]
        )

        assert result.returncode == 3
        assert result.stderr.strip() == "bad"

    def test_its_own_timeout_still_raises(self) -> None:
        with pytest.raises(subprocess.TimeoutExpired):
            run_tracked(_sleep_command(), timeout=0.2)

    def test_a_timed_out_child_is_not_left_behind(self) -> None:
        with pytest.raises(subprocess.TimeoutExpired):
            run_tracked(_sleep_command(), timeout=0.2)

        assert not _tracked

    def test_a_timed_out_child_that_ignores_sigterm_is_killed(self) -> None:
        """communicate() returning is not proof the group has gone."""
        begin = time.monotonic()

        with pytest.raises(subprocess.TimeoutExpired):
            run_tracked(_ignores_sigterm_command(), timeout=1.0)

        # It survived SIGTERM, so the grace period had to run out first.
        assert time.monotonic() - begin >= GRACE_SECONDS
        assert not _tracked

    def test_the_child_is_untracked_once_it_finishes(self) -> None:
        run_tracked([sys.executable, "-c", "pass"])

        assert not _tracked

    def test_the_child_runs_in_its_own_process_group(self) -> None:
        """Terminating the group is what reaches ssh and git-remote-https."""
        result = run_tracked([sys.executable, "-c", "import os; print(os.getpgrp())"])

        assert result.stdout.strip() != str(os.getpgrp())


class TestAbandon:
    """Abandoning must stop running children and refuse new ones."""

    def test_a_running_child_is_terminated(self, generation: int) -> None:
        started = threading.Event()
        outcome: list[str] = []

        def work() -> None:
            enter_generation(generation)
            started.set()
            try:
                run_tracked(_sleep_command())
                outcome.append("completed")
            except Exception as exc:
                outcome.append(type(exc).__name__)

        thread = threading.Thread(target=work)
        thread.start()
        assert started.wait(timeout=10)
        # Give the child a moment to actually exist before signalling.
        time.sleep(0.5)

        assert abandon_generation(generation) == 1

        thread.join(timeout=PROMPT_SECONDS)
        assert not thread.is_alive(), "worker blocked on a child that was terminated"
        assert outcome == ["completed"], (
            "the call returns; the child exited on the signal"
        )
        assert not _tracked

    def test_a_child_that_has_not_started_is_refused(self, generation: int) -> None:
        """A retrying worker must not start a fresh git after the batch gave up."""
        abandon_generation(generation)

        with pytest.raises(ProcessAbandonedError):
            run_tracked([sys.executable, "-c", "pass"])

    def test_a_later_batch_is_unaffected(self, generation: int) -> None:
        """Abandonment is scoped to a generation, never process-wide.

        A straggler from an abandoned pool must not be able to clear the
        state, and an abandoned pool must not block the next one.
        """
        abandon_generation(generation)

        enter_generation(new_generation())

        assert run_tracked([sys.executable, "-c", "pass"]).returncode == 0

    def test_abandoning_another_batch_leaves_this_one_running(self) -> None:
        other = new_generation()
        mine = new_generation()
        enter_generation(mine)
        started = threading.Event()
        release = threading.Event()

        def work() -> None:
            enter_generation(mine)
            started.set()
            with contextlib.suppress(Exception):
                run_tracked(_sleep_command())
            release.set()

        thread = threading.Thread(target=work)
        thread.start()
        assert started.wait(timeout=10)
        time.sleep(0.5)

        assert abandon_generation(other) == 0
        assert not release.wait(timeout=1), "another batch's abandon stopped this child"

        abandon_generation(mine)
        thread.join(timeout=PROMPT_SECONDS)
        assert not thread.is_alive()
        _thread_state.generation = None

    def test_abandoning_with_nothing_running_is_harmless(self) -> None:
        assert abandon_generation(new_generation()) == 0

    def test_the_launch_cannot_race_the_abandon(self, generation: int) -> None:
        """An abandon landing mid-launch must not miss the child.

        If the check, the launch and the registration were not atomic, an
        abandon between them would snapshot an empty set while the child
        started anyway -- outliving the batch that gave up on it.
        """
        outcomes: list[str] = []
        barrier = threading.Barrier(2, timeout=20)

        def launch() -> None:
            enter_generation(generation)
            barrier.wait()
            try:
                run_tracked(_sleep_command())
                outcomes.append("ran")
            except ProcessAbandonedError:
                outcomes.append("refused")

        def stop() -> None:
            barrier.wait()
            abandon_generation(generation)

        threads = [threading.Thread(target=launch), threading.Thread(target=stop)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=PROMPT_SECONDS)

        assert not any(thread.is_alive() for thread in threads)
        # Either outcome is correct; what must not happen is a child
        # left running once the batch has been abandoned.
        assert outcomes and outcomes[0] in {"ran", "refused"}
        assert not _tracked

    def test_many_children_share_one_grace_period(self, generation: int) -> None:
        """Terminating in turn would spend the grace period once per child.

        With a 5s grace period and 16 workers, serialising the wait would
        take about 80s -- defeating the bounded return this exists for.
        """
        started = threading.Barrier(5, timeout=20)

        def work() -> None:
            enter_generation(generation)
            started.wait()
            with contextlib.suppress(Exception):
                run_tracked(_ignores_sigterm_command())

        threads = [threading.Thread(target=work) for _ in range(4)]
        for thread in threads:
            thread.start()
        started.wait()
        # Let every child install its handler before signalling.
        time.sleep(1.0)

        begin = time.monotonic()
        assert abandon_generation(generation) == 4
        elapsed = time.monotonic() - begin

        for thread in threads:
            thread.join(timeout=PROMPT_SECONDS)
        assert not any(thread.is_alive() for thread in threads)

        # One shared grace period, not four.
        assert elapsed < 2 * GRACE_SECONDS, (
            f"terminating four children took {elapsed:.1f}s"
        )

    def test_a_child_that_ignores_sigterm_is_killed(self, generation: int) -> None:
        """Escalation reaches a leader that will not go quietly."""
        started = threading.Event()

        def work() -> None:
            enter_generation(generation)
            started.set()
            with contextlib.suppress(Exception):
                run_tracked(_ignores_sigterm_command())

        thread = threading.Thread(target=work)
        thread.start()
        assert started.wait(timeout=10)
        time.sleep(1.0)

        abandon_generation(generation)

        thread.join(timeout=PROMPT_SECONDS)
        assert not thread.is_alive(), "a child that ignored SIGTERM was not killed"
        assert not _tracked

    def test_a_surviving_helper_is_killed(
        self, generation: int, tmp_path: Path
    ) -> None:
        """Escalation is decided on the group, not just the leader.

        The leader exits on SIGTERM while the helper it spawned ignores
        it -- git exiting while its ssh does not.  Stopping the check
        once the leader has gone would leave that helper running past
        the batch, which is the whole reason for the process group.
        """
        started = threading.Event()

        def work() -> None:
            enter_generation(generation)
            started.set()
            with contextlib.suppress(Exception):
                run_tracked(_stubborn_helper_command(tmp_path / "helper.pid"))

        thread = threading.Thread(target=work)
        thread.start()
        assert started.wait(timeout=10)

        pid_file = tmp_path / "helper.pid"
        deadline = time.monotonic() + 10
        while not pid_file.exists() and time.monotonic() < deadline:
            time.sleep(0.05)
        assert pid_file.exists(), "the helper never started"
        helper_pid = int(pid_file.read_text())

        abandon_generation(generation)

        with pytest.raises(ProcessLookupError):
            os.kill(helper_pid, 0)

        thread.join(timeout=PROMPT_SECONDS)
        assert not thread.is_alive()
        assert not _tracked

    def test_a_child_without_a_process_group_is_still_killed(
        self, generation: int
    ) -> None:
        """Windows has no process groups in this sense.

        The fallback must escalate through ``Popen.kill()``: there is no
        ``signal.SIGKILL`` to send there, so reaching for one would
        raise instead of completing the bounded cleanup.

        The group is recorded at launch, so the patch has to be in place
        while the child starts -- not merely while it is terminated.
        """
        started = threading.Event()

        def work() -> None:
            enter_generation(generation)
            started.set()
            with contextlib.suppress(Exception):
                run_tracked(_ignores_sigterm_command())

        with patch(
            "gerrit_clone.subprocess_tracking._process_group", return_value=None
        ):
            thread = threading.Thread(target=work)
            thread.start()
            assert started.wait(timeout=10)
            time.sleep(1.0)

            with _tracked_lock:
                assert _tracked, "the child was never registered"
                assert all(tracked.group is None for tracked in _tracked.values()), (
                    "the fallback branch was not exercised"
                )

            abandon_generation(generation)

        thread.join(timeout=PROMPT_SECONDS)
        assert not thread.is_alive(), "the no-process-group fallback did not kill"
        assert not _tracked

    def test_the_kill_is_confirmed_before_returning(self, generation: int) -> None:
        """Signal delivery is asynchronous, so queuing SIGKILL is not enough.

        The caller starts deleting directories as soon as this returns,
        so the group must be gone by then, not merely signalled.
        """
        started = threading.Event()
        groups: list[int] = []

        def work() -> None:
            enter_generation(generation)
            started.set()
            with contextlib.suppress(Exception):
                run_tracked(_ignores_sigterm_command())

        thread = threading.Thread(target=work)
        thread.start()
        assert started.wait(timeout=10)
        time.sleep(1.0)
        with _tracked_lock:
            groups.extend(
                tracked.group for tracked in _tracked.values() if tracked.group
            )
        assert groups

        abandon_generation(generation)

        for group in groups:
            with pytest.raises(ProcessLookupError):
                os.killpg(group, 0)

        thread.join(timeout=PROMPT_SECONDS)
        assert not thread.is_alive()


class TestExecutorExit:
    """Leaving the block must not wait for work that was abandoned."""

    def test_abandoned_exit_returns_promptly(self) -> None:
        started = threading.Event()

        def work() -> None:
            started.set()
            with contextlib.suppress(Exception):
                run_tracked(_sleep_command())

        begin = time.monotonic()
        with interruptible_executor(max_workers=1, thread_name_prefix="test") as ex:
            ex.submit(work)
            assert started.wait(timeout=10)
            time.sleep(0.5)
            ex.abandon()
        elapsed = time.monotonic() - begin

        assert elapsed < PROMPT_SECONDS, (
            f"exit waited {elapsed:.1f}s for an abandoned task"
        )

    def test_a_normal_exit_still_waits(self) -> None:
        """The default must stay a waiting shutdown."""
        finished = threading.Event()

        def work() -> None:
            time.sleep(0.3)
            finished.set()

        with interruptible_executor(max_workers=1, thread_name_prefix="test") as ex:
            ex.submit(work)

        assert finished.is_set()

    def test_an_interrupt_during_the_wait_still_stops_children(self) -> None:
        """Ctrl+C can land after the block body has already finished.

        The waiting shutdown runs in the ``finally``, past the interrupt
        handler, and the children sit in their own sessions where the
        terminal's SIGINT cannot reach them.
        """
        started = threading.Event()

        def work() -> None:
            started.set()
            with contextlib.suppress(Exception):
                run_tracked(_sleep_command())

        real_shutdown = ThreadPoolExecutor.shutdown
        interrupted_once: list[bool] = []

        def shutdown(self, wait=True, *, cancel_futures=False):
            if wait and not interrupted_once:
                # Stand in for Ctrl+C arriving while we wait.
                interrupted_once.append(True)
                raise KeyboardInterrupt
            return real_shutdown(self, wait=wait, cancel_futures=cancel_futures)

        begin = time.monotonic()
        elapsed: float | None = None
        with patch("gerrit_clone.concurrent_utils.suppress_logging_after_interrupt"):
            try:
                with (
                    patch.object(ThreadPoolExecutor, "shutdown", shutdown),
                    interruptible_executor(
                        max_workers=1, thread_name_prefix="test"
                    ) as ex,
                ):
                    ex.submit(work)
                    assert started.wait(timeout=10)
                    time.sleep(0.5)
            except KeyboardInterrupt:
                elapsed = time.monotonic() - begin

        assert elapsed is not None, "KeyboardInterrupt did not propagate"
        assert elapsed < PROMPT_SECONDS
        assert not _tracked, "a child outlived an interrupt during the wait"

    def test_a_normal_exit_runs_every_queued_task(self) -> None:
        """cancel_futures on the normal path would drop queued work."""
        done: list[int] = []
        lock = threading.Lock()

        def work(index: int) -> None:
            time.sleep(0.05)
            with lock:
                done.append(index)

        with interruptible_executor(max_workers=1, thread_name_prefix="test") as ex:
            for index in range(8):
                ex.submit(work, index)

        assert sorted(done) == list(range(8))

    def test_each_pool_gets_its_own_generation(self) -> None:
        """A pool's workers must not inherit an abandoned pool's state."""
        with interruptible_executor(max_workers=1, thread_name_prefix="test") as first:
            first.abandon()

        with interruptible_executor(max_workers=1, thread_name_prefix="test") as second:
            future = second.submit(run_tracked, [sys.executable, "-c", "pass"])
            assert future.result(timeout=30).returncode == 0

    def test_keyboard_interrupt_still_propagates(self) -> None:
        """The interrupt contract is unchanged; it now also stops children."""
        # Patched: the real one silences the root logger for the rest of
        # the process, which would follow this test into every other.
        with (
            patch("gerrit_clone.concurrent_utils.suppress_logging_after_interrupt"),
            pytest.raises(KeyboardInterrupt),
            interruptible_executor(max_workers=1, thread_name_prefix="test"),
        ):
            raise KeyboardInterrupt

    def test_keyboard_interrupt_stops_running_children(self) -> None:
        """Children are in their own session, so SIGINT does not reach them."""
        started = threading.Event()

        def work() -> None:
            started.set()
            with contextlib.suppress(Exception):
                run_tracked(_sleep_command())

        begin = time.monotonic()
        elapsed: float | None = None
        with patch("gerrit_clone.concurrent_utils.suppress_logging_after_interrupt"):
            try:
                with interruptible_executor(
                    max_workers=1, thread_name_prefix="test"
                ) as ex:
                    ex.submit(work)
                    assert started.wait(timeout=10)
                    time.sleep(0.5)
                    raise KeyboardInterrupt
            except KeyboardInterrupt:
                elapsed = time.monotonic() - begin

        assert elapsed is not None, "KeyboardInterrupt did not propagate"
        assert elapsed < PROMPT_SECONDS
        assert not _tracked
