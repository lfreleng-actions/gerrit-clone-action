# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""Tests for the clone orchestrator's overall-timeout handling.

When the batch exceeds its overall timeout the orchestrator returns
partial results rather than raising, so every submitted project must be
represented in the returned list -- including the ones still queued.
"""

from __future__ import annotations

import threading
import time
from concurrent.futures import Future
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch

import pytest

from gerrit_clone.clone_conflicts import resolve_path_conflict
from gerrit_clone.clone_orchestrator import CloneManager
from gerrit_clone.clone_timeout import (
    TargetOwnedError,
    _lingering,
    _owned_paths,
    _release_on_completion,
    claim_new_target,
    claim_target_path,
    release_claims,
)
from gerrit_clone.github_worker import clone_github_repository
from gerrit_clone.models import (
    CloneResult,
    CloneStatus,
    Config,
    Project,
    ProjectState,
    SourceType,
)
from gerrit_clone.pathing import get_project_path
from gerrit_clone.subprocess_tracking import (
    _thread_state,
    enter_generation,
    new_generation,
)
from gerrit_clone.worker import CloneWorker

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
        assert "another batch" in (result.error_message or "")
        mock_run.assert_not_called()

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

    def test_an_inaccessible_destination_is_not_claimed(self, config: Config) -> None:
        """If we cannot see it, we cannot claim we created it."""
        target = config.path / "unreadable"
        generation = new_generation()
        enter_generation(generation)

        with patch.object(Path, "exists", side_effect=PermissionError("no access")):
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
        assert "another batch" in (result.error_message or "")
        mock_run.assert_not_called()

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
                patch.object(manager, "_consume_clone_futures", consume),
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
