# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""Refreshing a working copy that content filters cover.

A ``--no-mirror`` clone used to pull first and be filtered afterwards,
in place.  A failed filter left the new, unfiltered content in the
checkout; and a filter that ran at all rewrote the checkout itself --
its remote-tracking refs folded into local branches -- after which every
later refresh skipped it.

It is refreshed through a filtered copy instead, as a mirror is: the
copy fetches and is filtered, and the checkout takes what arrived only
if both worked, and only if it extends what the checkout holds.
"""

from __future__ import annotations

import subprocess
from contextlib import contextmanager
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any
from unittest.mock import patch

import pytest
from typer.testing import CliRunner

from gerrit_clone import content_stage, refresh_checkout_stage
from gerrit_clone.cli import app
from gerrit_clone.content_spec import ContentFilterSpec
from gerrit_clone.models import RefreshResult, RefreshStatus, RetryPolicy
from gerrit_clone.refresh_checkout_stage import (
    NO_FILTER_REPO_REFUSAL,
    UPSTREAM_GONE_REFUSAL,
)
from gerrit_clone.refresh_filtered import OVERTAKEN_REFUSAL, SHALLOW_HISTORY_REFUSAL
from gerrit_clone.refresh_manager import RefreshManager
from gerrit_clone.refresh_worker import (
    FILTERED_REFRESH_REFUSAL,
    FILTERED_WORKING_COPY_REFUSAL,
    RefreshWorker,
)

if TYPE_CHECKING:
    from collections.abc import Generator
    from pathlib import Path


def _git(*args: str, cwd: Path | None = None) -> str:
    return subprocess.run(
        [
            "git",
            "-c",
            "user.email=t@example.com",
            "-c",
            "user.name=T",
            "-c",
            "commit.gpgsign=false",
            *args,
        ],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()


def _commit(repo: Path, name: str, content: str) -> str:
    (repo / name).write_text(content)
    _git("add", name, cwd=repo)
    _git("commit", "-q", "-m", f"{name}: {content.strip()}", cwd=repo)
    return _git("rev-parse", "HEAD", cwd=repo)


@pytest.fixture
def upstream(tmp_path: Path) -> Path:
    path = tmp_path / "up"
    path.mkdir()
    _git("init", "-q", "-b", "main", str(path))
    _commit(path, "file.txt", "one\n")
    return path


@pytest.fixture
def checkout(tmp_path: Path, upstream: Path) -> Path:
    path = tmp_path / "tree" / "proj"
    _git("clone", "-q", upstream.as_uri(), str(path))
    return path


def _spec(checkout: Path, pattern: str = "secret.txt") -> ContentFilterSpec:
    return ContentFilterSpec([pattern], None, False, checkout.parent)


def _worker(
    filters: ContentFilterSpec | None,
    *,
    fetch_only: bool = False,
    auto_stash: bool = False,
    strategy: str = "merge",
    prune: bool = True,
) -> RefreshWorker:
    return RefreshWorker(
        retry_policy=RetryPolicy(max_attempts=1),
        timeout=60,
        filter_gerrit_only=False,
        ssh_jitter_seconds=0,
        fetch_only=fetch_only,
        prune=prune,
        auto_stash=auto_stash,
        strategy=strategy,
        content_filters=filters,
    )


def _history(repo: Path) -> list[str]:
    return _git("log", "--all", "--name-only", "--format=", cwd=repo).split()


def _refs(repo: Path) -> str:
    return _git("for-each-ref", "--format=%(refname) %(objectname)", cwd=repo)


def _stages_left(checkout: Path) -> list[Path]:
    return list((checkout / ".git").glob("gerrit-clone-stage-*"))


def _failing(*_args: object, **_kwargs: object) -> tuple[bool, str | None]:
    return False, "filter-repo failed"


class TestArrivingContent:
    def test_it_is_filtered_before_it_lands(
        self, upstream: Path, checkout: Path
    ) -> None:
        _commit(upstream, "secret.txt", "hunter2\n")
        _commit(upstream, "file.txt", "two\n")

        result = _worker(_spec(checkout)).refresh_repository(checkout)

        assert result.status == RefreshStatus.SUCCESS, result.error_message
        assert (checkout / "file.txt").read_text() == "two\n"
        assert not (checkout / "secret.txt").exists()
        assert "secret.txt" not in _history(checkout)
        assert _stages_left(checkout) == []
        assert _git("status", "--porcelain", cwd=checkout) == ""

    def test_the_checkout_keeps_its_layout(
        self, upstream: Path, checkout: Path
    ) -> None:
        """Remote-tracking refs stay remote-tracking refs, branches branches."""
        _commit(upstream, "secret.txt", "hunter2\n")
        branches = _git("branch", "--format=%(refname)", cwd=checkout)

        _worker(_spec(checkout)).refresh_repository(checkout)

        assert _git("branch", "--format=%(refname)", cwd=checkout) == branches
        assert "secret.txt" not in _history(checkout)
        assert _git("rev-parse", "--abbrev-ref", "main@{upstream}", cwd=checkout) == (
            "origin/main"
        )

    def test_later_content_is_followed_too(
        self, upstream: Path, checkout: Path
    ) -> None:
        """Rewritten once, the checkout still fast-forwards next time."""
        _commit(upstream, "secret.txt", "hunter2\n")
        _commit(upstream, "file.txt", "two\n")
        first = _worker(_spec(checkout)).refresh_repository(checkout)
        assert first.status == RefreshStatus.SUCCESS, first.error_message
        _commit(upstream, "file.txt", "three\n")

        again = _worker(_spec(checkout)).refresh_repository(checkout)

        assert again.status == RefreshStatus.SUCCESS, again.error_message
        assert (checkout / "file.txt").read_text() == "three\n"
        assert "secret.txt" not in _history(checkout)

    def test_a_fetch_only_refresh_updates_remote_tracking_refs(
        self, upstream: Path, checkout: Path
    ) -> None:
        head = _git("rev-parse", "HEAD", cwd=checkout)
        _commit(upstream, "secret.txt", "hunter2\n")
        _commit(upstream, "file.txt", "two\n")

        result = _worker(_spec(checkout), fetch_only=True).refresh_repository(checkout)

        assert result.status == RefreshStatus.SUCCESS, result.error_message
        assert _git("rev-parse", "HEAD", cwd=checkout) == head
        assert _git("rev-parse", "origin/main", cwd=checkout) != head
        assert "secret.txt" not in _history(checkout)

    def test_a_rebase_puts_local_commits_on_the_filtered_history(
        self, upstream: Path, checkout: Path
    ) -> None:
        _commit(checkout, "local.txt", "mine\n")
        _commit(upstream, "secret.txt", "hunter2\n")
        _commit(upstream, "file.txt", "two\n")

        result = _worker(_spec(checkout), strategy="rebase").refresh_repository(
            checkout
        )

        assert result.status == RefreshStatus.SUCCESS, result.error_message
        assert (checkout / "local.txt").read_text() == "mine\n"
        assert "secret.txt" not in _history(checkout)
        assert _git("rev-parse", "HEAD~1", cwd=checkout) == _git(
            "rev-parse", "origin/main", cwd=checkout
        )


class TestTags:
    def test_a_local_tag_is_left_alone_and_upstream_ones_arrive(
        self, upstream: Path, checkout: Path
    ) -> None:
        """The copy stands in for upstream: tags the operator made are theirs."""
        _git("checkout", "-q", "-b", "scratch", cwd=checkout)
        _commit(checkout, "secret.txt", "hunter2\n")
        _git("tag", "mine", cwd=checkout)
        _git("checkout", "-q", "main", cwd=checkout)
        mine = _git("rev-parse", "mine", cwd=checkout)
        _commit(upstream, "file.txt", "two\n")
        _git("tag", "v1", cwd=upstream)

        result = _worker(_spec(checkout)).refresh_repository(checkout)

        assert result.status == RefreshStatus.SUCCESS, result.error_message
        assert _git("rev-parse", "mine", cwd=checkout) == mine
        assert _git("rev-parse", "v1^{commit}", cwd=checkout) == _git(
            "rev-parse", "main", cwd=upstream
        )


class TestNoPrune:
    def test_a_branch_deleted_upstream_is_kept_and_filtered(
        self, upstream: Path, checkout: Path
    ) -> None:
        """The copy holds the checkout's remote-tracking refs, kept or not."""
        _git("checkout", "-q", "-b", "doomed", cwd=upstream)
        _commit(upstream, "secret.txt", "hunter2\n")
        _commit(upstream, "notes.txt", "n\n")
        _git("checkout", "-q", "main", cwd=upstream)
        _git("fetch", "-q", cwd=checkout)
        _git("branch", "-q", "-D", "doomed", cwd=upstream)
        _commit(upstream, "file.txt", "two\n")

        result = _worker(_spec(checkout), prune=False).refresh_repository(checkout)

        assert result.status == RefreshStatus.SUCCESS, result.error_message
        assert _git("rev-parse", "-q", "--verify", "origin/doomed", cwd=checkout)
        assert "notes.txt" in _history(checkout)
        assert "secret.txt" not in _history(checkout)


