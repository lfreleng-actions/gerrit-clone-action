# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""Refreshing default clones, which are bare mirrors.

Clones default to ``git clone --mirror``, so a refresh has no ``.git``
directory to find them by and no checked-out branch to pull.  These run
real git against local upstreams, end to end: discovery, the refresh
itself -- through ``refresh`` and through re-running ``clone`` -- and
what the command reports afterwards.
"""

from __future__ import annotations

import json
import subprocess
from typing import TYPE_CHECKING, Any
from unittest.mock import patch

import pytest
from typer.testing import CliRunner

from gerrit_clone.cli import app
from gerrit_clone.clone_manager import _refresh_repositories
from gerrit_clone.content_filter import apply_content_filters
from gerrit_clone.content_origin import NO_PUSH_URL
from gerrit_clone.content_policy import FilterPolicy, add_policy
from gerrit_clone.content_spec import ContentFilterSpec
from gerrit_clone.models import (
    CloneStatus,
    Config,
    Project,
    ProjectState,
    RefreshStatus,
    RetryPolicy,
)
from gerrit_clone.refresh_filtered import SHALLOW_HISTORY_REFUSAL
from gerrit_clone.refresh_git_env import run_git
from gerrit_clone.refresh_manager import RefreshManager, refresh_repositories
from gerrit_clone.refresh_worker import FILTERED_WORKING_COPY_REFUSAL, RefreshWorker
from gerrit_clone.subprocess_tracking import ProcessAbandonedError

if TYPE_CHECKING:
    from pathlib import Path


def _git(*args: str, cwd: Path | None = None) -> str:
    """Run git with an identity and no signing, returning its stdout."""
    return subprocess.run(
        [
            "git",
            "-c",
            "user.email=test@example.com",
            "-c",
            "user.name=Test",
            "-c",
            "commit.gpgsign=false",
            *args,
        ],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()


def _upstream(path: Path) -> Path:
    """An upstream repository with one commit and a branch to delete."""
    _git("init", "-q", "-b", "main", str(path))
    (path / "file.txt").write_text("one\n")
    _git("add", "file.txt", cwd=path)
    _git("commit", "-q", "-m", "one", cwd=path)
    _git("branch", "doomed", cwd=path)
    return path


def _advance(upstream: Path) -> str:
    """Move *upstream* on -- a new commit, a deleted branch -- and return main."""
    (upstream / "file.txt").write_text("two\n")
    _git("commit", "-q", "-am", "two", cwd=upstream)
    _git("branch", "-q", "-D", "doomed", cwd=upstream)
    return _git("rev-parse", "main", cwd=upstream)


def _has_ref(repo: Path, ref: str) -> bool:
    return (
        subprocess.run(
            ["git", "rev-parse", "-q", "--verify", ref],
            cwd=repo,
            capture_output=True,
            check=False,
        ).returncode
        == 0
    )


@pytest.fixture
def tree(tmp_path: Path) -> Path:
    """A default clone tree: a mirror, with another mirror nested inside it.

    Nesting is how Gerrit hierarchies land on disk -- ``com/parent/child``
    is cloned inside the ``com/parent`` mirror -- so discovery has to
    search a bare repository's subdirectories without mistaking git's own
    for projects.
    """
    for name in ("parent", "child"):
        _upstream(tmp_path / f"up-{name}")
    base = tmp_path / "tree"
    (base / "com").mkdir(parents=True)
    _git(
        "clone",
        "-q",
        "--mirror",
        (tmp_path / "up-parent").as_uri(),
        str(base / "com/parent"),
    )
    _git(
        "clone",
        "-q",
        "--mirror",
        (tmp_path / "up-child").as_uri(),
        str(base / "com/parent/child"),
    )
    return base


def _worker(
    *,
    force: bool = False,
    force_hard: bool = False,
    auto_stash: bool = False,
    fetch_only: bool = False,
    filters: ContentFilterSpec | None = None,
) -> RefreshWorker:
    return RefreshWorker(
        retry_policy=RetryPolicy(max_attempts=1),
        timeout=30,
        filter_gerrit_only=False,
        ssh_jitter_seconds=0,
        force=force,
        force_hard=force_hard,
        auto_stash=auto_stash,
        fetch_only=fetch_only,
        content_filters=filters,
    )


def _mark_filtered(repo: Path) -> None:
    """Record on *repo* what ``--remove-files secret.txt`` leaves."""
    assert add_policy(repo, FilterPolicy.of(["secret.txt"], [], False))


def _spec(base: Path, remove_files: str = "secret.txt") -> ContentFilterSpec:
    """A run's filters, as ``--remove-files`` gives them."""
    spec = ContentFilterSpec.from_options(remove_files, None, False, base)
    assert spec is not None
    return spec


