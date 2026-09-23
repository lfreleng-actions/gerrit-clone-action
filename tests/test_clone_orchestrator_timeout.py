# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""Tests for the clone orchestrator's overall-timeout handling.

When the batch exceeds its overall timeout the orchestrator returns
partial results rather than raising, so every submitted project must be
represented in the returned list -- including the ones still queued.
"""

from __future__ import annotations

import itertools
import subprocess
import threading
import time
from concurrent.futures import Future
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any
from unittest.mock import patch

import pytest

from gerrit_clone import clone_cleanup, clone_conflicts, clone_timeout
from gerrit_clone.clone_cleanup import _hold_back, hold_back_running_claims
from gerrit_clone.clone_conflicts import resolve_path_conflict
from gerrit_clone.clone_nesting import apply_late_nested_protection
from gerrit_clone.clone_orchestrator import CloneManager
from gerrit_clone.clone_reservations import (
    TargetOwnedError,
    _lingering,
    _owned_paths,
    _release_on_completion,
    claim_new_target,
    claim_target_path,
    hold_back_claim,
    release_claims,
    reserved_by_other,
)
from gerrit_clone.concurrent_utils import _TrackedThreadPoolExecutor
from gerrit_clone.github_gh_cli import clone_with_gh_cli
from gerrit_clone.github_worker import clone_github_repository
from gerrit_clone.models import (
    CloneResult,
    CloneStatus,
    Config,
    Project,
    ProjectState,
    SourceType,
)
from gerrit_clone.pathing import get_project_path, move_conflicting_path
from gerrit_clone.subprocess_tracking import (
    ProcessAbandonedError,
    _thread_state,
    abandon_generation,
    batch_abandoned,
    enter_generation,
    new_generation,
    refuse_generation,
)
from gerrit_clone.worker import CloneError, CloneWorker

if TYPE_CHECKING:
    from collections.abc import Callable

OVERALL_TIMEOUT = 42


def _own(path: Path, project: str = "owner") -> int:
    """Take ownership of *path* as a fresh batch would, and return it.

    Mirrors what a worker does when it is about to create a
    destination: bind the thread to a batch, then reserve.

    A reservation names the clone that holds it, so any test that goes
    on through the timeout path has to name the project it submits
    there; the default suits tests that only exercise the registry.
    """
    generation = new_generation()
    enter_generation(generation)
    claim_new_target(path, project)
    return generation


@pytest.fixture(autouse=True)
def _no_stale_ownership():
    """Ownership is process-global, so it must not leak between tests."""
    _owned_paths.clear()
    _lingering.clear()
    _thread_state.generation = None
    yield
    _owned_paths.clear()
    _lingering.clear()
    _thread_state.generation = None


@pytest.fixture
def config(tmp_path: Path) -> Config:
    """Minimal configuration pointing at a scratch directory."""
    return Config(host="gerrit.example.org", path=tmp_path / "repos")


def _project(name: str) -> Project:
    return Project(name=name, state=ProjectState.ACTIVE)


def _finished_future(result: CloneResult) -> Future[CloneResult]:
    future: Future[CloneResult] = Future()
    future.set_result(result)
    return future


def _running_future() -> Future[CloneResult]:
    """A future the executor has already started, so cancel() fails."""
    future: Future[CloneResult] = Future()
    assert future.set_running_or_notify_cancel() is True
    return future


def _finishing_when_handed_over(
    future: Future[CloneResult], result: CloneResult
) -> Callable[..., None]:
    """A ``hold_back_claim`` under which *future* completes as it is held.

    Its completion then gives its reservation back at once -- the case
    a handover has to be decided ahead of.
    """

    def hold_back(
        path: Path,
        owner: tuple[int, str],
        held: Future[CloneResult],
        before_release: Callable[[Future[CloneResult]], None] | None = None,
    ) -> None:
        hold_back_claim(path, owner, held, before_release)
        if held is future and not held.done():
            held.set_result(result)

    return hold_back


class TestHandleCloneTimeout:
    """Every unfinished project must appear in the partial results."""

    def test_queued_projects_still_get_a_timeout_result(self, config: Config) -> None:
        """A cancelled future reports done(), so it must be snapshotted.

        This is the regression: cancelling a queued future flips it to
        done(), so a second pass filtering on ``not future.done()``
        silently dropped exactly the projects that never started.
        """
        manager = CloneManager(config)
        queued = _project("queued-repo")
        future_to_project: dict[Future[CloneResult], Project] = {
            Future(): queued,
        }
        results: list[CloneResult] = []

        manager._handle_clone_timeout(future_to_project, results, OVERALL_TIMEOUT)

        assert len(results) == 1
        assert results[0].project.name == "queued-repo"
        assert results[0].status == CloneStatus.FAILED
        assert f"timed out after {OVERALL_TIMEOUT}s" in (results[0].error_message or "")

    def test_running_projects_get_a_timeout_result(self, config: Config) -> None:
        """A running future cannot be cancelled but still timed out."""
        manager = CloneManager(config)
        future_to_project: dict[Future[CloneResult], Project] = {
            _running_future(): _project("running-repo"),
        }
        results: list[CloneResult] = []

        manager._handle_clone_timeout(future_to_project, results, OVERALL_TIMEOUT)

        assert [r.project.name for r in results] == ["running-repo"]
        assert results[0].status == CloneStatus.FAILED

    def test_completed_projects_are_not_duplicated(self, config: Config) -> None:
        """Results already recorded must not gain a second entry."""
        manager = CloneManager(config)
        done_project = _project("done-repo")
        recorded = CloneResult(
            project=done_project,
            status=CloneStatus.SUCCESS,
            path=config.path / done_project.name,
        )
        future_to_project: dict[Future[CloneResult], Project] = {
            _finished_future(recorded): done_project,
            Future(): _project("queued-repo"),
        }
        results: list[CloneResult] = [recorded]

        manager._handle_clone_timeout(future_to_project, results, OVERALL_TIMEOUT)

        by_name = [r.project.name for r in results]
        assert by_name == ["done-repo", "queued-repo"]
        assert by_name.count("done-repo") == 1

    def test_future_finishing_after_the_timeout_is_still_reported(
        self, config: Config
    ) -> None:
        """A future can complete after as_completed() gave up on it.

        Such a future was never yielded, so nothing recorded its result,
        yet it reports done(). Outstanding work is therefore derived from
        the recorded results rather than from future state.
        """
        manager = CloneManager(config)
        raced_project = _project("raced-repo")
        raced = CloneResult(
            project=raced_project,
            status=CloneStatus.SUCCESS,
            path=config.path / raced_project.name,
        )
        # Finished, but its result never reached results.
        future_to_project: dict[Future[CloneResult], Project] = {
            _finished_future(raced): raced_project,
        }
        results: list[CloneResult] = []

        manager._handle_clone_timeout(future_to_project, results, OVERALL_TIMEOUT)

        assert [r.project.name for r in results] == ["raced-repo"]

    def test_every_submitted_project_is_accounted_for(self, config: Config) -> None:
        """The partial result set covers the full submission, mixed states."""
        manager = CloneManager(config)
        done_project = _project("done-repo")
        recorded = CloneResult(
            project=done_project,
            status=CloneStatus.SUCCESS,
            path=config.path / done_project.name,
        )
        future_to_project: dict[Future[CloneResult], Project] = {
            _finished_future(recorded): done_project,
            _running_future(): _project("running-repo"),
            Future(): _project("queued-one"),
            Future(): _project("queued-two"),
        }
        results: list[CloneResult] = [recorded]

        manager._handle_clone_timeout(future_to_project, results, OVERALL_TIMEOUT)

        assert len(results) == len(future_to_project)
        assert {r.project.name for r in results} == {
            "done-repo",
            "running-repo",
            "queued-one",
            "queued-two",
        }


class TestPartialCloneCleanup:
    """A killed clone can leave a directory the next run would trip over."""

    def test_a_half_written_directory_is_removed(self, config: Config) -> None:
        manager = CloneManager(config)
        project = _project("partial-repo")
        partial = config.path / project.name
        generation = _own(partial, project.name)
        partial.mkdir(parents=True)
        (partial / "objects").mkdir()

        manager._handle_clone_timeout(
            {Future(): project}, [], OVERALL_TIMEOUT, generation
        )

        assert not partial.exists()

    def test_a_finished_clone_is_left_alone(self, config: Config) -> None:
        """The clone completed after the batch gave up on it.

        Decided from the future, not from the directory: ``git clone``
        creates ``.git`` before transferring anything, so a clone killed
        moments after starting looks like a repository on disk.
        """
        manager = CloneManager(config)
        project = _project("raced-repo")
        cloned = config.path / project.name
        generation = _own(cloned, project.name)
        (cloned / ".git").mkdir(parents=True)
        recorded = CloneResult(project=project, status=CloneStatus.SUCCESS, path=cloned)

        manager._handle_clone_timeout(
            {_finished_future(recorded): project}, [], OVERALL_TIMEOUT, generation
        )

        assert cloned.is_dir()

    def test_an_initialised_but_incomplete_clone_is_removed(
        self, config: Config
    ) -> None:
        """A killed clone can already have a .git directory.

        Preserving it would let the next run mistake it for a complete
        clone, which is worse than the leftover it was meant to avoid.
        """
        manager = CloneManager(config)
        project = _project("half-cloned")
        partial = config.path / project.name
        generation = _own(partial, project.name)
        (partial / ".git").mkdir(parents=True)

        manager._handle_clone_timeout(
            {Future(): project}, [], OVERALL_TIMEOUT, generation
        )

        assert not partial.exists()

    def test_a_directory_the_batch_does_not_own_is_left_alone(
        self, config: Config
    ) -> None:
        """A destination this batch never took belongs to someone else.

        Such a project fails with a path conflict; if that result races
        the overall timeout it still looks outstanding, and deleting its
        directory would destroy data the run never owned.
        """
        manager = CloneManager(config)
        project = _project("users-dir")
        theirs = config.path / project.name
        theirs.mkdir(parents=True)
        (theirs / "important.txt").write_text("do not delete")

        # The worker is refused a destination that appeared after the
        # conflict checks, so it never takes ownership of one.
        generation = new_generation()
        enter_generation(generation)
        with pytest.raises(TargetOwnedError):
            claim_new_target(theirs, project.name)

        manager._handle_clone_timeout(
            {Future(): project}, [], OVERALL_TIMEOUT, generation
        )

        assert (theirs / "important.txt").read_text() == "do not delete"

    def test_a_missing_directory_is_not_an_error(self, config: Config) -> None:
        manager = CloneManager(config)
        results: list[CloneResult] = []

        manager._handle_clone_timeout(
            {Future(): _project("never-started")}, results, OVERALL_TIMEOUT
        )

        assert len(results) == 1

    def test_a_still_running_worker_is_waited_for_first(self, config: Config) -> None:
        """Deleting under a live worker races its post-clone work.

        A worker whose git child was killed can still be switching the
        remote to SSH or finalizing an atomic clone, so its directory
        must not be removed while it is in use.
        """
        manager = CloneManager(config)
        project = _project("slow-finisher")
        target = config.path / project.name
        generation = _own(target, project.name)
        target.mkdir(parents=True)
        observed: list[bool] = []

        future: Future[CloneResult] = Future()
        future.set_running_or_notify_cancel()

        def finish_late() -> None:
            time.sleep(0.3)
            # Still doing post-clone work; the directory must survive.
            observed.append(target.is_dir())
            future.set_result(
                CloneResult(project=project, status=CloneStatus.FAILED, path=target)
            )

        worker = threading.Thread(target=finish_late)
        worker.start()
        manager._handle_clone_timeout(
            {future: project}, [], OVERALL_TIMEOUT, generation
        )
        worker.join(timeout=10)

        assert observed == [True], "the directory went while the worker was live"
        assert not target.exists()

    def test_a_wedged_worker_does_not_hold_the_batch(self, config: Config) -> None:
        """The settle wait is bounded; that is the point of this path."""
        manager = CloneManager(config)
        project = _project("wedged")
        future: Future[CloneResult] = Future()
        future.set_running_or_notify_cancel()
        results: list[CloneResult] = []

        with patch("gerrit_clone.clone_timeout._SETTLE_TIMEOUT_SECONDS", 0.2):
            begin = time.monotonic()
            manager._handle_clone_timeout({future: project}, results, OVERALL_TIMEOUT)
            elapsed = time.monotonic() - begin

        assert elapsed < 5, f"waited {elapsed:.1f}s on a worker that never finished"
        assert len(results) == 1
        future.set_result(
            CloneResult(
                project=project,
                status=CloneStatus.FAILED,
                path=config.path / project.name,
            )
        )


class TestOwnership:
    """Absence before the batch is not proof the batch created it."""

    def test_a_destination_is_claimed_as_it_is_taken(self, config: Config) -> None:
        target = config.path / "brand-new"
        generation = _own(target)

        assert release_claims(generation) == {target}

    def test_an_existing_destination_is_refused(self, config: Config) -> None:
        """It belongs to whoever put it there, not to this clone.

        The conflict checks have already passed, so a destination that
        is there now arrived during this run.  Declining quietly left
        the caller to clone over whatever had appeared.
        """
        existing = config.path / "already-here"
        existing.mkdir(parents=True)
        generation = new_generation()
        enter_generation(generation)

        with pytest.raises(TargetOwnedError, match="appeared"):
            claim_new_target(existing, "already-here")

        assert release_claims(generation) == set()

    def test_a_destination_another_batch_published_is_refused(
        self, config: Config
    ) -> None:
        """The race an existence check cannot win on its own.

        Another batch reserves and creates the destination between this
        one's conflict checks and its reservation.  Treating "it exists"
        as success had this batch clone over that repository, and the
        GitHub finalizer replace it outright.
        """
        target = config.path / "published"
        first = _own(target, "published")
        target.mkdir(parents=True)

        enter_generation(new_generation())
        with pytest.raises(TargetOwnedError):
            claim_new_target(target, "published")

        assert release_claims(first) == {target}

    def test_the_first_batch_to_reserve_a_path_keeps_it(self, config: Config) -> None:
        """Two batches can both find a destination absent.

        The second is refused rather than merely unrecorded: letting it
        clone anyway would put two batches on one path, and whichever
        finished second would decide what the other's cleanup found.
        """
        target = config.path / "contested"

        first = _own(target)
        second = new_generation()
        enter_generation(second)

        with pytest.raises(TargetOwnedError, match="another batch"):
            claim_new_target(target, "rival")

        assert release_claims(first) == {target}
        assert release_claims(second) == set()

    def test_a_refused_worker_does_not_clone(self, tmp_path: Path) -> None:
        """The refusal has to stop the clone, not just skip a bookkeeping entry.

        Otherwise the unrecorded batch can publish first and the
        recorded owner's timeout deletes a repository that succeeded.
        """
        project = Project(
            name="contested", state=ProjectState.ACTIVE, source_type=SourceType.GITHUB
        )
        config = Config(
            host="github.com/org",
            source_type=SourceType.GITHUB,
            path=tmp_path / "repos",
        )
        target = config.path / project.filesystem_path

        _own(target)
        enter_generation(new_generation())

        with patch("gerrit_clone.github_worker.run_tracked") as mock_run:
            result = clone_github_repository(project, config)

        assert result.status == CloneStatus.FAILED
        assert "already being cloned" in (result.error_message or "")
        # Not ``assert_not_called()``, which prints the call's environment.
        assert not mock_run.called, "launched git for a refused clone"

    def test_a_second_batch_will_not_delete_the_first_batch_s_clone(
        self, config: Config
    ) -> None:
        manager = CloneManager(config)
        project = _project("contested")
        target = config.path / project.name

        _own(target)
        second = new_generation()
        enter_generation(second)
        with pytest.raises(TargetOwnedError):
            claim_new_target(target, "rival")
        target.mkdir(parents=True)

        manager._handle_clone_timeout({Future(): project}, [], OVERALL_TIMEOUT, second)

        assert target.is_dir()

    def test_a_claim_outside_a_batch_is_not_recorded(self, config: Config) -> None:
        """Without a batch to attribute it to, the path is not ours."""
        manager = CloneManager(config)
        project = _project("reclaimed")
        target = config.path / project.name

        _thread_state.generation = None
        claim_target_path(target, "reclaimed")
        target.mkdir(parents=True)

        manager._handle_clone_timeout(
            {Future(): project}, [], OVERALL_TIMEOUT, new_generation()
        )

        assert target.is_dir()

    def test_an_inaccessible_destination_is_refused(self, config: Config) -> None:
        """If we cannot see it, we cannot clone into it either.

        Returning quietly would leave the worker cloning without a
        reservation: nothing would stop a second batch taking the same
        path, and the partial directory would not be this batch's to
        clear.
        """
        target = config.path / "unreadable"
        generation = new_generation()
        enter_generation(generation)

        with (
            patch.object(Path, "exists", side_effect=PermissionError("no access")),
            pytest.raises(TargetOwnedError, match="could not be inspected"),
        ):
            claim_new_target(target, "rival")

        assert release_claims(generation) == set()

    def test_a_conflict_resolved_destination_is_owned(self, config: Config) -> None:
        """Conflict resolution clears a destination and claims it.

        ``claim_new_target`` would decline, the path being occupied when
        the worker arrived, so the clearing code claims explicitly.
        """
        manager = CloneManager(config)
        project = _project("reclaimed")
        target = config.path / project.name
        target.mkdir(parents=True)

        generation = new_generation()
        enter_generation(generation)
        claim_target_path(target, "reclaimed")
        (target / "objects").mkdir()

        manager._handle_clone_timeout(
            {Future(): project}, [], OVERALL_TIMEOUT, generation
        )

        assert not target.exists()

    def test_a_github_destination_is_resolved_as_the_worker_writes_it(
        self, tmp_path: Path
    ) -> None:
        """The two source types write to different paths.

        The GitHub worker clones to ``config.path / filesystem_path``
        while the Gerrit path sanitises the name first, so assuming one
        of them would inspect a directory nothing was ever written to.
        """
        # A trailing dot is stripped by sanitisation, so the two
        # conventions disagree for this name.
        project = Project(name="org/repo.", state=ProjectState.ACTIVE)
        github = Config(
            host="github.com/org",
            source_type=SourceType.GITHUB,
            path=tmp_path / "repos",
        )
        manager = CloneManager(github)
        target = github.path / project.filesystem_path

        generation = _own(target, project.name)
        target.mkdir(parents=True)

        manager._handle_clone_timeout(
            {Future(): project}, [], OVERALL_TIMEOUT, generation
        )

        assert not target.exists()

    def test_an_interrupted_batch_gives_up_its_reservations(
        self, config: Config
    ) -> None:
        """``interruptible_executor`` re-raises Ctrl+C.

        Leaving the reservations behind would stop any later batch from
        taking those paths, so their timeout cleanup would silently do
        nothing for the rest of the process.
        """
        manager = CloneManager(config)
        project = _project("interrupted")
        target = config.path / project.name

        def clone(_project: Project) -> CloneResult:
            claim_new_target(target, _project.name)
            raise KeyboardInterrupt

        with (
            patch.object(manager, "_clone_project_with_progress", clone),
            pytest.raises(KeyboardInterrupt),
        ):
            manager._execute_bulk_clone([project])

        assert not _owned_paths, "an interrupted batch kept its reservations"

        # A later batch can therefore still take the path.
        assert release_claims(_own(target)) == {target}

    def test_a_refused_gerrit_worker_does_not_clone(self, config: Config) -> None:
        """The Gerrit path must stand down on a refusal too.

        Nothing there catches ``TargetOwnedError`` explicitly: it ends
        the clone by propagating, and this pins that down so a later
        change to the retry handling cannot quietly let git run.
        """
        project = _project("contested")
        target = get_project_path(project.name, config.path)

        _own(target)
        enter_generation(new_generation())

        with patch("gerrit_clone.worker.run_tracked") as mock_run:
            result = CloneWorker(config).clone_project(project)

        assert result.status == CloneStatus.FAILED
        assert "already being cloned" in (result.error_message or "")
        # Not ``assert_not_called()``, which prints the call's environment.
        assert not mock_run.called, "launched git for a refused clone"

    def test_a_refused_batch_leaves_the_owner_s_directory_alone(
        self, config: Config
    ) -> None:
        """Clearing a destination destroys it, so reserve it first.

        Reserving afterwards means a losing batch discovers the loss
        only once ``rmtree`` has deleted the winner's half-written
        clone.
        """
        target = config.path / "contested"
        target.mkdir(parents=True)
        (target / "keep.txt").write_text("owner content")

        enter_generation(new_generation())
        claim_target_path(target, "reclaimed")

        # A second batch arrives at the same conflicting destination.
        enter_generation(new_generation())
        result = CloneResult(
            project=_project("contested"),
            status=CloneStatus.PENDING,
            path=target,
        )
        stop = resolve_path_conflict(
            "incomplete_clone", result, datetime.now(UTC), True
        )

        assert stop
        assert result.status == CloneStatus.FAILED
        assert (target / "keep.txt").read_text() == "owner content"

    def test_clearing_an_incomplete_clone_spares_a_nested_reservation(
        self, config: Config
    ) -> None:
        """Nesting within a batch is allowed; clearing takes the whole tree.

        A clone nested beneath the destination in the same batch holds
        its own reservation there, finished or not, and must not go
        with the parent's incomplete content.
        """
        parent_path = config.path / "parent"
        child_path = parent_path / "child"
        _own(child_path, "parent/child")
        child_path.mkdir(parents=True)
        (child_path / "cloned.txt").write_text("the child's clone")

        result = CloneResult(
            project=_project("parent"), status=CloneStatus.PENDING, path=parent_path
        )
        stop = resolve_path_conflict(
            "incomplete_clone", result, datetime.now(UTC), True
        )

        assert stop
        assert result.status == CloneStatus.FAILED
        assert (child_path / "cloned.txt").read_text() == "the child's clone"

    def test_a_failed_gh_clone_spares_a_nested_reservation(
        self, config: Config
    ) -> None:
        """The gh CLI writes straight into the destination it cleans up."""
        parent = _project("parent")
        parent_path = config.path / "parent"
        child_path = parent_path / "child"
        _own(parent_path, parent.name)
        claim_new_target(child_path, "parent/child")
        child_path.mkdir(parents=True)
        (child_path / "cloned.txt").write_text("the child's clone")

        failed = subprocess.CompletedProcess(["gh"], 1, "", "clone failed")
        with patch("gerrit_clone.github_gh_cli.run_tracked", return_value=failed):
            result = clone_with_gh_cli(parent, config, parent_path, datetime.now(UTC))

        assert result.status == CloneStatus.FAILED
        assert (child_path / "cloned.txt").read_text() == "the child's clone"

    def test_clearing_leaves_neither_the_content_nor_its_trash(
        self, config: Config
    ) -> None:
        """Moved out of the way before being deleted, and then deleted."""
        target = config.path / "incomplete"
        target.mkdir(parents=True)
        (target / "partial.txt").write_text("half a clone")
        enter_generation(new_generation())

        result = CloneResult(
            project=_project("incomplete"), status=CloneStatus.PENDING, path=target
        )
        stop = resolve_path_conflict(
            "incomplete_clone", result, datetime.now(UTC), True
        )

        assert not stop
        assert not target.exists()
        assert sorted(p.name for p in config.path.iterdir()) == []

    def test_a_clearing_whose_delete_fails_puts_the_tree_back(
        self, config: Config
    ) -> None:
        """Left under its generated name, the tree would be known to nothing.

        Put back at the destination, still reserved, the next run
        classifies it and clears it like any other incomplete clone.
        """
        target = config.path / "incomplete"
        target.mkdir(parents=True)
        (target / "partial.txt").write_text("half a clone")
        enter_generation(new_generation())

        result = CloneResult(
            project=_project("incomplete"), status=CloneStatus.PENDING, path=target
        )
        with patch(
            "gerrit_clone.clone_conflicts.shutil.rmtree",
            side_effect=PermissionError("read-only"),
        ):
            stop = resolve_path_conflict(
                "incomplete_clone", result, datetime.now(UTC), True
            )

        assert stop, "went ahead over a destination it could not clear"
        assert result.status == CloneStatus.FAILED
        assert (target / "partial.txt").read_text() == "half a clone"
        assert sorted(p.name for p in config.path.iterdir()) == ["incomplete"]

    def test_clearing_is_refused_if_the_batch_gives_up_after_the_claim(
        self, config: Config
    ) -> None:
        """The claim only fends off rival batches; it is not the removal.

        A batch abandoned between the two must find the content where it
        was, not have this worker destroy it after the timeout path has
        returned.
        """
        target = config.path / "incomplete"
        target.mkdir(parents=True)
        (target / "partial.txt").write_text("half a clone")
        generation = new_generation()
        enter_generation(generation)

        def abandon_meanwhile(*_args: object) -> None:
            refuse_generation(generation)

        result = CloneResult(
            project=_project("incomplete"), status=CloneStatus.PENDING, path=target
        )
        refused = False
        with patch(
            "gerrit_clone.clone_conflicts.reservation_beneath",
            side_effect=abandon_meanwhile,
        ):
            try:
                resolve_path_conflict(
                    "incomplete_clone", result, datetime.now(UTC), True
                )
            except ProcessAbandonedError:
                refused = True

        assert refused, "cleared the destination after abandonment"
        assert (target / "partial.txt").read_text() == "half a clone"

    def test_moving_an_obstruction_is_refused_if_the_batch_gives_up_meanwhile(
        self, config: Config
    ) -> None:
        """The backup name is reserved first, the rename comes after.

        A batch abandoned in between must find the parent's file where
        it was.
        """
        blocker = config.path / "parent" / "child"
        blocker.parent.mkdir(parents=True)
        blocker.write_text("the parent's file")
        generation = new_generation()
        enter_generation(generation)
        real_move = move_conflicting_path

        def move(path: Path, *, reserve: Callable[[Path], bool], **kwargs: Any) -> bool:
            def reserve_then_abandon(candidate: Path) -> bool:
                granted = reserve(candidate)
                refuse_generation(generation)
                return granted

            return real_move(path, reserve=reserve_then_abandon, **kwargs)

        result = CloneResult(
            project=_project("parent/child"), status=CloneStatus.PENDING, path=blocker
        )
        refused = False
        with patch.object(clone_conflicts, "move_conflicting_path", move):
            try:
                resolve_path_conflict(
                    "nested_file_conflict", result, datetime.now(UTC), True
                )
            except ProcessAbandonedError:
                refused = True

        assert refused, "moved the obstruction after abandonment"
        assert blocker.read_text() == "the parent's file"

    def test_reservations_outlive_the_timeout_cleanup(self, config: Config) -> None:
        """Releasing up front leaves a window over the paths being removed.

        A batch that gave its reservations back can watch a rival take
        one of those paths, create a clone there during the settle wait,
        and then delete it while working through the same list.
        """
        manager = CloneManager(config)
        project = _project("held")
        target = config.path / project.name
        generation = _own(target, project.name)
        rival_refused: list[bool] = []

        def observe(*_args: object, **_kwargs: object) -> None:
            enter_generation(new_generation())
            try:
                claim_target_path(target, "reclaimed")
            except TargetOwnedError:
                rival_refused.append(True)
            else:
                rival_refused.append(False)
            enter_generation(generation)

        with patch("gerrit_clone.clone_timeout.discard_partial_clone", observe):
            manager._handle_clone_timeout(
                {Future(): project}, [], OVERALL_TIMEOUT, generation
            )

        assert rival_refused == [True], "a rival took a path being cleaned up"
        assert release_claims(generation) == set(), "cleanup kept its reservations"

    def test_two_projects_on_one_path_do_not_share_a_reservation(
        self, config: Config
    ) -> None:
        """Sanitisation is not injective, and de-duplication is by name.

        ``repo`` and ``repo.`` are distinct projects that resolve to one
        directory, so a batch can carry two clones for it. Reserving per
        batch would let both proceed, and a timeout in either would then
        discard the other's finished repository.
        """
        enter_generation(new_generation())
        first = get_project_path("repo", config.path)
        second = get_project_path("repo.", config.path)
        assert first == second, "expected these names to collapse to one path"

        claim_new_target(first, "repo")

        with pytest.raises(TargetOwnedError, match="in this batch"):
            claim_new_target(second, "repo.")

    def test_a_retry_may_reserve_the_path_it_already_holds(
        self, config: Config
    ) -> None:
        """Reserving happens per attempt, so it has to be idempotent."""
        target = config.path / "retried"
        generation = _own(target, "retried")

        claim_new_target(target, "retried")

        assert release_claims(generation) == {target}

    def test_a_wedged_worker_keeps_its_directory_and_reservation(
        self, config: Config
    ) -> None:
        """The settle wait is bounded, so a worker can outlast it.

        Deleting then would race whatever the worker is still doing, and
        it could recreate the destination after the report was written.
        """
        manager = CloneManager(config)
        project = _project("wedged")
        target = config.path / project.name
        generation = _own(target, project.name)
        target.mkdir(parents=True)
        (target / "objects").mkdir()
        future = _running_future()

        with patch("gerrit_clone.clone_timeout._SETTLE_TIMEOUT_SECONDS", 0.2):
            manager._handle_clone_timeout(
                {future: project}, [], OVERALL_TIMEOUT, generation
            )

        assert target.is_dir(), "deleted a directory under a live worker"

        # The orchestrator releases the batch's reservations on its way
        # out, knowing nothing of the timeout path.  That release has to
        # step over this one, or it would be gone immediately.
        assert release_claims(generation) == set(), "released a live worker's path"

        # So nothing else can take a path the worker may still write to.
        enter_generation(new_generation())
        with pytest.raises(TargetOwnedError):
            claim_target_path(target, "later")

        # And is given back once that worker finally stops.
        future.set_result(
            CloneResult(project=project, status=CloneStatus.FAILED, path=target)
        )
        claim_target_path(target, "later")

    def test_a_late_release_leaves_a_newer_reservation_alone(
        self, config: Config
    ) -> None:
        """A held-back reservation is given up whenever its worker stops.

        By then the destination may legitimately belong to somebody
        else, and taking it from them would be worse than the leak.
        """
        target = config.path / "handed-on"
        wedged = _own(target, "wedged")
        handover = (wedged, "wedged")

        # The path is given up and taken afresh by a later batch.
        release_claims(wedged)
        later = _own(target, "later")

        # Only now does the wedged worker get round to finishing.
        _release_on_completion(target, handover, Future())

        assert release_claims(later) == {target}

    def test_a_colliding_project_does_not_speak_for_the_owner(
        self, config: Config
    ) -> None:
        """Resolving to a path is not the same as holding it.

        ``repo`` and ``repo.`` resolve to one directory.  The second was
        refused its clone, so its future says nothing about the first --
        yet accounting for it by path alone had it hand the owner's
        reservation to its own completion, and had its directory
        removed on the owner's behalf.
        """
        manager = CloneManager(config)
        owner = _project("repo")
        intruder = _project("repo.")
        target = get_project_path(owner.name, config.path)
        generation = _own(target, owner.name)
        target.mkdir(parents=True)
        (target / "objects").mkdir()

        # The owner is still cloning; only the refused project is done.
        refused = _finished_future(
            CloneResult(project=intruder, status=CloneStatus.FAILED, path=target)
        )

        with patch("gerrit_clone.clone_timeout._SETTLE_TIMEOUT_SECONDS", 0.2):
            manager._handle_clone_timeout(
                {refused: intruder, _running_future(): owner},
                [],
                OVERALL_TIMEOUT,
                generation,
            )

        assert target.is_dir(), "removed a directory on another project's behalf"
        assert _owned_paths.get(target) == (generation, owner.name)

    def test_a_nested_destination_is_refused_across_batches(
        self, config: Config
    ) -> None:
        """A descendant of another batch's reservation is not free.

        Cleanup removes a directory tree, so the batch holding the
        parent would take this clone with it when it timed out.
        """
        parent = config.path / "parent"
        _own(parent, "parent")

        enter_generation(new_generation())
        with pytest.raises(TargetOwnedError, match="overlaps"):
            claim_new_target(parent / "child", "child")

    def test_an_ancestor_destination_is_refused_across_batches(
        self, config: Config
    ) -> None:
        """The overlap is refused from the other direction too.

        Here the newcomer would be the one doing the destroying: its
        cleanup would remove the tree containing the held clone.
        """
        child = config.path / "parent" / "child"
        _own(child, "child")

        enter_generation(new_generation())
        with pytest.raises(TargetOwnedError, match="overlaps"):
            claim_new_target(config.path / "parent", "parent")

    def test_nesting_within_one_batch_is_allowed(self, config: Config) -> None:
        """Nested repositories are a supported arrangement.

        A batch cloning a project and a project beneath it discards each
        reservation separately, so the overlap rule must not reach
        inside a single batch and refuse the inner clone.
        """
        parent = config.path / "parent"
        generation = _own(parent, "parent")

        claim_new_target(parent / "child", "child")

        assert release_claims(generation) == {parent, parent / "child"}

    def test_cleanup_spares_a_nested_reservation(self, config: Config) -> None:
        """A timed-out parent must not take a nested clone with it.

        One batch can hold both, and the inner clone may have succeeded
        while the outer one timed out.  Removing the parent's tree would
        destroy a finished repository the report says nothing about.
        """
        manager = CloneManager(config)
        parent = _project("parent")
        target = get_project_path(parent.name, config.path)
        generation = _own(target, parent.name)
        nested = target / "child"
        nested.mkdir(parents=True)
        claim_target_path(nested, "child")

        failed = _finished_future(
            CloneResult(project=parent, status=CloneStatus.FAILED, path=target)
        )
        manager._handle_clone_timeout({failed: parent}, [], OVERALL_TIMEOUT, generation)

        assert nested.is_dir(), "removed a nested clone another project holds"

    def test_an_interrupted_batch_keeps_a_running_worker_reservation(
        self, config: Config
    ) -> None:
        """Ctrl+C leaves without waiting, so workers outlive the batch.

        Giving the destination back on the way out would let a batch
        started by a caller that caught the interrupt reserve a path the
        old worker is still writing to.
        """
        project = _project("still-going")
        target = get_project_path(project.name, config.path)
        generation = _own(target, project.name)
        running = _running_future()

        hold_back_running_claims(config, {running: project}, generation)

        assert release_claims(generation) == set(), "gave up a live destination"
        assert _owned_paths.get(target) == (generation, project.name)

        # The worker finally stops, and gives the destination back.
        _release_on_completion(target, (generation, project.name), running)
        assert target not in _owned_paths

    def test_an_interrupted_batch_releases_finished_workers(
        self, config: Config
    ) -> None:
        """Only the live destinations are kept back.

        A worker that has stopped is writing nothing, so holding its
        reservation past the batch would strand it.
        """
        project = _project("finished")
        target = get_project_path(project.name, config.path)
        generation = _own(target, project.name)
        done = _finished_future(
            CloneResult(project=project, status=CloneStatus.SUCCESS, path=target)
        )

        hold_back_running_claims(config, {done: project}, generation)

        assert release_claims(generation) == {target}

    def test_a_gh_cli_clone_reserves_its_destination(self, config: Config) -> None:
        """Every GitHub clone takes part, whichever command runs.

        The reservation used to sit inside the git path, which the gh
        CLI branch returns before reaching, so two gh batches could both
        pass the existence check and the loser's cleanup could delete
        the winner's repository.
        """
        project = _project("gh-cloned")
        target = config.path / project.filesystem_path
        gh_config = Config(
            host="github.com/org",
            source_type=SourceType.GITHUB,
            path=config.path,
            use_gh_cli=True,
        )
        generation = new_generation()
        enter_generation(generation)

        with (
            patch("gerrit_clone.github_worker._is_gh_cli_available", return_value=True),
            patch("gerrit_clone.github_worker.clone_with_gh_cli") as gh_clone,
        ):
            gh_clone.return_value = CloneResult(
                project=project, status=CloneStatus.SUCCESS, path=target
            )
            clone_github_repository(project, gh_config)

        assert gh_clone.called, "the gh CLI path should have run"
        assert _owned_paths.get(target) == (generation, project.name)

    def test_a_refused_gh_cli_clone_never_launches(self, config: Config) -> None:
        """A gh clone that cannot reserve stands down like any other."""
        project = _project("contested")
        target = config.path / project.filesystem_path
        gh_config = Config(
            host="github.com/org",
            source_type=SourceType.GITHUB,
            path=config.path,
            use_gh_cli=True,
        )
        _own(target, "rival")

        enter_generation(new_generation())
        with (
            patch("gerrit_clone.github_worker._is_gh_cli_available", return_value=True),
            patch("gerrit_clone.github_worker.clone_with_gh_cli") as gh_clone,
        ):
            result = clone_github_repository(project, gh_config)

        assert result.status == CloneStatus.FAILED
        gh_clone.assert_not_called()

    def test_interrupted_cleanup_keeps_a_running_worker_reservation(
        self, config: Config
    ) -> None:
        """Ctrl+C during the settle wait must not free a live destination.

        The release runs from a ``finally``, so it is reached even when
        the loop that hands reservations to still-running workers never
        got there.
        """
        manager = CloneManager(config)
        project = _project("wedged")
        target = get_project_path(project.name, config.path)
        generation = _own(target, project.name)
        running = _running_future()

        with (
            patch(
                "gerrit_clone.clone_timeout.wait_for_futures",
                side_effect=KeyboardInterrupt,
            ),
            pytest.raises(KeyboardInterrupt),
        ):
            manager._handle_clone_timeout(
                {running: project}, [], OVERALL_TIMEOUT, generation
            )

        assert _owned_paths.get(target) == (generation, project.name), (
            "released a destination a live worker may still be writing to"
        )

    def test_a_destination_is_only_held_back_once(self, config: Config) -> None:
        """Handing the same path over twice must not double-register.

        The timeout path hands over the workers it saw running and then,
        on its way out, whatever an exceptional exit left behind.  A
        second callback would have the worker release a reservation that
        may by then belong to somebody else.
        """
        project = _project("held")
        target = get_project_path(project.name, config.path)
        generation = _own(target, project.name)
        running = _running_future()

        hold_back_running_claims(config, {running: project}, generation)
        hold_back_running_claims(config, {running: project}, generation)

        # The owner gives it up once; a second callback would then take
        # the path from whoever reserved it next.
        _release_on_completion(target, (generation, project.name), running)
        later = _own(target, "later")
        _release_on_completion(target, (generation, project.name), running)

        assert _owned_paths.get(target) == (later, "later")

    def test_a_held_reservation_is_refused_after_abandonment(
        self, config: Config
    ) -> None:
        """Re-reserving is how a clone goes on; it must ask too.

        A retry, and the pre-clone checks following conflict
        resolution, both ask again for a path they already hold.  That
        answered without looking at abandonment, so a worker whose batch
        had given up went on to create directories and write nested
        protection into a parent repository.
        """
        target = config.path / "held"
        generation = _own(target, "held")
        abandon_generation(generation)

        with pytest.raises(TargetOwnedError, match="abandoned"):
            claim_target_path(target, "held")

    def test_a_refused_re_reservation_is_left_in_place(self, config: Config) -> None:
        """Unlike a fresh one, it already belongs to the batch's lifecycle.

        It was taken before the batch gave up, so the release as the
        batch leaves -- or the hold-back for a worker still running --
        accounts for it.  Removing it here could hand a path this worker
        has part-written to a later batch.
        """
        target = config.path / "held"
        generation = _own(target, "held")
        abandon_generation(generation)

        with pytest.raises(TargetOwnedError):
            claim_target_path(target, "held")

        assert _owned_paths.get(target) == (generation, "held")

    def test_an_occupied_own_destination_is_refused_after_abandonment(
        self, config: Config
    ) -> None:
        """The retry path's other form: the directory is already there."""
        target = config.path / "retried"
        generation = _own(target, "retried")
        target.mkdir(parents=True)
        abandon_generation(generation)

        with pytest.raises(TargetOwnedError, match="abandoned"):
            claim_new_target(target, "retried")

    def test_a_refused_gerrit_worker_writes_nothing_after_abandonment(
        self, config: Config
    ) -> None:
        """End to end: no parent directories once the batch has given up.

        Conflict resolution reserves first and the pre-clone checks then
        re-reserve, so the re-reservation is where an abandonment that
        landed in between has to be caught -- before ``mkdir``.
        """
        project = _project("parent/child")
        target = get_project_path(project.name, config.path)
        generation = _own(target, project.name)
        abandon_generation(generation)

        with patch("gerrit_clone.worker.run_tracked") as mock_run:
            result = CloneWorker(config).clone_project(project)

        assert result.status == CloneStatus.FAILED
        # Not ``assert_not_called()``: on failure that prints the call's
        # arguments, and the clone environment carries the parent's --
        # tokens included.
        assert not mock_run.called, "launched git after abandonment"
        assert not target.parent.exists(), "created directories after abandonment"

    def test_a_retry_after_abandonment_writes_no_late_protection(
        self, config: Config
    ) -> None:
        """A retry reaches the late ancestor check after a delay.

        The batch can give up during that delay, and the check can write
        nested protection into a parent repository, so the
        re-reservation has to be asked first.
        """
        project = _project("parent/child")
        target = get_project_path(project.name, config.path)
        generation = _own(target, project.name)
        abandon_generation(generation)
        result = CloneResult(project=project, status=CloneStatus.CLONING, path=target)

        refused = False
        with (
            patch("gerrit_clone.worker.recheck_nested_ancestor") as recheck,
            patch("gerrit_clone.worker.run_tracked") as mock_run,
        ):
            try:
                CloneWorker(config)._perform_clone(project, target, result)
            except CloneError:
                refused = True

        assert refused, "the attempt went ahead after abandonment"
        assert not recheck.called, "looked for a late ancestor after abandonment"
        assert not mock_run.called, "launched git after abandonment"

    def test_a_reservation_taken_after_abandonment_is_given_back(
        self, config: Config
    ) -> None:
        """A late worker must not strand a destination for the process.

        A worker can reach its reservation after the batch was
        abandoned and its release has already run. Nothing would be
        left to give the path up, so it would stay blocked.
        """
        target = config.path / "late"
        generation = new_generation()
        enter_generation(generation)
        abandon_generation(generation)

        with pytest.raises(TargetOwnedError, match="abandoned"):
            claim_new_target(target, "late")

        assert target not in _owned_paths, "stranded a destination"

    def test_a_reservation_racing_abandonment_is_not_stranded(
        self, config: Config
    ) -> None:
        """The check publishes first, so either order is covered.

        Here the batch is abandoned and released *after* the entry goes
        in, which is the ordering the batch-wide release would catch.
        The worker's own check must not leave it behind either.
        """
        target = config.path / "racing"
        generation = new_generation()
        enter_generation(generation)

        real_abandoned = batch_abandoned

        def abandon_midway() -> bool:
            # Stands in for the batch giving up in the window between
            # the entry being published and this check running.
            abandon_generation(generation)
            release_claims(generation)
            return real_abandoned()

        with (
            patch(
                "gerrit_clone.clone_reservations.batch_abandoned",
                side_effect=abandon_midway,
            ),
            pytest.raises(TargetOwnedError, match="abandoned"),
        ):
            claim_new_target(target, "racing")

        assert target not in _owned_paths, "stranded a destination"

    def test_a_contested_github_target_is_not_reported_as_existing(
        self, config: Config
    ) -> None:
        """A rival's half-written clone must not read as a finished one.

        ``git clone`` creates ``.git`` before it transfers anything, so
        the shortcut would report a repository that the owner's own
        timeout cleanup is about to remove.
        """
        project = _project("contested")
        gh_config = Config(
            host="github.com/org",
            source_type=SourceType.GITHUB,
            path=config.path,
        )
        target = config.path / project.filesystem_path
        _own(target, "rival")

        # The rival's clone has reached the point of looking complete.
        (target / ".git").mkdir(parents=True)

        enter_generation(new_generation())
        result = clone_github_repository(project, gh_config)

        assert result.status == CloneStatus.FAILED, (
            "reported a rival's half-written clone as an existing repository"
        )

    def test_a_refused_github_clone_creates_no_directories(
        self, config: Config
    ) -> None:
        """Nothing is written before the reservation is held.

        ``mkdir(parents=True)`` can bring an ancestor into being that
        another batch reserved, failing the owner's clone on a directory
        that appeared beneath it.
        """
        project = _project("nested/child")
        gh_config = Config(
            host="github.com/org",
            source_type=SourceType.GITHUB,
            path=config.path,
        )
        ancestor = config.path / "nested"
        _own(ancestor, "ancestor-owner")

        enter_generation(new_generation())
        result = clone_github_repository(project, gh_config)

        assert result.status == CloneStatus.FAILED
        assert not ancestor.exists(), "created a directory another batch reserved"

    def test_a_same_batch_collision_is_contested(self, config: Config) -> None:
        """Two projects resolving to one path must not see each other's work.

        Name sanitisation is not injective -- ``repo`` and ``repo.``
        arrive as two projects for one destination -- so the second
        would find the first's clone in progress and report it as an
        existing repository rather than being refused.
        """
        owner = _project("repo")
        intruder = _project("repo.")
        target = get_project_path(owner.name, config.path)
        generation = _own(target, owner.name)

        assert reserved_by_other(target, intruder.name) == target
        # The holder itself is not contested by its own reservation,
        # which is how a retry re-checks.
        assert reserved_by_other(target, owner.name) is None
        assert generation is not None

    def test_an_interrupted_submission_clones_nothing(self, config: Config) -> None:
        """A worker is never left running outside the batch's records.

        Submission is two steps -- ``submit()`` starts the task, the
        mapping records it -- and an interrupt in between would leave a
        worker that may already hold a reservation but is invisible to
        the hold-back that protects it.
        """
        manager = CloneManager(config)
        cloned: list[str] = []
        original = _TrackedThreadPoolExecutor.submit
        submits = itertools.count(1)

        def submit_then_interrupt(
            pool: _TrackedThreadPoolExecutor, fn: object, *args: object, **kw: object
        ) -> Future[CloneResult]:
            if next(submits) > 1:
                raise KeyboardInterrupt
            return original(pool, fn, *args, **kw)  # type: ignore[arg-type]

        with (
            patch.object(
                CloneManager,
                "_clone_project_with_progress",
                lambda _self, project: cloned.append(project.name),
            ),
            patch.object(_TrackedThreadPoolExecutor, "submit", submit_then_interrupt),
            pytest.raises(KeyboardInterrupt),
        ):
            manager._execute_bulk_clone([_project("first"), _project("second")])

        assert cloned == [], "a worker ran outside the batch's records"

    def test_an_interrupt_during_the_gate_release_still_releases_it(
        self, config: Config
    ) -> None:
        """The release itself can be cut short by the interrupt.

        Workers parked on a gate nobody opens never finish, and the
        interpreter's exit-time join would then wait on them for good.
        """
        manager = CloneManager(config)
        cloned: list[str] = []
        real_event = threading.Event
        made: list[threading.Event] = []

        class _ReleaseInterrupted(threading.Event):
            """The gate, whose first release is interrupted part-way."""

            interrupted = False

            def set(self) -> None:
                if not self.interrupted:
                    self.interrupted = True
                    raise KeyboardInterrupt
                super().set()

        def event() -> threading.Event:
            # The gate is the first event the batch creates.
            made.append(_ReleaseInterrupted() if not made else real_event())
            return made[-1]

        raised = False
        try:
            with (
                patch.object(
                    CloneManager,
                    "_clone_project_with_progress",
                    lambda _self, project: cloned.append(project.name),
                ),
                patch("threading.Event", event),
            ):
                try:
                    manager._execute_bulk_clone([_project("first"), _project("second")])
                except KeyboardInterrupt:
                    raised = True

            assert raised, "the interrupt did not propagate"
            assert made[0].is_set(), "left the workers parked on the gate"
            assert cloned == [], "a worker cloned after the batch was interrupted"
        finally:
            # Opened regardless, so a failure here cannot hang the run.
            if made:
                made[0].set()

    def test_late_protection_is_refused_after_abandonment(self, config: Config) -> None:
        """It writes into a parent repository the batch does not own."""
        parent_repo = config.path / "parent"
        (parent_repo / ".git").mkdir(parents=True)
        generation = new_generation()
        enter_generation(generation)
        refuse_generation(generation)

        refused = False
        try:
            apply_late_nested_protection(
                parent_repo, parent_repo / "child", "parent/child", "parent"
            )
        except ProcessAbandonedError:
            refused = True

        assert refused, "wrote nested protection after abandonment"
        assert not (parent_repo / ".git" / "info" / "exclude").exists()

    def test_an_abandon_waits_for_a_protection_write_in_progress(
        self, config: Config
    ) -> None:
        """The abandonment check and the write it permits are one step.

        A batch giving up while a worker is mid-write must find the
        write finished; a separate check would let it land afterwards.
        """
        parent_repo = config.path / "parent"
        (parent_repo / ".git" / "info").mkdir(parents=True)
        generation = new_generation()
        writing = threading.Event()
        finish = threading.Event()
        real_open = Path.open

        def slow_open(self: Path, mode: str = "r", *args: Any, **kwargs: Any) -> Any:
            if mode == "a":
                writing.set()
                finish.wait(timeout=10)
            return real_open(self, mode, *args, **kwargs)

        def write() -> None:
            enter_generation(generation)
            apply_late_nested_protection(
                parent_repo, parent_repo / "child", "parent/child", "parent"
            )

        with patch.object(Path, "open", slow_open):
            writer = threading.Thread(target=write)
            writer.start()
            try:
                assert writing.wait(timeout=10), "the write never started"
                abandoner = threading.Thread(
                    target=refuse_generation, args=(generation,)
                )
                abandoner.start()
                abandoner.join(timeout=0.3)
                waited = abandoner.is_alive()
            finally:
                finish.set()
                writer.join(timeout=10)
            abandoner.join(timeout=10)

        assert waited, "the batch was abandoned in the middle of the write"
        assert "child" in (parent_repo / ".git" / "info" / "exclude").read_text()

    def test_a_worker_that_finishes_during_the_wait_is_cleaned_up(
        self, config: Config
    ) -> None:
        """The running set is a snapshot, so it is not the last word.

        A worker can finish between the wait returning and the loop
        reaching it.  Treating it as still running would hand its
        reservation to a callback that fires at once, releasing the path
        while the failed clone's directory was never discarded.
        """
        manager = CloneManager(config)
        project = _project("finished-late")
        target = get_project_path(project.name, config.path)
        generation = _own(target, project.name)
        target.mkdir(parents=True)
        (target / "objects").mkdir()

        late = _running_future()

        def finish_during_wait(
            futures: object, timeout: float | None = None
        ) -> tuple[set[object], set[object]]:
            # Reports the worker as unfinished, then lets it complete --
            # exactly the window the snapshot cannot see.
            late.set_result(
                CloneResult(project=project, status=CloneStatus.FAILED, path=target)
            )
            return set(), {late}

        with patch(
            "gerrit_clone.clone_timeout.wait_for_futures",
            side_effect=finish_during_wait,
        ):
            manager._handle_clone_timeout(
                {late: project}, [], OVERALL_TIMEOUT, generation
            )

        assert not target.exists(), "left a partial clone behind"

    def test_a_target_created_after_the_check_is_still_contested(
        self, config: Config
    ) -> None:
        """The first look runs while the path is absent, so it must be repeated.

        A clone always reserves before it creates anything, so a rival's
        directory has a reservation taken strictly earlier -- which a
        second look, once something is found, is guaranteed to see.
        """
        project = _project("appeared")
        gh_config = Config(
            host="github.com/org",
            source_type=SourceType.GITHUB,
            path=config.path,
        )
        target = config.path / project.filesystem_path
        enter_generation(new_generation())

        real_exists = Path.exists
        arrived = False

        def rival_arrives(self_: Path, *a: object, **kw: object) -> bool:
            nonlocal arrived
            if self_ == target and not arrived:
                # The rival reserves and creates its clone in the window
                # between the first check and this one.  Flagged before
                # reserving, the reservation itself asking whether the
                # path exists.
                arrived = True
                _own(target, "rival")
                (target / ".git").mkdir(parents=True)
                enter_generation(new_generation())
            return bool(real_exists(self_))

        with patch.object(Path, "exists", rival_arrives):
            result = clone_github_repository(project, gh_config)

        assert result.status == CloneStatus.FAILED, (
            "reported a rival's clone in progress as an existing repository"
        )

    def test_an_abandoned_github_clone_keeps_no_temporary_directory(
        self, config: Config
    ) -> None:
        """Termination must not be mistaken for a failure worth inspecting.

        Killing the child surfaces as a negative return code, which the
        ordinary policy can read as an error worth preserving the
        temporary clone for.  Cleanup only knows the reserved
        destination, so a randomly named ``.partial`` sibling would
        survive the run.
        """
        project = _project("abandoned")
        gh_config = Config(
            host="github.com/org",
            source_type=SourceType.GITHUB,
            path=config.path,
        )
        generation = new_generation()
        enter_generation(generation)

        def terminated_clone(*_a: object, **_kw: object) -> object:
            # The batch gives up while the child is running, which is
            # how the worker learns of it: a negative return code, with
            # stderr the preservation policy would want to keep.
            abandon_generation(generation)
            return SimpleNamespace(
                returncode=-15, stdout="", stderr="fatal: permission denied"
            )

        with patch(
            "gerrit_clone.github_worker.run_tracked", side_effect=terminated_clone
        ):
            result = clone_github_repository(project, gh_config)

        assert result.status == CloneStatus.FAILED
        leftovers = list(config.path.glob("*.partial*")) + list(
            config.path.glob("**/*.partial*")
        )
        assert leftovers == [], f"left a temporary clone behind: {leftovers}"

    def test_a_timed_out_parent_and_child_are_both_discarded(
        self, config: Config
    ) -> None:
        """A nested failure must not keep its parent's partial clone alive.

        Reservations outlive the cleanup, so the child's claim is still
        registered when the parent is considered.  Treating that as
        protection left the parent behind, and nothing revisits it once
        the child has been dealt with.
        """
        manager = CloneManager(config)
        parent = _project("parent")
        child = _project("parent/child")
        parent_path = get_project_path(parent.name, config.path)
        child_path = get_project_path(child.name, config.path)

        # Reserved before either exists, as a worker would.
        generation = _own(parent_path, parent.name)
        claim_new_target(child_path, child.name)
        child_path.mkdir(parents=True)
        (child_path / "objects").mkdir()

        futures = {
            _finished_future(
                CloneResult(project=parent, status=CloneStatus.FAILED, path=parent_path)
            ): parent,
            _finished_future(
                CloneResult(project=child, status=CloneStatus.FAILED, path=child_path)
            ): child,
        }
        manager._handle_clone_timeout(futures, [], OVERALL_TIMEOUT, generation)

        assert not parent_path.exists(), "left the parent's partial clone behind"

    def test_a_successful_nested_clone_still_protects_its_parent(
        self, config: Config
    ) -> None:
        """Only descendants being discarded stop protecting their parent.

        The exemption must not reach a nested clone that succeeded, or a
        timed-out parent would take a finished repository with it.
        """
        manager = CloneManager(config)
        parent = _project("parent")
        child = _project("parent/child")
        parent_path = get_project_path(parent.name, config.path)
        child_path = get_project_path(child.name, config.path)

        generation = _own(parent_path, parent.name)
        claim_new_target(child_path, child.name)
        child_path.mkdir(parents=True)

        futures = {
            _finished_future(
                CloneResult(project=parent, status=CloneStatus.FAILED, path=parent_path)
            ): parent,
            _finished_future(
                CloneResult(project=child, status=CloneStatus.SUCCESS, path=child_path)
            ): child,
        }
        manager._handle_clone_timeout(futures, [], OVERALL_TIMEOUT, generation)

        assert child_path.is_dir(), "destroyed a nested clone that had succeeded"
        assert parent_path.is_dir(), "removed a tree holding a finished clone"

    def test_holding_back_a_worker_that_already_stopped_cleans_up(
        self, config: Config
    ) -> None:
        """The future can complete between the check and the handover.

        The callback then fires immediately.  Releasing the reservation
        without discarding would leave exactly the partial directory the
        timeout path exists to remove.
        """
        project = _project("finished-in-the-gap")
        target = get_project_path(project.name, config.path)
        generation = _own(target, project.name)
        target.mkdir(parents=True)
        (target / "objects").mkdir()

        already_done = _finished_future(
            CloneResult(project=project, status=CloneStatus.FAILED, path=target)
        )
        _hold_back(project, target, generation, already_done)

        assert not target.exists(), "left the partial clone behind"
        assert target not in _owned_paths, "kept the reservation"

    def test_a_held_back_worker_cleans_up_when_it_finally_stops(
        self, config: Config
    ) -> None:
        """A worker outlasting the settle wait still tidies up after itself."""
        manager = CloneManager(config)
        project = _project("wedged")
        target = get_project_path(project.name, config.path)
        generation = _own(target, project.name)
        target.mkdir(parents=True)
        (target / "objects").mkdir()
        running = _running_future()

        with patch(
            "gerrit_clone.clone_timeout.wait_for_futures",
            return_value=(set(), {running}),
        ):
            manager._handle_clone_timeout(
                {running: project}, [], OVERALL_TIMEOUT, generation
            )

        assert target.is_dir(), "removed a directory a live worker still held"
        assert _owned_paths.get(target) == (generation, project.name)

        running.set_result(
            CloneResult(project=project, status=CloneStatus.FAILED, path=target)
        )

        assert not target.exists(), "left the partial clone behind"
        assert target not in _owned_paths, "kept the reservation"

    def test_each_worker_is_judged_once(self, config: Config) -> None:
        """The success verdict is taken once, not asked again to act on.

        Asking twice could classify the same worker two ways: judged
        failed while the discard set was built and successful when its
        own turn came, a clone would escape its own removal and still be
        exempted from its parent's -- so the parent's removal would take
        the finished repository with it.
        """
        manager = CloneManager(config)
        first = _project("first")
        second = _project("second")
        generation = _own(get_project_path(first.name, config.path), first.name)
        claim_new_target(get_project_path(second.name, config.path), second.name)

        judged: list[object] = []
        real = clone_cleanup._completed_successfully

        def counting(future: Future[CloneResult]) -> bool:
            judged.append(future)
            return real(future)

        futures = {
            _finished_future(
                CloneResult(project=first, status=CloneStatus.FAILED, path=config.path)
            ): first,
            _finished_future(
                CloneResult(
                    project=second, status=CloneStatus.SUCCESS, path=config.path
                )
            ): second,
        }
        with patch.object(clone_timeout, "_completed_successfully", counting):
            manager._handle_clone_timeout(futures, [], OVERALL_TIMEOUT, generation)

        assert len(judged) == 2, f"judged {len(judged)} times for 2 workers"

    def test_a_backup_destination_is_held_back_too(self, config: Config) -> None:
        """A clone can hold more than the path it clones into.

        Conflict resolution also reserves wherever it moves an
        obstruction aside.  Releasing that while the rename is still to
        come would let a later batch take a path this worker can still
        write to.
        """
        project = _project("wedged")
        target = get_project_path(project.name, config.path)
        generation = _own(target, project.name)
        backup = target.with_name(target.name + ".parent")
        claim_target_path(backup, project.name)
        running = _running_future()

        hold_back_running_claims(config, {running: project}, generation)

        assert release_claims(generation) == set(), "gave up a live destination"
        assert _owned_paths.get(backup) == (generation, project.name)

        running.set_result(
            CloneResult(project=project, status=CloneStatus.SUCCESS, path=target)
        )

        assert backup not in _owned_paths, "kept the backup reservation"

    def test_a_backup_destination_is_never_discarded(self, config: Config) -> None:
        """A backup holds content moved aside, not a partial clone.

        It is given back when the worker stops, never removed -- doing
        so would destroy the very thing the move was protecting.
        """
        project = _project("wedged")
        target = get_project_path(project.name, config.path)
        generation = _own(target, project.name)
        backup = target.with_name(target.name + ".parent")
        claim_target_path(backup, project.name)
        backup.mkdir(parents=True)
        (backup / "rescued.txt").write_text("moved out of the way")
        running = _running_future()

        hold_back_running_claims(config, {running: project}, generation)
        running.set_result(
            CloneResult(project=project, status=CloneStatus.FAILED, path=target)
        )

        assert (backup / "rescued.txt").read_text() == "moved out of the way", (
            "destroyed content that had been moved aside"
        )

    def test_a_held_back_parent_spares_a_held_back_child(self, config: Config) -> None:
        """Independent completions cannot see each other's outcome.

        A nested clone that succeeds gives its reservation back as it
        finishes, so an ancestor failing afterwards would find nothing
        beneath it and take the finished repository with it.
        """
        manager = CloneManager(config)
        parent = _project("parent")
        child = _project("parent/child")
        parent_path = get_project_path(parent.name, config.path)
        child_path = get_project_path(child.name, config.path)

        generation = _own(parent_path, parent.name)
        claim_new_target(child_path, child.name)
        parent_path.mkdir(parents=True)
        child_path.mkdir(parents=True)

        parent_future = _running_future()
        child_future = _running_future()

        with patch(
            "gerrit_clone.clone_timeout.wait_for_futures",
            return_value=(set(), {parent_future, child_future}),
        ):
            manager._handle_clone_timeout(
                {parent_future: parent, child_future: child},
                [],
                OVERALL_TIMEOUT,
                generation,
            )

        # The child finishes first, and succeeds.
        child_future.set_result(
            CloneResult(project=child, status=CloneStatus.SUCCESS, path=child_path)
        )
        # The parent then fails.
        parent_future.set_result(
            CloneResult(project=parent, status=CloneStatus.FAILED, path=parent_path)
        )

        assert child_path.is_dir(), "took a finished nested clone with the parent"

    def test_a_held_back_parent_spares_a_child_that_already_succeeded(
        self, config: Config
    ) -> None:
        """A finished nested clone protects its parent as a running one does.

        The batch gives a finished clone's reservation back as it leaves,
        so a parent failing afterwards would find nothing beneath it and
        take the repository with it.
        """
        manager = CloneManager(config)
        parent = _project("parent")
        child = _project("parent/child")
        parent_path = get_project_path(parent.name, config.path)
        child_path = get_project_path(child.name, config.path)

        generation = _own(parent_path, parent.name)
        claim_new_target(child_path, child.name)
        parent_path.mkdir(parents=True)
        child_path.mkdir(parents=True)

        parent_future = _running_future()
        child_future = _finished_future(
            CloneResult(project=child, status=CloneStatus.SUCCESS, path=child_path)
        )

        with patch(
            "gerrit_clone.clone_timeout.wait_for_futures",
            return_value=(set(), {parent_future}),
        ):
            manager._handle_clone_timeout(
                {parent_future: parent, child_future: child},
                [],
                OVERALL_TIMEOUT,
                generation,
            )

        assert child_path not in _owned_paths, "the premise: the batch gave it back"
        parent_future.set_result(
            CloneResult(project=parent, status=CloneStatus.FAILED, path=parent_path)
        )

        assert child_path.is_dir(), "took a finished nested clone with the parent"

    def test_the_handover_does_not_depend_on_the_order_of_workers(
        self, config: Config
    ) -> None:
        """A child handed over first can finish as it is handed over.

        Its completion then gives its reservation back at once, so a
        parent handed over after it must already have decided to spare
        it rather than asking again and finding nothing there.
        """
        manager = CloneManager(config)
        parent = _project("parent")
        child = _project("parent/child")
        parent_path = get_project_path(parent.name, config.path)
        child_path = get_project_path(child.name, config.path)

        generation = _own(parent_path, parent.name)
        claim_new_target(child_path, child.name)
        child_path.mkdir(parents=True)
        (child_path / "cloned.txt").write_text("the child's clone")

        parent_future = _running_future()
        child_future = _running_future()
        finishes = _finishing_when_handed_over(
            child_future,
            CloneResult(project=child, status=CloneStatus.SUCCESS, path=child_path),
        )

        with (
            patch(
                "gerrit_clone.clone_timeout.wait_for_futures",
                return_value=(set(), {parent_future, child_future}),
            ),
            patch.object(clone_cleanup, "hold_back_claim", finishes),
        ):
            # The child comes first.
            manager._handle_clone_timeout(
                {child_future: child, parent_future: parent},
                [],
                OVERALL_TIMEOUT,
                generation,
            )

        parent_future.set_result(
            CloneResult(project=parent, status=CloneStatus.FAILED, path=parent_path)
        )

        assert (child_path / "cloned.txt").read_text() == "the child's clone"

    def test_a_parent_discard_is_decided_before_any_worker_is_handed_over(
        self, config: Config
    ) -> None:
        """The discards ask the same question the handover does.

        A held-back child can finish as it is handed over and give its
        reservation back, so a failed parent must already have been
        spared by then rather than asking afterwards and finding nothing.
        """
        manager = CloneManager(config)
        parent = _project("parent")
        child = _project("parent/child")
        parent_path = get_project_path(parent.name, config.path)
        child_path = get_project_path(child.name, config.path)

        generation = _own(parent_path, parent.name)
        claim_new_target(child_path, child.name)
        child_path.mkdir(parents=True)
        (child_path / "cloned.txt").write_text("the child's clone")

        parent_future = _finished_future(
            CloneResult(project=parent, status=CloneStatus.FAILED, path=parent_path)
        )
        child_future = _running_future()
        finishes = _finishing_when_handed_over(
            child_future,
            CloneResult(project=child, status=CloneStatus.SUCCESS, path=child_path),
        )

        with (
            patch(
                "gerrit_clone.clone_timeout.wait_for_futures",
                return_value=(set(), {child_future}),
            ),
            patch.object(clone_cleanup, "hold_back_claim", finishes),
        ):
            manager._handle_clone_timeout(
                {parent_future: parent, child_future: child},
                [],
                OVERALL_TIMEOUT,
                generation,
            )

        assert (child_path / "cloned.txt").read_text() == "the child's clone"

    def test_a_held_back_clone_with_nothing_nested_still_cleans_up(
        self, config: Config
    ) -> None:
        """The rule above must not disable cleanup generally."""
        manager = CloneManager(config)
        alone = _project("alone")
        sibling = _project("sibling")
        alone_path = get_project_path(alone.name, config.path)
        sibling_path = get_project_path(sibling.name, config.path)

        generation = _own(alone_path, alone.name)
        claim_new_target(sibling_path, sibling.name)
        alone_path.mkdir(parents=True)
        sibling_path.mkdir(parents=True)

        alone_future = _running_future()
        sibling_future = _running_future()

        with patch(
            "gerrit_clone.clone_timeout.wait_for_futures",
            return_value=(set(), {alone_future, sibling_future}),
        ):
            manager._handle_clone_timeout(
                {alone_future: alone, sibling_future: sibling},
                [],
                OVERALL_TIMEOUT,
                generation,
            )

        alone_future.set_result(
            CloneResult(project=alone, status=CloneStatus.FAILED, path=alone_path)
        )

        assert not alone_path.exists(), "a sibling is not nested; cleanup must run"

    def test_an_invalid_project_name_is_skipped(self, config: Config) -> None:
        manager = CloneManager(config)
        results: list[CloneResult] = []

        manager._handle_clone_timeout(
            {Future(): _project("")}, results, OVERALL_TIMEOUT, new_generation()
        )

        assert len(results) == 1