class TestConflicts:
    def test_a_rebase_conflict_is_reported_as_one(
        self, upstream: Path, checkout: Path
    ) -> None:
        _commit(checkout, "file.txt", "mine\n")
        _commit(upstream, "file.txt", "theirs\n")

        result = _worker(_spec(checkout), strategy="rebase").refresh_repository(
            checkout
        )

        assert result.status == RefreshStatus.CONFLICTS, result.error_message
        assert "CONFLICT" in (result.error_message or "")


class TestLeftAsItWas:
    def test_when_filtering_fails(self, upstream: Path, checkout: Path) -> None:
        before = _refs(checkout)
        _commit(upstream, "secret.txt", "hunter2\n")

        with patch(
            "gerrit_clone.refresh_checkout_stage.apply_content_filters", _failing
        ):
            result = _worker(_spec(checkout)).refresh_repository(checkout)

        assert result.status == RefreshStatus.FAILED
        assert "left as it was" in (result.error_message or "")
        assert _refs(checkout) == before
        assert "secret.txt" not in _history(checkout)
        assert _stages_left(checkout) == []

    def test_a_stash_it_made_is_put_back(self, upstream: Path, checkout: Path) -> None:
        (checkout / "file.txt").write_text("local edit\n")
        _commit(upstream, "secret.txt", "hunter2\n")

        with patch(
            "gerrit_clone.refresh_checkout_stage.apply_content_filters", _failing
        ):
            result = _worker(_spec(checkout), auto_stash=True).refresh_repository(
                checkout
            )

        assert result.status == RefreshStatus.FAILED
        assert (checkout / "file.txt").read_text() == "local edit\n"
        assert _git("stash", "list", cwd=checkout) == ""

    def test_a_stash_is_put_back_when_only_cleaning_up_fails(
        self, upstream: Path, checkout: Path
    ) -> None:
        """Brought up to date, the refresh still fails, and keeps no stash."""
        _commit(upstream, "notes.txt", "n\n")
        _git("pull", "-q", cwd=checkout)
        (checkout / "notes.txt").write_text("local edit\n")
        _commit(upstream, "file.txt", "two\n")

        with patch.object(RefreshWorker, "_remove_stage", return_value=False):
            result = _worker(_spec(checkout), auto_stash=True).refresh_repository(
                checkout
            )

        assert result.status == RefreshStatus.FAILED
        assert (checkout / "file.txt").read_text() == "two\n"
        assert (checkout / "notes.txt").read_text() == "local edit\n"
        assert _git("stash", "list", cwd=checkout) == ""

    def test_a_stash_is_put_back_when_staging_raises(
        self, upstream: Path, checkout: Path
    ) -> None:
        (checkout / "file.txt").write_text("local edit\n")
        _commit(upstream, "file.txt", "two\n")

        with patch(
            "gerrit_clone.refresh_checkout_stage.tempfile.mkdtemp",
            side_effect=OSError("no space left"),
        ):
            result = _worker(_spec(checkout), auto_stash=True).refresh_repository(
                checkout
            )

        assert result.status == RefreshStatus.FAILED
        assert "no space left" in (result.error_message or "")
        assert (checkout / "file.txt").read_text() == "local edit\n"
        assert _git("stash", "list", cwd=checkout) == ""

    def test_when_a_pruned_ref_cannot_be_deleted(
        self, upstream: Path, checkout: Path
    ) -> None:
        """Kept, it would hold history the copy pruned, unfiltered."""
        _git("branch", "-q", "doomed", cwd=upstream)
        _git("fetch", "-q", cwd=checkout)
        _git("branch", "-q", "-D", "doomed", cwd=upstream)
        _commit(upstream, "file.txt", "two\n")
        lock = checkout / ".git" / "refs" / "remotes" / "origin" / "doomed.lock"
        lock.parent.mkdir(parents=True, exist_ok=True)
        lock.write_text("")
        before = _refs(checkout)

        result = _worker(_spec(checkout)).refresh_repository(checkout)

        assert result.status == RefreshStatus.FAILED
        assert "refs/remotes/origin/doomed" in (result.error_message or "")
        assert _refs(checkout) == before

    def test_when_publishing_fails_nothing_is_pruned(
        self, upstream: Path, checkout: Path
    ) -> None:
        """Pruning and publishing are one transaction."""
        _git("branch", "-q", "doomed", cwd=upstream)
        _git("fetch", "-q", cwd=checkout)
        _git("branch", "-q", "-D", "doomed", cwd=upstream)
        _commit(upstream, "file.txt", "two\n")
        lock = checkout / ".git" / "refs" / "remotes" / "origin" / "main.lock"
        lock.parent.mkdir(parents=True, exist_ok=True)
        lock.write_text("")
        before = _refs(checkout)

        result = _worker(_spec(checkout)).refresh_repository(checkout)

        assert result.status == RefreshStatus.FAILED
        assert _refs(checkout) == before

    def test_a_fetch_only_refresh_still_checks_it_can_follow(
        self, upstream: Path, checkout: Path
    ) -> None:
        """Recorded as filtered, its branch would still hold what they remove."""
        _commit(upstream, "secret.txt", "hunter2\n")
        _git("pull", "-q", cwd=checkout)
        before = _refs(checkout)
        _commit(upstream, "file.txt", "two\n")

        result = _worker(_spec(checkout), fetch_only=True).refresh_repository(checkout)

        assert result.status == RefreshStatus.SKIPPED
        assert "cannot follow" in (result.error_message or "")
        assert _refs(checkout) == before

    def test_when_no_remote_fetches_a_branch(
        self, checkout: Path, tmp_path: Path
    ) -> None:
        """Fetching would update FETCH_HEAD alone, and take nothing."""
        _git("config", "--unset-all", "remote.origin.fetch", cwd=checkout)
        result = RefreshResult(
            path=checkout,
            project_name="proj",
            status=RefreshStatus.REFRESHING,
            started_at=datetime.now(UTC),
        )

        outcome = _worker(_spec(checkout))._refresh_checkout_stage(
            checkout, tmp_path / "stage.git", result
        )

        assert outcome.name == "FAILED"
        assert "fetch refspec" in (result.error_message or "")

    def test_when_another_refresh_published_first(
        self, upstream: Path, checkout: Path
    ) -> None:
        """Its newer refs stay: this run's copy was made before them."""
        _commit(upstream, "file.txt", "two\n")
        real = content_stage.publishing

        @contextmanager
        def overtaken(*args: Any) -> Generator[str | None, None, None]:
            _commit(upstream, "file.txt", "three\n")
            _git("fetch", "-q", cwd=checkout)
            with real(*args) as stale:
                yield stale

        with patch.object(refresh_checkout_stage, "publishing", overtaken):
            result = _worker(_spec(checkout)).refresh_repository(checkout)
        newer = _git("rev-parse", "origin/main", cwd=checkout)

        assert result.status == RefreshStatus.FAILED
        assert result.error_message == OVERTAKEN_REFUSAL
        assert newer == _git("rev-parse", "main", cwd=upstream)

    def test_when_another_refresh_moved_a_ref_it_does_not_follow(
        self, upstream: Path, checkout: Path
    ) -> None:
        """Every ref publishing replaces is checked, not only its upstream."""
        _git("branch", "-q", "other", cwd=upstream)
        _git("fetch", "-q", cwd=checkout)
        _commit(upstream, "file.txt", "two\n")
        real = content_stage.publishing

        @contextmanager
        def overtaken(*args: Any) -> Generator[str | None, None, None]:
            _git("checkout", "-q", "other", cwd=upstream)
            _commit(upstream, "notes.txt", "n\n")
            _git("checkout", "-q", "main", cwd=upstream)
            _git(
                "fetch",
                "-q",
                "origin",
                "+refs/heads/other:refs/remotes/origin/other",
                cwd=checkout,
            )
            with real(*args) as stale:
                yield stale

        with patch.object(refresh_checkout_stage, "publishing", overtaken):
            result = _worker(_spec(checkout)).refresh_repository(checkout)

        assert result.status == RefreshStatus.FAILED
        assert result.error_message == OVERTAKEN_REFUSAL
        assert _git("rev-parse", "origin/other", cwd=checkout) == _git(
            "rev-parse", "other", cwd=upstream
        )

    def test_when_its_upstream_branch_is_gone(
        self, upstream: Path, checkout: Path
    ) -> None:
        """Pruned and published, the branch would have nothing to follow."""
        _git("branch", "-q", "feature", cwd=upstream)
        _git("fetch", "-q", cwd=checkout)
        _git("checkout", "-q", "-b", "feature", "origin/feature", cwd=checkout)
        (checkout / "file.txt").write_text("local edit\n")
        _git("branch", "-q", "-D", "feature", cwd=upstream)
        _commit(upstream, "file.txt", "two\n")
        before = _refs(checkout)

        result = _worker(_spec(checkout), auto_stash=True).refresh_repository(checkout)

        assert result.status == RefreshStatus.SKIPPED
        assert result.error_message == UPSTREAM_GONE_REFUSAL
        assert _refs(checkout) == before
        assert (checkout / "file.txt").read_text() == "local edit\n"

    def test_fetching_only_prunes_an_upstream_branch_that_is_gone(
        self, upstream: Path, checkout: Path
    ) -> None:
        _git("branch", "-q", "feature", cwd=upstream)
        _git("fetch", "-q", cwd=checkout)
        _git("checkout", "-q", "-b", "feature", "origin/feature", cwd=checkout)
        _git("branch", "-q", "-D", "feature", cwd=upstream)

        result = _worker(_spec(checkout), fetch_only=True).refresh_repository(checkout)

        assert result.success, result.error_message
        assert "refs/remotes/origin/feature" not in _refs(checkout)

    def test_when_only_the_worktree_fallback_could_filter_it(
        self, upstream: Path, checkout: Path
    ) -> None:
        """Its removal commit could never be extended by the next one."""
        _commit(upstream, "secret.txt", "hunter2\n")
        _commit(upstream, "file.txt", "two\n")
        before = _refs(checkout)

        with (
            patch(
                "gerrit_clone.content_filter._check_git_filter_repo", return_value=False
            ),
            patch(
                "gerrit_clone.refresh_checkout_stage._check_git_filter_repo",
                return_value=False,
            ),
        ):
            result = _worker(_spec(checkout)).refresh_repository(checkout)

        assert result.status == RefreshStatus.SKIPPED
        assert result.error_message == NO_FILTER_REPO_REFUSAL
        assert _refs(checkout) == before

    def test_when_pushing_cannot_be_blocked_nothing_is_recorded(
        self, upstream: Path, checkout: Path
    ) -> None:
        """Recording and blocking are both or neither, before publishing."""
        _commit(upstream, "secret.txt", "hunter2\n")
        _commit(upstream, "file.txt", "two\n")
        before = _refs(checkout)
        config = _git("config", "--list", "--local", cwd=checkout)

        def halfway(repo: Path) -> bool:
            _git("config", "remote.origin.pushurl", "nowhere", cwd=repo)
            return False

        with patch("gerrit_clone.content_policy.block_pushes", side_effect=halfway):
            result = _worker(_spec(checkout)).refresh_repository(checkout)

        assert result.status == RefreshStatus.FAILED
        assert "block pushing" in (result.error_message or "")
        assert _refs(checkout) == before
        assert _git("config", "--list", "--local", cwd=checkout) == config

    def test_when_a_remote_fetches_where_no_copy_can(
        self, upstream: Path, checkout: Path
    ) -> None:
        _git(
            "config",
            "--add",
            "remote.origin.fetch",
            "+refs/*:refs/other/*",
            cwd=checkout,
        )
        before = _refs(checkout)
        _commit(upstream, "file.txt", "two\n")

        result = _worker(_spec(checkout)).refresh_repository(checkout)

        assert result.status == RefreshStatus.FAILED
        assert "refs/tags/" in (result.error_message or "")
        assert _refs(checkout) == before
        assert _stages_left(checkout) == []

    def test_when_the_filters_rewrite_history_it_holds(
        self, upstream: Path, checkout: Path
    ) -> None:
        """Cloned before the filters, it holds what they remove: filtered
        history no longer extends it."""
        _commit(upstream, "secret.txt", "hunter2\n")
        _git("pull", "-q", cwd=checkout)
        before = _refs(checkout)
        _commit(upstream, "file.txt", "two\n")

        result = _worker(_spec(checkout)).refresh_repository(checkout)

        assert result.status == RefreshStatus.SKIPPED
        assert "cannot follow" in (result.error_message or "")
        assert _refs(checkout) == before