def _filtered_mirror(tree: Path) -> Path:
    """``com/parent``, fetched with a secret in it and then filtered."""
    upstream = tree.parent / "up-parent"
    (upstream / "secret.txt").write_text("hunter2\n")
    _git("add", "secret.txt", cwd=upstream)
    _git("commit", "-q", "-m", "oops", cwd=upstream)
    mirror = tree / "com/parent"
    assert _worker().refresh_repository(mirror).success
    success, error = apply_content_filters(
        mirror, "com/parent", remove_patterns=["secret.txt"]
    )
    assert success, error
    return mirror


def _history_files(repo: Path) -> list[str]:
    return _git("log", "--all", "--name-only", "--format=", cwd=repo).split()


def _stages_left(tree: Path) -> list[Path]:
    return list(tree.rglob(".gerrit-clone-stage-*"))


class TestDiscovery:
    """A mirror has no ``.git`` directory to be recognised by."""

    def test_mirrors_are_found_nested_ones_included(self, tree: Path) -> None:
        repos = RefreshManager().discover_local_repositories(tree)

        assert repos == [
            (tree / "com/parent").resolve(),
            (tree / "com/parent/child").resolve(),
        ]

    def test_git_own_directories_are_not_searched(self, tree: Path) -> None:
        """``modules`` holds whole git directories; they are not projects."""
        submodule_git_dir = tree / "com/parent/modules/sub"
        for part in ("objects", "refs"):
            (submodule_git_dir / part).mkdir(parents=True)
        for part in ("HEAD", "config"):
            (submodule_git_dir / part).write_text("")

        repos = RefreshManager().discover_local_repositories(tree)

        assert submodule_git_dir.resolve() not in repos
        assert len(repos) == 2

    def test_a_nested_project_named_like_a_git_directory_is_found(
        self, tree: Path
    ) -> None:
        """Only git's own ``logs`` is skipped, not a project called that."""
        nested = tree / "com/parent/logs"
        _git(
            "clone", "-q", "--mirror", (tree.parent / "up-child").as_uri(), str(nested)
        )

        repos = RefreshManager().discover_local_repositories(tree)

        assert nested.resolve() in repos
        assert len(repos) == 3

    def test_non_recursive_discovery_stops_at_a_mirror(self, tree: Path) -> None:
        repos = RefreshManager(recursive=False).discover_local_repositories(tree)

        assert repos == [(tree / "com/parent").resolve()]