class TestBatchReturnsPromptly:
    """The regression: the batch waited for the slowest running clone."""

    def test_a_running_clone_does_not_hold_the_batch(self, config: Config) -> None:
        """TimeoutError was caught inside the executor block, so leaving it
        took the ``shutdown(wait=True)`` path and blocked until the clone
        finished -- however long past ``overall_timeout`` that was.
        """
        manager = CloneManager(config)
        started = threading.Event()
        release = threading.Event()
        projects = [_project("slow-repo"), _project("queued-repo")]

        def slow_clone(project: Project) -> CloneResult:
            if project.name != "slow-repo":
                release.wait(timeout=60)
            started.set()
            # Stands in for a clone sitting inside git; a cancelled
            # future cannot reach it.
            release.wait(timeout=60)
            return CloneResult(
                project=project,
                status=CloneStatus.SUCCESS,
                path=config.path / project.name,
            )

        def consume(*args: object, **kwargs: object) -> None:
            assert started.wait(timeout=10)
            raise TimeoutError

        try:
            with (
                patch.object(manager, "_clone_project_with_progress", slow_clone),
                patch(
                    "gerrit_clone.clone_orchestrator.consume_clone_futures",
                    consume,
                ),
            ):
                begin = time.monotonic()
                results = manager._execute_bulk_clone(projects)
                elapsed = time.monotonic() - begin
        finally:
            release.set()

        # The stand-in clone waits up to 60s; anything close to that
        # means the exit waited for it.
        assert elapsed < 10, f"batch exit waited {elapsed:.1f}s for a running clone"
        assert {r.project.name for r in results} == {"slow-repo", "queued-repo"}
        assert all(r.status == CloneStatus.FAILED for r in results)
