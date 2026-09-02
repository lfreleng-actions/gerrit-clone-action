# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""Tests for the isolated git config directories used by clone workers.

Each worker thread gets a private ``HOME`` and git config so concurrent
clones do not contend on the user's global ``.gitconfig``.  One
directory per clone *attempt* used to be created and never removed, so a
run over a large hierarchy left thousands behind in the system temp
directory.
"""

from __future__ import annotations

import shutil
import subprocess
import threading
from typing import TYPE_CHECKING
from unittest.mock import MagicMock, patch

import pytest

from gerrit_clone.clone_git_env import (
    _cleanup_at_exit,
    _is_config_lock_error,
    _isolated_config_dirs,
    _quarantined_config_dirs,
    _subprocess_error_text,
    build_clone_environment,
    cleanup_isolated_git_configs,
    isolated_git_config_dir,
    isolated_git_config_scope,
    set_ssh_remote,
)
from gerrit_clone.clone_orchestrator import CloneManager
from gerrit_clone.models import (
    CloneResult,
    CloneStatus,
    Config,
    Project,
    ProjectState,
)
from gerrit_clone.subprocess_tracking import (
    ProcessAbandonedError,
    _thread_state,
    abandon_generation,
    enter_generation,
    new_generation,
)

if TYPE_CHECKING:
    from pathlib import Path


def _project(name: str) -> Project:
    return Project(name=name, state=ProjectState.ACTIVE)


def _recording_clone(config: Config, created: list[Path]):
    """A clone worker that creates a config directory and records it."""

    def clone(project: Project) -> CloneResult:
        build_clone_environment(config)
        created.extend(_isolated_config_dirs)
        return CloneResult(
            project=project,
            status=CloneStatus.SUCCESS,
            path=config.path / project.name,
        )

    return clone


@pytest.fixture(autouse=True)
def _clean_slate():
    """Leave no directories behind, whichever way the test ends."""
    _cleanup_at_exit()
    yield
    _cleanup_at_exit()


@pytest.fixture
def config(tmp_path):
    return Config(host="gerrit.example.org", path=tmp_path / "repos")


class TestDirectoryReuse:
    """The directory contents are static, so one per thread is enough."""

    def test_repeated_calls_on_one_thread_reuse_one_directory(
        self, config: Config
    ) -> None:
        """This is the leak: a clone retried N times created N directories."""
        homes = {build_clone_environment(config)["HOME"] for _ in range(25)}

        assert len(homes) == 1
        assert len(_isolated_config_dirs) == 1

    def test_each_thread_gets_its_own_directory(self, config: Config) -> None:
        """Isolation between concurrent workers must survive the reuse."""
        homes: list[str] = []
        lock = threading.Lock()

        def record() -> None:
            home = build_clone_environment(config)["HOME"]
            with lock:
                homes.append(home)

        threads = [threading.Thread(target=record) for _ in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        assert len(set(homes)) == 4

    def test_the_directory_is_populated(self, config: Config) -> None:
        config_dir = isolated_git_config_dir()

        assert (config_dir / ".gitconfig").is_file()
        assert (config_dir / ".ssh" / "known_hosts").is_file()

    def test_the_environment_points_at_the_directory(self, config: Config) -> None:
        env = build_clone_environment(config)

        assert env["GIT_CONFIG_GLOBAL"].startswith(env["HOME"])
        assert env["HOME"] in {str(path) for path in _isolated_config_dirs}


class TestCleanup:
    """Nothing may survive a completed run."""

    def test_cleanup_removes_every_directory(self, config: Config) -> None:
        build_clone_environment(config)
        created = list(_isolated_config_dirs)
        assert created

        cleanup_isolated_git_configs()

        assert not _isolated_config_dirs
        assert not any(path.exists() for path in created)

    def test_a_thread_recreates_its_directory_after_cleanup(
        self, config: Config
    ) -> None:
        """A cached path must not be handed out once it has been removed."""
        first = isolated_git_config_dir()
        cleanup_isolated_git_configs()

        second = isolated_git_config_dir()

        assert second != first
        assert second.is_dir()
        assert (second / ".gitconfig").is_file()

    def test_cleanup_is_idempotent(self, config: Config) -> None:
        build_clone_environment(config)

        cleanup_isolated_git_configs()
        cleanup_isolated_git_configs()

        assert not _isolated_config_dirs

    def test_a_completed_run_leaves_nothing_behind(self, config: Config) -> None:
        """atexit alone is not enough for a long-lived caller.

        ``clone_projects`` may be invoked repeatedly in one process, by
        the mirror pipeline as well as the clone pipeline, and each
        dependency batch builds a fresh thread pool, so without a
        cleanup at the operation boundary the directories accumulate
        with the batch count rather than the thread count.
        """
        manager = CloneManager(config)
        created: list[Path] = []

        with patch.object(
            manager, "_clone_project_with_progress", _recording_clone(config, created)
        ):
            manager.clone_projects([_project("example/repo")])

        assert created
        assert not any(path.exists() for path in created)

    def test_a_retry_pass_is_cleaned_up_too(self, config: Config) -> None:
        """``retry_failed_clones`` drives its own pools directly.

        Scoping only ``clone_projects`` would let every retry worker
        leave a directory behind, so a run *with failures* would still
        violate the guarantee.
        """
        manager = CloneManager(config)
        created: list[Path] = []

        with patch.object(
            manager, "_clone_project_with_progress", _recording_clone(config, created)
        ):
            manager._execute_dependency_aware_clone([_project("example/repo")])

        assert created
        assert not any(path.exists() for path in created)

    def test_a_concurrent_operation_keeps_its_directories(self, config: Config) -> None:
        """The registry is process-wide, so cleanup must be reference-counted.

        An unconditional cleanup would delete the HOME and
        GIT_CONFIG_GLOBAL of another caller's live clones.
        """
        outer_dir: Path | None = None

        with isolated_git_config_scope():
            outer_dir = isolated_git_config_dir()

            with isolated_git_config_scope():
                pass

            # The inner operation finished, but this one has not.
            assert outer_dir.is_dir()

        assert not outer_dir.exists()

    def test_a_directory_that_cannot_be_removed_is_kept_for_retry(
        self, config: Config
    ) -> None:
        """Forgetting an undeletable path would leave the artifact forever."""
        build_clone_environment(config)
        remaining = set(_isolated_config_dirs)
        assert remaining

        with patch(
            "gerrit_clone.clone_git_env.shutil.rmtree",
            side_effect=PermissionError("read-only"),
        ):
            cleanup_isolated_git_configs()

        assert _isolated_config_dirs == remaining

        # A later attempt can still succeed.
        cleanup_isolated_git_configs()
        assert not _isolated_config_dirs

    def test_an_already_deleted_directory_is_not_retried(self, config: Config) -> None:
        build_clone_environment(config)
        for path in list(_isolated_config_dirs):
            shutil.rmtree(path)

        cleanup_isolated_git_configs()

        assert not _isolated_config_dirs

    def test_a_direct_cleanup_defers_to_an_active_operation(
        self, config: Config
    ) -> None:
        """The atexit hook can fire while a clone is still running.

        Bypassing the reference count would delete a live HOME, which is
        exactly what the scope exists to prevent.
        """
        with isolated_git_config_scope():
            live = isolated_git_config_dir()

            cleanup_isolated_git_configs()

            assert live.is_dir()
            assert live in _isolated_config_dirs

        assert not live.exists()

    def test_an_interrupted_operation_quarantines_its_directories(
        self, config: Config
    ) -> None:
        """Ctrl+C shuts the executor down without waiting.

        Its tasks are still cloning against these directories as HOME,
        so removing them here would cause the very cross-thread race
        this lifecycle exists to prevent -- and leaving them registered
        would only postpone it until the next operation finished.
        """
        live: Path | None = None
        interrupted = False

        try:
            with isolated_git_config_scope():
                live = isolated_git_config_dir()
                raise KeyboardInterrupt
        except KeyboardInterrupt:
            interrupted = True

        assert interrupted, "KeyboardInterrupt did not propagate"
        assert live is not None
        assert live.is_dir()
        # Out of reach of the shared registry, so no later scope can
        # collect it.
        assert live not in _isolated_config_dirs
        assert live in _quarantined_config_dirs

    def test_a_later_operation_cannot_collect_a_quarantined_directory(
        self, config: Config
    ) -> None:
        """Skipping cleanup on the interrupt alone only postpones the race."""
        live: Path | None = None

        try:
            with isolated_git_config_scope():
                live = isolated_git_config_dir()
                raise KeyboardInterrupt
        except KeyboardInterrupt:
            pass

        assert live is not None

        # A later operation completes normally and cleans up.
        with isolated_git_config_scope():
            isolated_git_config_dir()
        cleanup_isolated_git_configs()

        assert live.is_dir(), "a later operation collected a live directory"

        # Only process exit clears the quarantine.
        _cleanup_at_exit()
        assert not live.exists()

    def test_a_surviving_worker_registers_into_the_quarantine(
        self, config: Config
    ) -> None:
        """A worker can reach the registry after the snapshot is taken.

        ``shutdown(wait=False)`` leaves tasks alive, and one part-way
        through its pre-clone checks builds its environment afterwards.
        Registering that normally would put a live HOME back where the
        next completed operation would collect it.
        """
        try:
            with isolated_git_config_scope():
                raise KeyboardInterrupt
        except KeyboardInterrupt:
            pass

        # The straggler builds its environment now, on another thread.
        late: list[Path] = []
        thread = threading.Thread(target=lambda: late.append(isolated_git_config_dir()))
        thread.start()
        thread.join(timeout=10)

        assert late
        assert late[0] not in _isolated_config_dirs
        assert late[0] in _quarantined_config_dirs

        # A later operation completing must not collect it.
        with isolated_git_config_scope():
            isolated_git_config_dir()

        assert late[0].is_dir()

    def test_an_operation_starting_during_cleanup_keeps_its_directory(
        self, config: Config
    ) -> None:
        """The zero-to-snapshot handoff must be atomic.

        If the registry were snapshotted after the lock was released, a
        new operation could register its directory in the gap and have
        its live HOME deleted by the operation that was on its way out.
        """
        leaving = threading.Event()
        registered = threading.Event()
        newcomer: list[Path] = []
        real_rmtree = shutil.rmtree

        def slow_removal(path: Path) -> None:
            leaving.set()
            # Hold the exiting operation inside deletion long enough for
            # the next one to start and register.
            assert registered.wait(timeout=10)
            real_rmtree(path)

        def start_next_operation() -> None:
            assert leaving.wait(timeout=10)
            with isolated_git_config_scope():
                newcomer.append(isolated_git_config_dir())
                registered.set()
                # Still inside the scope, so this directory is live.
                assert newcomer[0].is_dir()

        thread = threading.Thread(target=start_next_operation)
        thread.start()
        with (
            patch("gerrit_clone.clone_git_env.shutil.rmtree", slow_removal),
            isolated_git_config_scope(),
        ):
            isolated_git_config_dir()
        thread.join(timeout=15)

        assert not thread.is_alive()
        assert newcomer


SSH_URL = "ssh://gerrit.example.org:29418/example/repo"
LOCK_MESSAGE = "error: could not lock config file .git/config: File exists"


def _lock_error() -> subprocess.CalledProcessError:
    """The exception ``check=True`` raises for a lost config-file race."""
    return subprocess.CalledProcessError(
        returncode=1,
        cmd=["git", "remote", "set-url", "origin", SSH_URL],
        stderr=LOCK_MESSAGE,
    )


class TestConfigLockClassification:
    """The reason for a git failure lives on stderr, not in str(exc)."""

    def test_str_alone_never_names_the_lock(self) -> None:
        """This is why the retry loop could never fire."""
        assert not _is_config_lock_error(str(_lock_error()))

    def test_captured_stderr_is_classified(self) -> None:
        assert _is_config_lock_error(_subprocess_error_text(_lock_error()))

    def test_a_bare_missing_path_is_not_lock_contention(self) -> None:
        """Reading real stderr activated a substring that matches far too much.

        The retry policy already treats a missing path as permanent, so
        it has to be tied to the config file before it counts here.
        """
        assert not _is_config_lock_error(
            "fatal: could not create work tree dir 'repo': No such file or directory"
        )
        assert not _is_config_lock_error(
            "fatal: destination path 'repo' already exists"
        )

    def test_a_missing_config_file_is_lock_contention(self) -> None:
        """A .git/config that is not there yet is a race, not a dead end."""
        assert _is_config_lock_error(
            "error: could not open '.git/config': No such file or directory"
        )

    def test_error_text_gathers_both_streams(self) -> None:
        error = subprocess.CalledProcessError(
            returncode=1, cmd=["git"], output="on stdout", stderr="on stderr"
        )

        text = _subprocess_error_text(error)

        assert "on stdout" in text
        assert "on stderr" in text

    def test_absent_streams_are_skipped(self) -> None:
        error = subprocess.CalledProcessError(returncode=1, cmd=["git"])

        assert _subprocess_error_text(error) == str(error)


class TestSetSshRemoteRetry:
    """Losing the race for .git/config must not leave origin on HTTPS."""

    @patch("gerrit_clone.clone_git_env.time.sleep")
    @patch("gerrit_clone.clone_git_env.run_tracked")
    def test_lock_contention_is_retried_until_it_succeeds(
        self, mock_run: MagicMock, mock_sleep: MagicMock, tmp_path
    ) -> None:
        mock_run.side_effect = [_lock_error(), MagicMock(returncode=0)]

        set_ssh_remote("example/repo", tmp_path, SSH_URL, {})

        assert mock_run.call_count == 2
        assert mock_sleep.call_count == 1
        assert mock_run.call_args_list[1][0][0] == [
            "git",
            "remote",
            "set-url",
            "origin",
            SSH_URL,
        ]

    @patch("gerrit_clone.clone_git_env.time.sleep")
    @patch("gerrit_clone.clone_git_env.run_tracked")
    def test_retries_are_bounded(
        self, mock_run: MagicMock, mock_sleep: MagicMock, tmp_path
    ) -> None:
        mock_run.side_effect = _lock_error()

        set_ssh_remote("example/repo", tmp_path, SSH_URL, {})

        assert mock_run.call_count == 3
        # No delay after the final attempt.
        assert mock_sleep.call_count == 2

    @patch("gerrit_clone.clone_git_env.time.sleep")
    @patch("gerrit_clone.clone_git_env.run_tracked")
    def test_other_failures_are_not_retried(
        self, mock_run: MagicMock, mock_sleep: MagicMock, tmp_path
    ) -> None:
        """Only lock contention is transient; a bad URL will not improve."""
        mock_run.side_effect = subprocess.CalledProcessError(
            returncode=128,
            cmd=["git"],
            stderr="fatal: No such remote 'origin'",
        )

        set_ssh_remote("example/repo", tmp_path, SSH_URL, {})

        assert mock_run.call_count == 1
        mock_sleep.assert_not_called()

    @patch("gerrit_clone.clone_git_env.time.sleep")
    @patch("gerrit_clone.clone_git_env.run_tracked")
    def test_a_missing_path_failure_is_not_retried(
        self, mock_run: MagicMock, mock_sleep: MagicMock, tmp_path
    ) -> None:
        """A permanent failure must not be mistaken for lock contention."""
        mock_run.side_effect = subprocess.CalledProcessError(
            returncode=128,
            cmd=["git"],
            stderr="fatal: cannot chdir to 'repo': No such file or directory",
        )

        set_ssh_remote("example/repo", tmp_path, SSH_URL, {})

        assert mock_run.call_count == 1
        mock_sleep.assert_not_called()

    @patch("gerrit_clone.clone_git_env.run_tracked")
    def test_an_abandoned_rewrite_is_not_swallowed(
        self, mock_run: MagicMock, tmp_path
    ) -> None:
        """A batch that gave up must not leave a clone reported clean.

        Termination shows up as a negative return code, which looks like
        any other git failure -- and every other failure here is logged
        and shrugged off, the clone itself having worked.  Doing that
        with this one had the worker report success for a repository
        still on HTTPS, which timeout cleanup then kept.
        """
        generation = new_generation()
        enter_generation(generation)
        try:
            mock_run.return_value = MagicMock(returncode=-15)
            abandon_generation(generation)

            with pytest.raises(ProcessAbandonedError):
                set_ssh_remote("example/repo", tmp_path, SSH_URL, {})
        finally:
            _thread_state.generation = None

    @patch("gerrit_clone.clone_git_env.run_tracked")
    def test_a_refused_launch_is_not_swallowed(
        self, mock_run: MagicMock, tmp_path
    ) -> None:
        """Abandonment before launch arrives as an exception instead."""
        mock_run.side_effect = ProcessAbandonedError("batch abandoned")

        with pytest.raises(ProcessAbandonedError):
            set_ssh_remote("example/repo", tmp_path, SSH_URL, {})

    @patch("gerrit_clone.clone_git_env.run_tracked")
    def test_success_runs_once(self, mock_run: MagicMock, tmp_path) -> None:
        mock_run.return_value = MagicMock(returncode=0)

        set_ssh_remote("example/repo", tmp_path, SSH_URL, {})

        assert mock_run.call_count == 1