class TestRefreshingAMirror:
    """A mirror is brought up to date by fetching every ref."""

    def test_new_commits_arrive_and_deleted_branches_go(self, tree: Path) -> None:
        mirror = tree / "com/parent"
        upstream_main = _advance(tree.parent / "up-parent")

        result = _worker().refresh_repository(mirror)

        assert result.status == RefreshStatus.SUCCESS, result.error_message
        assert result.was_behind
        assert _git("rev-parse", "main", cwd=mirror) == upstream_main
        assert not _has_ref(mirror, "refs/heads/doomed")

    def test_a_mirror_already_current_is_up_to_date(self, tree: Path) -> None:
        result = _worker().refresh_repository(tree / "com/parent")

        assert result.status == RefreshStatus.UP_TO_DATE, result.error_message

    def test_force_mode_fetches_without_working_tree_repairs(self, tree: Path) -> None:
        """No branch to switch, reset or stash: force changes nothing here."""
        mirror = tree / "com/parent"
        upstream_main = _advance(tree.parent / "up-parent")

        result = _worker(force=True, auto_stash=True).refresh_repository(mirror)

        assert result.status == RefreshStatus.SUCCESS, result.error_message
        assert not result.stash_created
        assert not result.hard_reset
        assert _git("rev-parse", "main", cwd=mirror) == upstream_main
        assert _git("rev-parse", "--is-bare-repository", cwd=mirror) == "true"

    def test_a_pull_setting_does_not_stop_a_mirror_refreshing(self, tree: Path) -> None:
        """Pulling is the default; a bare repository can only be fetched."""
        mirror = tree / "com/parent"
        upstream_main = _advance(tree.parent / "up-parent")

        result = _worker(fetch_only=False).refresh_repository(mirror)

        assert result.status == RefreshStatus.SUCCESS, result.error_message
        assert _git("rev-parse", "main", cwd=mirror) == upstream_main

    def test_a_bare_clone_without_a_fetch_refspec_is_skipped(self, tree: Path) -> None:
        """Fetching would update only ``FETCH_HEAD``; that is no refresh."""
        upstream = tree.parent / "up-child"
        bare = tree / "com/plainbare"
        _git("clone", "-q", "--bare", upstream.as_uri(), str(bare))
        before = _git("rev-parse", "main", cwd=bare)
        _advance(upstream)

        result = _worker().refresh_repository(bare)

        assert result.status == RefreshStatus.SKIPPED
        assert "no fetch refspec" in (result.error_message or "")
        assert _git("rev-parse", "main", cwd=bare) == before

    @pytest.mark.parametrize(
        "refspec",
        ["", "refs/heads/main", "^refs/heads/other", "refs/heads/main:"],
        ids=["empty", "no-destination", "negative", "empty-destination"],
    )
    def test_a_refspec_that_updates_no_ref_is_skipped(
        self, tree: Path, refspec: str
    ) -> None:
        """Git fetches these, exits 0 and stores nothing but ``FETCH_HEAD``."""
        upstream = tree.parent / "up-child"
        bare = tree / "com/plainbare"
        _git("clone", "-q", "--bare", upstream.as_uri(), str(bare))
        _git("config", "--add", "remote.origin.fetch", refspec, cwd=bare)
        before = _git("rev-parse", "main", cwd=bare)
        _advance(upstream)

        result = _worker().refresh_repository(bare)
        predicted = RefreshManager(
            dry_run=True, filter_gerrit_only=False
        ).refresh_repositories(tree, [bare])

        assert result.status == RefreshStatus.SKIPPED
        assert "no fetch refspec that updates a ref" in (result.error_message or "")
        assert _git("rev-parse", "main", cwd=bare) == before
        [prediction] = predicted.results
        assert prediction.status == RefreshStatus.SKIPPED

    def test_a_dry_run_predicts_both_outcomes(self, tree: Path) -> None:
        bare = tree / "com/plainbare"
        _git("clone", "-q", "--bare", (tree.parent / "up-child").as_uri(), str(bare))

        batch = RefreshManager(
            dry_run=True, filter_gerrit_only=False
        ).refresh_repositories(tree)

        statuses = {r.path.name: r.status for r in batch.results}
        assert statuses == {
            "parent": RefreshStatus.SUCCESS,
            "child": RefreshStatus.SUCCESS,
            "plainbare": RefreshStatus.SKIPPED,
        }

    def test_a_mirror_whose_remote_is_not_origin_is_refreshed(self, tree: Path) -> None:
        """The fetch is ``--all``, so any remote with a refspec will do."""
        upstream = tree.parent / "up-child"
        mirror = tree / "com/upstreamnamed"
        _git(
            "clone",
            "-q",
            "--mirror",
            "--origin",
            "upstream",
            upstream.as_uri(),
            str(mirror),
        )
        upstream_main = _advance(upstream)

        result = _worker().refresh_repository(mirror)

        assert result.status == RefreshStatus.SUCCESS, result.error_message
        assert _git("rev-parse", "main", cwd=mirror) == upstream_main

    def test_a_dry_run_predicts_the_refusal_of_a_filtered_mirror(
        self, tree: Path
    ) -> None:
        _mark_filtered(tree / "com/parent")

        def predicted(filters: ContentFilterSpec | None) -> RefreshStatus:
            batch = RefreshManager(
                dry_run=True, filter_gerrit_only=False, content_filters=filters
            ).refresh_repositories(tree)
            return {r.path.name: r.status for r in batch.results}["parent"]

        assert predicted(None) == RefreshStatus.SKIPPED
        assert predicted(_spec(tree, "unrelated.txt")) == RefreshStatus.SKIPPED
        assert predicted(_spec(tree)) == RefreshStatus.SUCCESS