class TestRewrittenThroughACopy:
    """A working copy a staged refresh rewrote keeps its layout."""

    def test_tracking_a_local_branch_it_is_refused(
        self, upstream: Path, checkout: Path
    ) -> None:
        """It would take nothing the copy fetched, yet report success."""
        _commit(upstream, "secret.txt", "hunter2\n")
        _commit(upstream, "file.txt", "two\n")
        first = _worker(_spec(checkout)).refresh_repository(checkout)
        assert first.status == RefreshStatus.SUCCESS, first.error_message
        _git("branch", "-q", "other", cwd=checkout)
        _git("branch", "-q", "--set-upstream-to=other", "main", cwd=checkout)
        _commit(upstream, "file.txt", "three\n")

        result = _worker(_spec(checkout)).refresh_repository(checkout)

        assert result.status == RefreshStatus.SKIPPED
        assert result.error_message == FILTERED_WORKING_COPY_REFUSAL
        assert (checkout / "file.txt").read_text() == "two\n"

    def test_without_the_filters_it_is_refused(
        self, upstream: Path, checkout: Path
    ) -> None:
        _commit(upstream, "secret.txt", "hunter2\n")
        _commit(upstream, "file.txt", "two\n")
        first = _worker(_spec(checkout)).refresh_repository(checkout)
        assert first.status == RefreshStatus.SUCCESS, first.error_message
        before = _refs(checkout)
        _commit(upstream, "file.txt", "three\n")

        result = _worker(None).refresh_repository(checkout)

        assert result.status == RefreshStatus.SKIPPED
        assert result.error_message == FILTERED_REFRESH_REFUSAL
        assert _refs(checkout) == before


