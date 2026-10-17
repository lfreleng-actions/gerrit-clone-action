# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""Refreshing a working copy the run filters, through a filtered copy.

A working copy (``--no-mirror``) used to pull first and be filtered
afterwards, where it stood.  A filter that failed left what had just
arrived, unfiltered, in the checkout.  One that ran at all rewrote the
checkout itself -- ``git filter-repo`` folds remote-tracking refs into
local branches -- and recorded it as filtered, so every later refresh
skipped it.

Instead, as for a mirror (see :mod:`gerrit_clone.refresh_filtered`):

1. A bare copy is made inside the checkout's git directory, where
   ``git status`` never sees it, holding the checkout's remote-tracking
   refs as branches -- ``refs/remotes/X`` as ``refs/heads/X`` -- and its
   tags.  Both filter methods rewrite branches, and ``filter-repo`` has
   no ``origin`` refs to fold.
2. The copy fetches from the checkout's own remotes, and is filtered.
3. Under the tree's intent lock, so no other refresh publishes in
   between: if the filtered upstream does not extend what the checkout
   holds, or its branch's upstream is gone, it is refused.
4. Otherwise any filters that rewrote the copy are recorded in the
   checkout, and pushing from it blocked; its remote-tracking refs and
   tags then take the copy's, in one atomic fetch.
5. The current branch fast-forwards, or rebases, onto its upstream, as a
   pull would, with nothing fetched again.