class TestBothEntryPoints:
    """``refresh`` and a re-run ``clone`` share the worker, not the route."""

    def test_refresh_updates_a_default_clone_tree(self, tree: Path) -> None:
        upstream_mains = {
            name: _advance(tree.parent / f"up-{name}") for name in ("parent", "child")
        }

        batch = refresh_repositories(tree, filter_gerrit_only=False, threads=2)

        assert batch.total_count == 2
        assert batch.success_count == 2
        assert (
            _git("rev-parse", "main", cwd=tree / "com/parent")
            == (upstream_mains["parent"])
        )
        assert (
            _git("rev-parse", "main", cwd=tree / "com/parent/child")
            == (upstream_mains["child"])
        )

    def test_re_running_clone_refreshes_an_existing_mirror(self, tree: Path) -> None:
        """Previously skipped: \"no upstream tracking branch\"."""
        upstream_main = _advance(tree.parent / "up-parent")
        config = Config(host="gerrit.example.org", path=tree, quiet=True)
        project = Project(name="com/parent", state=ProjectState.ACTIVE)

        [result] = _refresh_repositories(config, [project])

        assert result.status == CloneStatus.REFRESHED, result.error_message
        assert _git("rev-parse", "main", cwd=tree / "com/parent") == upstream_main

    def test_re_running_clone_refreshes_a_filtered_mirror_only_to_refilter(
        self, tree: Path
    ) -> None:
        """The same refusal as ``refresh``, lifted when clone filters too."""
        mirror = tree / "com/parent"
        _mark_filtered(mirror)
        before = _git("rev-parse", "main", cwd=mirror)
        upstream_main = _advance(tree.parent / "up-parent")
        project = Project(name="com/parent", state=ProjectState.ACTIVE)
        config = Config(host="gerrit.example.org", path=tree, quiet=True)

        [refused] = _refresh_repositories(config, [project])

        assert refused.status == CloneStatus.SKIPPED
        assert _git("rev-parse", "main", cwd=mirror) == before

        config.content_filters = _spec(tree)
        [refreshed] = _refresh_repositories(config, [project])

        assert refreshed.status == CloneStatus.REFRESHED, refreshed.error_message
        assert refreshed.content_filtered
        assert _git("rev-parse", "main", cwd=mirror) == upstream_main


def _filtered_working_copy(tmp_path: Path) -> tuple[Path, Path]:
    """A working copy whose history content filtering has rewritten."""
    upstream = _upstream(tmp_path / "up")
    (upstream / "secret.txt").write_text("hunter2\n")
    _git("add", "secret.txt", cwd=upstream)
    _git("commit", "-q", "-m", "oops", cwd=upstream)
    checkout = tmp_path / "checkout"
    _git("clone", "-q", upstream.as_uri(), str(checkout))
    success, error = apply_content_filters(
        checkout, "checkout", remove_patterns=["secret.txt"]
    )
    assert success, error
    return upstream, checkout


class TestFilteredWorkingCopy:
    """A ``--no-mirror`` clone after content filtering rewrote its history.

    Its rewritten history cannot fast-forward, and resetting it to
    upstream would put the filtered content back in its working tree, so
    it is refused whatever the run's filters: it is left for re-cloning.
    """

    @pytest.mark.parametrize(
        ("force_hard", "with_filters"),
        [(False, False), (True, False), (True, True), (False, True)],
        ids=["plain", "force-hard", "force-hard-with-filters", "with-filters"],
    )
    def test_it_is_refused_and_left_as_it_was(
        self, tmp_path: Path, force_hard: bool, with_filters: bool
    ) -> None:
        upstream, checkout = _filtered_working_copy(tmp_path)
        before = _git("rev-parse", "main", cwd=checkout)
        _advance(upstream)
        filters = _spec(tmp_path) if with_filters else None

        result = _worker(force_hard=force_hard, filters=filters).refresh_repository(
            checkout
        )

        assert result.status == RefreshStatus.SKIPPED
        assert result.error_message == FILTERED_WORKING_COPY_REFUSAL
        assert _git("rev-parse", "main", cwd=checkout) == before
        assert "secret.txt" not in _history_files(checkout)