def _refresh(tree: Path, *options: str) -> Any:
    return CliRunner().invoke(
        app, ["refresh", "--output-path", str(tree), "--all-repos", *options]
    )


class TestTheCommand:
    def test_a_filter_matching_nothing_leaves_it_refreshable(
        self, upstream: Path, checkout: Path
    ) -> None:
        """It used to rewrite the checkout in place, and every later
        refresh then skipped it."""
        for content in ("two\n", "three\n"):
            _commit(upstream, "file.txt", content)
            result = _refresh(checkout.parent, "--remove-files", "nomatch.txt")
            assert result.exit_code == 0, result.output
            assert (checkout / "file.txt").read_text() == content

        assert _git("config", "--get-regexp", "^remote\\.", cwd=checkout)
        assert _git("rev-parse", "--abbrev-ref", "main@{upstream}", cwd=checkout) == (
            "origin/main"
        )


class TestDryRun:
    def test_it_predicts_the_refusal_of_a_shallow_checkout(
        self, tmp_path: Path, upstream: Path
    ) -> None:
        """History filters cannot be trusted on truncated history."""
        _commit(upstream, "file.txt", "two\n")
        shallow = tmp_path / "shallow" / "proj"
        _git("clone", "-q", "--depth", "1", upstream.as_uri(), str(shallow))
        filters = ContentFilterSpec(None, None, True, shallow.parent)

        [predicted] = (
            RefreshManager(
                dry_run=True, filter_gerrit_only=False, content_filters=filters
            )
            .refresh_repositories(shallow.parent)
            .results
        )
        actual = _worker(filters).refresh_repository(shallow)

        assert predicted.status == RefreshStatus.SKIPPED
        assert predicted.error_message == SHALLOW_HISTORY_REFUSAL
        assert actual.status == RefreshStatus.SKIPPED
        assert actual.error_message == SHALLOW_HISTORY_REFUSAL