Anything failing before step 4 leaves the checkout as it was, and puts
back any stash the refresh made.  Filtering upstream content the same
way gives the same commits each time, so a checkout an earlier staged
refresh rewrote follows it the same way.  One filtered in place has
lost its remote-tracking refs, and is refused.
"""

from __future__ import annotations

import tempfile
from contextlib import nullcontext
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING, TypeVar

from gerrit_clone.content_filter import _check_git_filter_repo, apply_content_filters
from gerrit_clone.content_intent import IntentError
from gerrit_clone.content_journal import refs_digest
from gerrit_clone.content_policy import (
    PolicyRecordError,
    recorded_policy,
    recorded_until_published,
    stored_policy,
)
from gerrit_clone.content_stage import filter_repository, publishing
from gerrit_clone.logging import get_logger
from gerrit_clone.models import RefreshStatus
from gerrit_clone.refresh_checkout_refs import (
    PUBLISHED,
    TRACKING,
    as_upstream_copy,
    configured_upstream,
    copy_tags,
    publish_command,
)
from gerrit_clone.refresh_filtered import OVERTAKEN_REFUSAL, FilteredRefreshMixin
from gerrit_clone.refresh_git_env import run_git
from gerrit_clone.subprocess_tracking import ProcessAbandonedError

if TYPE_CHECKING:
    from collections.abc import Callable

    from gerrit_clone.models import RefreshResult

logger = get_logger(__name__)

_T = TypeVar("_T")

#: Why a working copy is refused a rewrite only the worktree fallback can do.
NO_FILTER_REPO_REFUSAL = (
    "Without git filter-repo, content filtering adds a removal commit that "
    "no later refresh could follow, so the working copy was left as it "
    "was. Install git-filter-repo to refresh it"
)

#: Why a working copy is refused a refresh when its branch's upstream went.
UPSTREAM_GONE_REFUSAL = (
    "The branch this working copy's branch tracks no longer exists upstream, "
    "so there is nothing for it to follow; it was left as it was"
)

#: Why a working copy is refused upstream history its filters rewrote.
CANNOT_FOLLOW_REFUSAL = (
    "Filtered, upstream history no longer extends what this working copy "
    "holds, so it cannot follow it, and was left as it was. Re-clone it, "
    "with the filters, to update it"
)


class _Staged(Enum):
    """How a staged refresh of a working copy ended."""

    #: Published, and the current branch brought up to date.
    DONE = "done"
    #: Published, but the branch could not be brought up to date.
    NOT_INTEGRATED = "not integrated"
    #: Nothing reached the checkout.
    FAILED = "failed"
    #: Nothing reached the checkout, which cannot follow upstream.
    REFUSED = "refused"


class StagedCheckoutMixin(FilteredRefreshMixin):
    """Staged refresh of working copies the run filters."""

    def _refresh_in_place(
        self, repo_path: Path, result: RefreshResult, *, bare: bool
    ) -> bool:
        """Refresh a prepared *repo_path*: staged if a working copy the run filters."""
        spec = self.content_filters
        if bare or spec is None or spec.filters_for(spec.project_name(repo_path)).empty:
            return self._execute_adaptive_refresh(repo_path, result, bare=bare)
        git_dir = self._checkout_git_dir(repo_path, result)
        if git_dir is None:
            self._restore_stash(repo_path, result)
            return False
        try:
            outcome, removed = self._in_checkout_stage(
                git_dir,
                result,
                lambda stage: self._refresh_checkout_stage(repo_path, stage, result),
            )
        except ProcessAbandonedError:
            raise  # A stash an abandoned refresh made stays for git stash list.
        except Exception:
            if not result.content_filtered:  # Nothing published yet.
                self._restore_stash(repo_path, result)
            raise
        # Brought up to date but for the copy's removal, the refresh still
        # fails, and the outcome step never restores a failure's stash.
        if outcome in (_Staged.FAILED, _Staged.REFUSED) or (
            outcome is _Staged.DONE and not removed
        ):
            self._restore_stash(repo_path, result)
        if outcome is _Staged.REFUSED:
            result.status = RefreshStatus.SKIPPED
            logger.warning(f"⚠️ {result.project_name}: {result.error_message}")
        return outcome is _Staged.DONE and removed

    @staticmethod
    def _checkout_git_dir(repo_path: Path, result: RefreshResult) -> str | None:
        """The absolute git directory of *repo_path*, where its copy is staged.

        ``None``, with ``result.error_message`` saying why, if git cannot
        find it.
        """
        git_dir = run_git(
            ["git", "rev-parse", "--absolute-git-dir"], repo_path, timeout=10
        )
        if git_dir.returncode != 0:
            result.error_message = f"Could not find its git directory: {git_dir.stderr}"
            return None
        return git_dir.stdout.strip()

    def _in_checkout_stage(
        self,
        git_dir: str,
        result: RefreshResult,
        work: Callable[[Path], _T],
    ) -> tuple[_T, bool]:
        """Run *work* on a copy's path inside *git_dir*, then remove the copy.

        The copy sits in a temporary directory under *git_dir*, where
        ``git status`` never sees it, and its removal is tried however
        *work* ends: an exception *work* raises propagates afterwards.  A
        copy that cannot be removed is reported in ``result.error_message``,
        naming its path (see :meth:`_remove_stage`).

        Returns:
            What *work* returned, and whether the copy has gone.
        """
        stage_parent = Path(tempfile.mkdtemp(prefix="gerrit-clone-stage-", dir=git_dir))
        try:
            done = work(stage_parent / "repo.git")
        finally:
            removed = self._remove_stage(stage_parent, result)
        return done, removed

    def _refresh_checkout_stage(
        self, repo_path: Path, stage: Path, result: RefreshResult
    ) -> _Staged:
        """Steps 1 to 5 of the module docstring, with *stage* as the copy."""
        # What publishing replaces, as it stood when the copy was made.
        base = refs_digest(repo_path, *PUBLISHED)
        stopped = self._stage_and_filter(repo_path, stage, result)
        if stopped is None:
            stopped = self._record_and_publish(repo_path, stage, result, base)
        if stopped is not None:
            return stopped
        result.content_filtered = True
        if self._bring_branch_up_to_date(repo_path, result):
            return _Staged.DONE
        return _Staged.NOT_INTEGRATED

    def _stage_and_filter(
        self,
        repo_path: Path,
        stage: Path,
        result: RefreshResult,
        *,
        fetch: bool = True,
    ) -> _Staged | None:
        """Steps 1 and 2: ``None`` if the copy fetched and was filtered.

        Without *fetch*, the copy is filtered holding the checkout's
        remote-tracking refs as branches and its tags, which would else
        escape the filters: for a checkout just cloned, holding upstream.
        """
        spec = self.content_filters
        assert spec is not None  # Checked by the caller.
        if not (
            self._prepare_stage(repo_path, stage, result)
            and self._as_upstream_copy(repo_path, stage, result, tags=not fetch)
            and (not fetch or self._execute_adaptive_refresh(stage, result, bare=True))
        ):
            return _Staged.FAILED
        # Looked up here, so a test patching this module's name sees it.
        reason = filter_repository(
            spec,
            stage,
            spec.project_name(repo_path),
            self.timeout,
            apply=apply_content_filters,
        )
        if reason is not None:
            result.error_message = (
                f"Filtering what arrived failed, so the working copy was "
                f"left as it was: {reason}"
                if fetch
                else f"Filtering the working copy failed: {reason}"
            )
            return _Staged.FAILED
        # The worktree fallback's removal commit sits on upstream's tip, not
        # on the last one: the next refresh's could never extend this one.
        if not _check_git_filter_repo() and not stored_policy(stage).empty:
            result.error_message = NO_FILTER_REPO_REFUSAL
            return _Staged.REFUSED
        return None

    def _record_and_publish(
        self,
        repo_path: Path,
        stage: Path,
        result: RefreshResult,
        base: str | None,
        *,
        follow: bool = True,
    ) -> _Staged | None:
        """Steps 3 and 4, under the tree's intent lock (see :func:`publishing`).

        Checked under the lock, so another refresh of the checkout cannot
        publish in between: the refs publishing replaces must still be
        as *base* saw them, or a copy staged before another run's would
        roll any of them back.  ``None`` once published.

        Without *follow*, the checkout is not refused when the filtered
        upstream does not extend its branch, or its branch's upstream is
        gone: for a checkout that was just cloned, whose branch holds the
        unfiltered history.  Every other check still runs, and any filters
        that rewrote the copy are still recorded, with pushing blocked.
        Its tags are then the copy's exactly (see :meth:`_publish_to_checkout`).
        """
        spec = self.content_filters
        assert spec is not None  # Checked by the caller.
        try:
            policy = recorded_policy(stage)
            with publishing(spec, spec.project_name(repo_path)) as stale:
                moved = base is None or refs_digest(repo_path, *PUBLISHED) != base
                reason = stale or (OVERTAKEN_REFUSAL if moved else None)
                if reason is not None:
                    result.error_message = reason
                    return _Staged.FAILED
                # Fetch-only too: the checkout is recorded as filtered.
                refusal = self._follow_refusal(repo_path, stage) if follow else None
                if refusal is not None:
                    result.error_message = refusal
                    return _Staged.REFUSED
                # Undone unless the refs are published too.
                guard = (
                    recorded_until_published(repo_path, policy)
                    if not policy.empty
                    else nullcontext(lambda: None)
                )
                with guard as published:
                    if not self._publish_to_checkout(
                        repo_path, stage, result, prune_tags=not follow
                    ):
                        return _Staged.FAILED
                    published()
                return None
        except (IntentError, PolicyRecordError) as exc:
            result.error_message = (
                f"Could not publish the refreshed refs, so the working copy "
                f"was left as it was: {exc}"
                if follow
                else f"Could not publish the filtered refs: {exc}"
            )
            return _Staged.FAILED

    def _as_upstream_copy(
        self, repo_path: Path, stage: Path, result: RefreshResult, *, tags: bool
    ) -> bool:
        """Make *stage* stand in for upstream (see :func:`as_upstream_copy`).

        With *tags*, it then takes *repo_path*'s (see :func:`copy_tags`).
        """
        failure = as_upstream_copy(repo_path, stage, self.timeout, fetching=not tags)
        if failure is None and tags:
            failure = copy_tags(repo_path, stage, self.timeout)
        if failure is not None:
            result.error_message = failure
        return failure is None

    def _follow_refusal(self, repo_path: Path, stage: Path) -> str | None:
        """Why *repo_path* cannot follow the filtered upstream, if it cannot.

        Its upstream's old tip, or the branch itself if that ref has gone,
        must be an ancestor of the new one: an earlier filter's rewrite,
        repeated, gives the same commits, while one that now rewrites
        history the checkout already holds does not.  An upstream branch
        deleted upstream leaves nothing to follow, unless only fetching.
        """
        upstream = run_git(
            ["git", "rev-parse", "--symbolic-full-name", "@{upstream}"],
            repo_path,
            timeout=10,
        )
        ref = upstream.stdout.strip()
        if upstream.returncode != 0 or not ref.startswith(TRACKING):
            ref = configured_upstream(repo_path)
        if not ref.startswith(TRACKING):
            return None  # Tracks a local branch, as a pull would take.
        new = run_git(
            [
                "git",
                "rev-parse",
                "-q",
                "--verify",
                "refs/heads/" + ref[len(TRACKING) :],
            ],
            stage,
            timeout=10,
        )
        if new.returncode != 0:
            return None if self.fetch_only else UPSTREAM_GONE_REFUSAL
        old = run_git(
            ["git", "rev-parse", "-q", "--verify", ref], repo_path, timeout=10
        )
        if old.returncode != 0:
            old = run_git(["git", "rev-parse", "HEAD"], repo_path, timeout=10)
        ancestor = run_git(
            [
                "git",
                "merge-base",
                "--is-ancestor",
                old.stdout.strip(),
                new.stdout.strip(),
            ],
            stage,
            timeout=self.timeout,
        )
        return None if ancestor.returncode == 0 else CANNOT_FOLLOW_REFUSAL

    def _publish_to_checkout(
        self,
        repo_path: Path,
        stage: Path,
        result: RefreshResult,
        *,
        prune_tags: bool = False,
    ) -> bool:
        """Give *repo_path* the copy's refs (see :func:`publish_command`)."""
        published = run_git(
            publish_command(stage, prune=self.prune, prune_tags=prune_tags),
            repo_path,
            timeout=self.timeout,
        )
        if published.returncode != 0:
            result.error_message = (
                f"Could not publish the refreshed refs, so the working copy "
                f"was left as it was: {published.stderr.strip()}"
            )
            return False
        result.commits_pulled = self._count_fetched_commits(published.stderr)
        return True

    def _bring_branch_up_to_date(self, repo_path: Path, result: RefreshResult) -> bool:
        """Fast-forward or rebase onto the published upstream, as a pull would."""
        if self.fetch_only:
            return True
        before = run_git(["git", "rev-parse", "HEAD"], repo_path, timeout=10)
        if self.strategy == "rebase":
            command = ["git", "rebase", "@{upstream}"]
        else:
            command = ["git", "merge", "--ff-only", "@{upstream}"]
        done = run_git(command, repo_path, timeout=self.timeout)
        if done.returncode != 0:
            output = (done.stdout + done.stderr).strip()
            if "CONFLICT" in output:
                result.status = RefreshStatus.CONFLICTS
            result.error_message = f"Could not bring the branch up to date: {output}"
            return False
        moved = run_git(
            ["git", "rev-list", "--count", f"{before.stdout.strip()}..HEAD"],
            repo_path,
            timeout=30,
        )
        result.commits_pulled = (
            int(moved.stdout.strip() or 0) if moved.returncode == 0 else 0
        )
        diff = run_git(
            ["git", "diff", "--shortstat", before.stdout.strip(), "HEAD"],
            repo_path,
            timeout=30,
        )
        result.files_changed = self._count_changed_files(diff.stdout)
        return True