class TestStagedRefresh:
    """A filtered mirror is refreshed through a filtered copy, or not at all."""

    def test_it_is_refreshed_and_stays_filtered(self, tree: Path) -> None:
        mirror = _filtered_mirror(tree)
        _advance(tree.parent / "up-parent")

        result = _worker(filters=_spec(tree)).refresh_repository(mirror)

        assert result.status == RefreshStatus.SUCCESS, result.error_message
        assert result.content_filtered
        assert _git("show", "main:file.txt", cwd=mirror) == "two"
        assert "secret.txt" not in _history_files(mirror)
        assert not _has_ref(mirror, "refs/heads/doomed")
        assert _git("config", "remote.origin.pushurl", cwd=mirror) == NO_PUSH_URL
        assert _stages_left(tree) == []

    def test_a_current_mirror_is_up_to_date(self, tree: Path) -> None:
        """Re-filtering unchanged history reproduces it exactly."""
        mirror = _filtered_mirror(tree)
        before = _git("for-each-ref", cwd=mirror)

        result = _worker(filters=_spec(tree)).refresh_repository(mirror)

        assert result.status == RefreshStatus.UP_TO_DATE, result.error_message
        assert _git("for-each-ref", cwd=mirror) == before

    def test_a_failed_re_filter_leaves_the_mirror_as_it_was(self, tree: Path) -> None:
        mirror = _filtered_mirror(tree)
        before = _git("for-each-ref", cwd=mirror)
        _advance(tree.parent / "up-parent")

        with patch(
            "gerrit_clone.refresh_filtered.apply_content_filters",
            return_value=(False, "filter-repo failed"),
        ):
            result = _worker(filters=_spec(tree)).refresh_repository(mirror)

        assert result.status == RefreshStatus.FAILED
        assert "left as it was" in (result.error_message or "")
        assert _git("for-each-ref", cwd=mirror) == before
        assert "secret.txt" not in _history_files(mirror)
        assert _stages_left(tree) == []

    def test_a_failed_fetch_leaves_the_mirror_as_it_was(self, tree: Path) -> None:
        mirror = _filtered_mirror(tree)
        before = _git("for-each-ref", cwd=mirror)
        gone = (tree.parent / "gone").as_uri()
        _git("config", "remote.origin.url", gone, cwd=mirror)

        result = _worker(filters=_spec(tree)).refresh_repository(mirror)

        assert result.status == RefreshStatus.FAILED
        assert _git("for-each-ref", cwd=mirror) == before
        assert _stages_left(tree) == []

    def test_a_remote_read_failure_reports_its_own_error(self, tree: Path) -> None:
        """Not the clone's stderr, which is empty when the clone worked."""
        mirror = _filtered_mirror(tree)
        before = _git("for-each-ref", cwd=mirror)

        def unreadable(
            cmd: list[str], *args: Any, **kwargs: Any
        ) -> subprocess.CompletedProcess[str]:
            if "--get-regexp" in cmd:
                return subprocess.CompletedProcess(cmd, 128, "", "bad config line")
            ran: subprocess.CompletedProcess[str] = run_git(cmd, *args, **kwargs)
            return ran

        with patch("gerrit_clone.refresh_filtered.run_git", unreadable):
            result = _worker(filters=_spec(tree)).refresh_repository(mirror)

        assert result.status == RefreshStatus.FAILED
        assert "bad config line" in (result.error_message or "")
        assert _git("for-each-ref", cwd=mirror) == before
        assert _stages_left(tree) == []

    def test_a_push_guard_failure_is_reported_as_such(self, tree: Path) -> None:
        """Told apart from failing to record the policy."""
        mirror = _filtered_mirror(tree)
        before = _git("for-each-ref", cwd=mirror)
        _advance(tree.parent / "up-parent")

        with patch("gerrit_clone.content_policy.block_pushes", return_value=False):
            result = _worker(filters=_spec(tree)).refresh_repository(mirror)

        assert result.status == RefreshStatus.FAILED
        assert "block pushing" in (result.error_message or "")
        assert "policy" not in (result.error_message or "")
        assert _git("for-each-ref", cwd=mirror) == before
        assert _stages_left(tree) == []

    def test_a_copy_still_attached_to_the_mirror_is_not_fetched(
        self, tree: Path
    ) -> None:
        """Its origin would still read the mirror, not upstream."""
        mirror = _filtered_mirror(tree)
        before = _git("for-each-ref", cwd=mirror)
        _advance(tree.parent / "up-parent")

        def stuck(
            cmd: list[str], *args: Any, **kwargs: Any
        ) -> subprocess.CompletedProcess[str]:
            if cmd[1:3] == ["remote", "remove"]:
                return subprocess.CompletedProcess(cmd, 1, "", "could not remove")
            ran: subprocess.CompletedProcess[str] = run_git(cmd, *args, **kwargs)
            return ran

        with patch("gerrit_clone.refresh_filtered.run_git", stuck):
            result = _worker(filters=_spec(tree)).refresh_repository(mirror)

        assert result.status == RefreshStatus.FAILED
        assert "could not remove" in (result.error_message or "")
        assert _git("for-each-ref", cwd=mirror) == before
        assert _stages_left(tree) == []

    def test_a_copy_that_cannot_be_removed_fails_the_refresh(self, tree: Path) -> None:
        """It may hold unfiltered history, so it is reported, not ignored."""
        mirror = _filtered_mirror(tree)
        _advance(tree.parent / "up-parent")

        def undeletable(path: Path, ignore_errors: bool = False) -> None:
            if not ignore_errors:
                raise PermissionError(13, "Permission denied", str(path))

        with patch("gerrit_clone.refresh_filtered.shutil.rmtree", undeletable):
            result = _worker(filters=_spec(tree)).refresh_repository(mirror)

        [stage] = _stages_left(tree)
        assert result.status == RefreshStatus.FAILED
        assert str(stage) in (result.error_message or "")
        assert "unfiltered history" in (result.error_message or "")

    @pytest.mark.parametrize(
        ("raised", "reported"),
        [
            (ProcessAbandonedError("abandoned"), "Refresh abandoned"),
            (RuntimeError("boom"), "Unexpected error: boom"),
        ],
        ids=["abandoned", "unexpected"],
    )
    def test_a_copy_left_behind_is_still_named_when_the_refresh_stops(
        self, tree: Path, raised: BaseException, reported: str
    ) -> None:
        """Not overwritten by the reason the refresh stopped."""
        mirror = _filtered_mirror(tree)
        _advance(tree.parent / "up-parent")

        def undeletable(path: Path, ignore_errors: bool = False) -> None:
            if not ignore_errors:
                raise PermissionError(13, "Permission denied", str(path))

        with (
            patch("gerrit_clone.refresh_filtered.shutil.rmtree", undeletable),
            patch(
                "gerrit_clone.refresh_filtered.apply_content_filters",
                side_effect=raised,
            ),
        ):
            result = _worker(filters=_spec(tree)).refresh_repository(mirror)

        [stage] = _stages_left(tree)
        assert result.status == RefreshStatus.FAILED
        assert reported in (result.error_message or "")
        assert str(stage) in (result.error_message or "")
        assert "unfiltered history" in (result.error_message or "")

    def test_other_filters_are_refused(self, tree: Path) -> None:
        """Any filter is not enough: ``unrelated.txt`` would let it back."""
        mirror = _filtered_mirror(tree)
        before = _git("for-each-ref", cwd=mirror)
        _advance(tree.parent / "up-parent")

        result = _worker(filters=_spec(tree, "unrelated.txt")).refresh_repository(
            mirror
        )

        assert result.status == RefreshStatus.SKIPPED
        assert "--remove-files secret.txt" in (result.error_message or "")
        assert _git("for-each-ref", cwd=mirror) == before

    def test_a_staging_copy_is_never_discovered(self, tree: Path) -> None:
        stage = tree / "com/parent/.gerrit-clone-stage-x/repo.git"
        _git("clone", "-q", "--mirror", str(tree / "com/parent"), str(stage))

        repos = RefreshManager().discover_local_repositories(tree)

        assert stage.resolve() not in repos


def _shallow_filtered_mirror(tree: Path) -> Path:
    """A filtered mirror holding only the latest commit of its history."""
    upstream = tree.parent / "up-parent"
    (upstream / "file.txt").write_text("two\n")
    _git("commit", "-q", "-am", "two", cwd=upstream)
    mirror = tree / "com/shallow"
    _git("clone", "-q", "--mirror", "--depth", "1", upstream.as_uri(), str(mirror))
    assert _git("rev-parse", "--is-shallow-repository", cwd=mirror) == "true"
    _mark_filtered(mirror)
    return mirror


class TestStagedRefreshRefusals:
    """What a staged refresh refuses, and the dry run predicts alike."""

    def test_history_filters_are_refused_on_a_shallow_mirror(self, tree: Path) -> None:
        """Truncated history hides older secrets; it must not pass as filtered."""
        mirror = _shallow_filtered_mirror(tree)
        before = _git("for-each-ref", cwd=mirror)
        spec = ContentFilterSpec.from_options("secret.txt", None, True, tree)
        assert spec is not None

        result = _worker(filters=spec).refresh_repository(mirror)
        predicted = RefreshManager(
            dry_run=True, filter_gerrit_only=False, content_filters=spec
        ).refresh_repositories(tree, [mirror])

        assert result.status == RefreshStatus.SKIPPED
        assert result.error_message == SHALLOW_HISTORY_REFUSAL
        assert _git("for-each-ref", cwd=mirror) == before
        [prediction] = predicted.results
        assert prediction.status == RefreshStatus.SKIPPED
        assert prediction.error_message == SHALLOW_HISTORY_REFUSAL

    def test_a_shallow_mirror_still_refreshes_under_file_removal_alone(
        self, tree: Path
    ) -> None:
        """``--remove-files`` does not depend on history it cannot see."""
        mirror = _shallow_filtered_mirror(tree)

        result = _worker(filters=_spec(tree)).refresh_repository(mirror)

        assert result.success, result.error_message

    def test_the_dry_run_predicts_a_refspec_less_refusal(self, tree: Path) -> None:
        """The refresh checks for a fetch refspec; so must its prediction."""
        bare = tree / "com/plainbare"
        _git("clone", "-q", "--bare", (tree.parent / "up-child").as_uri(), str(bare))
        _mark_filtered(bare)

        real = _worker(filters=_spec(tree)).refresh_repository(bare)
        predicted = RefreshManager(
            dry_run=True, filter_gerrit_only=False, content_filters=_spec(tree)
        ).refresh_repositories(tree, [bare])

        assert real.status == RefreshStatus.SKIPPED
        [prediction] = predicted.results
        assert prediction.status == RefreshStatus.SKIPPED
        assert prediction.error_message == real.error_message


class TestReporting:
    """What the ``refresh`` command says must match what it did."""

    def test_the_default_gerrit_only_refresh_reads_every_remote(
        self, tree: Path
    ) -> None:
        """A Gerrit mirror cloned with ``--origin upstream`` is still Gerrit.

        Run as a dry run, through the command's defaults, so that no real
        Gerrit server is needed.
        """
        mirror = tree / "com/parent"
        _git("remote", "rename", "origin", "upstream", cwd=mirror)
        gerrit_url = "ssh://gerrit.example.org:29418/com/parent"
        _git("config", "remote.upstream.url", gerrit_url, cwd=mirror)

        outcome = CliRunner().invoke(
            app,
            ["refresh", "--output-path", str(tree), "--dry-run"],
        )

        assert outcome.exit_code == 0, outcome.output
        [manifest] = tree.glob("refresh-manifest-*.json")
        statuses = {
            r["project"]: r["status"]
            for r in json.loads(manifest.read_text())["results"]
        }
        assert statuses["parent"] == RefreshStatus.SUCCESS.value

    def test_an_empty_tree_is_not_reported_as_refreshed(self, tmp_path: Path) -> None:
        empty = tmp_path / "empty"
        empty.mkdir()

        outcome = CliRunner().invoke(
            app, ["refresh", "--output-path", str(empty), "--all-repos"]
        )

        assert outcome.exit_code == 1
        assert "No repositories found to refresh" in outcome.output
        assert "All repositories refreshed successfully" not in outcome.output

    def test_a_partial_refresh_is_not_reported_as_complete(self, tree: Path) -> None:
        _git(
            "clone",
            "-q",
            "--bare",
            (tree.parent / "up-child").as_uri(),
            str(tree / "com/plainbare"),
        )

        outcome = CliRunner().invoke(
            app, ["refresh", "--output-path", str(tree), "--all-repos"]
        )

        assert outcome.exit_code == 0, outcome.output
        assert "1 of 3 repositories were not refreshed" in outcome.output
        assert "All repositories refreshed successfully" not in outcome.output

    def test_a_default_clone_tree_is_refreshed_via_the_command(
        self, tree: Path
    ) -> None:
        upstream_main = _advance(tree.parent / "up-parent")

        outcome = CliRunner().invoke(
            app, ["refresh", "--output-path", str(tree), "--all-repos"]
        )

        assert outcome.exit_code == 0, outcome.output
        assert "All repositories refreshed successfully" in outcome.output
        assert _git("rev-parse", "main", cwd=tree / "com/parent") == upstream_main

    def test_content_filters_run_on_a_refreshed_mirror(self, tree: Path) -> None:
        """Previously never reached: nothing was found to filter."""
        upstream = tree.parent / "up-parent"
        (upstream / "secret.txt").write_text("hunter2\n")
        # A change beside the secret, so the commit outlives its removal.
        (upstream / "file.txt").write_text("fetched\n")
        _git("add", "secret.txt", "file.txt", cwd=upstream)
        _git("commit", "-q", "-m", "oops", cwd=upstream)

        outcome = CliRunner().invoke(
            app,
            [
                "refresh",
                "--output-path",
                str(tree),
                "--all-repos",
                "--remove-files",
                "secret.txt",
            ],
        )

        assert outcome.exit_code == 0, outcome.output
        mirror = tree / "com/parent"
        assert _git("show", "main:file.txt", cwd=mirror) == "fetched"
        history = _git("log", "--all", "--name-only", "--format=", cwd=mirror)
        assert "secret.txt" not in history.split()

    def test_a_filtered_mirror_is_refreshed_again_with_its_filters(
        self, tree: Path
    ) -> None:
        """``git filter-repo`` removes ``origin`` when it rewrites history.

        Without it a mirror has nothing to fetch from, and every later
        refresh would skip it.  Refreshed with the same filters, the new
        commits arrive and the filtered file stays gone.
        """
        upstream = tree.parent / "up-parent"
        (upstream / "secret.txt").write_text("hunter2\n")
        _git("add", "secret.txt", cwd=upstream)
        _git("commit", "-q", "-m", "oops", cwd=upstream)
        command = [
            "refresh",
            "--output-path",
            str(tree),
            "--all-repos",
            "--remove-files",
            "secret.txt",
        ]
        filtered = CliRunner().invoke(app, command)
        assert filtered.exit_code == 0, filtered.output
        _advance(upstream)

        again = CliRunner().invoke(app, command)

        assert again.exit_code == 0, again.output
        assert "All repositories refreshed successfully" in again.output
        mirror = tree / "com/parent"
        assert _git("show", "main:file.txt", cwd=mirror) == "two"
        history = _git("log", "--all", "--name-only", "--format=", cwd=mirror)
        assert "secret.txt" not in history.split()
        assert _git("config", "remote.origin.pushurl", cwd=mirror) == NO_PUSH_URL

    def test_a_filtered_mirror_is_refreshed_without_restating_its_filters(
        self, tree: Path
    ) -> None:
        """The tree recorded them; the run applies them as before.

        Fetching ``+refs/*:refs/*`` alone would force the original refs
        back, the removed file with them.
        """
        upstream = tree.parent / "up-parent"
        (upstream / "secret.txt").write_text("hunter2\n")
        _git("add", "secret.txt", cwd=upstream)
        _git("commit", "-q", "-m", "oops", cwd=upstream)
        command = ["refresh", "--output-path", str(tree), "--all-repos"]
        filtered = CliRunner().invoke(app, [*command, "--remove-files", "secret.txt"])
        assert filtered.exit_code == 0, filtered.output
        mirror = tree / "com/parent"
        _advance(upstream)

        again = CliRunner().invoke(app, command)

        assert again.exit_code == 0, again.output
        assert "All repositories refreshed successfully" in again.output
        assert _git("show", "main:file.txt", cwd=mirror) == "two"
        history = _git("log", "--all", "--name-only", "--format=", cwd=mirror)
        assert "secret.txt" not in history.split()

    def test_a_dry_run_applies_no_content_filters(self, tree: Path) -> None:
        """Filtering rewrites history; a dry run promises no changes."""
        upstream = tree.parent / "up-parent"
        (upstream / "secret.txt").write_text("hunter2\n")
        _git("add", "secret.txt", cwd=upstream)
        _git("commit", "-q", "-m", "oops", cwd=upstream)
        command = ["refresh", "--output-path", str(tree), "--all-repos"]
        fetched = CliRunner().invoke(app, command)
        assert fetched.exit_code == 0, fetched.output
        mirror = tree / "com/parent"
        refs = _git("for-each-ref", cwd=mirror)

        outcome = CliRunner().invoke(
            app, [*command, "--dry-run", "--remove-files", "secret.txt"]
        )

        assert outcome.exit_code == 0, outcome.output
        assert "content filters not applied" in outcome.output
        assert _git("for-each-ref", cwd=mirror) == refs

    def test_git_filter_names_a_nested_mirror_hierarchically(self, tree: Path) -> None:
        """``com/parent/child``, as at clone time -- not just ``child``."""
        token = "sekrit-token-4f9a2c"
        upstream = tree.parent / "up-child"
        (upstream / "file.txt").write_text(f"key={token}\n")
        _git("commit", "-q", "-am", "leak", cwd=upstream)

        outcome = CliRunner().invoke(
            app,
            [
                "refresh",
                "--output-path",
                str(tree),
                "--all-repos",
                "--git-filter",
                f"com/parent/child:{token}",
            ],
        )

        assert outcome.exit_code == 0, outcome.output
        child = tree / "com/parent/child"
        assert _git("log", "-1", "--format=%s", "main", cwd=child) == "leak"
        assert token not in _git("log", "--all", "-p", cwd=child)
