# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""Refreshing a repository that content filtering has rewritten.

Refreshing one brings the original history back: a mirror's
``+refs/*:refs/*`` fetch forces every upstream ref over the rewritten
one, removed files and redacted secrets included.  So such a repository
is refreshed only by a run whose filters cover the policy it was filtered
under (see :mod:`gerrit_clone.content_policy`), and then only as a staged
transaction: the mirror is copied, the copy is fetched and filtered, and
its refs reach the mirror only once all of that has succeeded.  A fetch
or filter that fails leaves the mirror exactly as it was.

A working copy is refreshed through a filtered copy too, once it has
been prepared as any working copy is: see
:mod:`gerrit_clone.refresh_checkout_stage`.  Here it meets the same
refusals as a mirror, and one more: filtered where it stands, it has
lost the remote-tracking branch a refresh follows, and is refused.
"""

from __future__ import annotations

import shutil
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING

from gerrit_clone.content_filter import apply_content_filters, is_shallow_repository
from gerrit_clone.content_intent import IntentError
from gerrit_clone.content_journal import refs_digest
from gerrit_clone.content_policy import (
    PolicyRecordError,
    mark_recorded,
    recorded_policy,
    recorded_until_published,
)
from gerrit_clone.content_spec import missing_tokens_refusal
from gerrit_clone.content_stage import filter_repository, publishing
from gerrit_clone.logging import get_logger
from gerrit_clone.models import RefreshStatus
from gerrit_clone.refresh_force import ForceModeMixin
from gerrit_clone.refresh_git_env import run_git

if TYPE_CHECKING:
    from datetime import datetime

    from gerrit_clone.content_policy import FilterPolicy
    from gerrit_clone.content_spec import ContentFilterSpec
    from gerrit_clone.models import RefreshResult

logger = get_logger(__name__)

#: Why a content-filtered repository is refused a refresh without filters.
FILTERED_REFRESH_REFUSAL = (
    "Content filtering rewrote this repository; refreshing it without the "
    "same filters would bring the filtered content back. Refresh with "
    "--remove-files, --git-filter or --redact-secrets as before"
)

#: Why a filtered working copy with nothing to follow is refused a refresh.
FILTERED_WORKING_COPY_REFUSAL = (
    "Content filtering rewrote this working copy, and its branch has no "
    "remote-tracking branch for a refresh to follow -- filtering a working "
    "copy in place removes them -- so it cannot be refreshed"
)

#: Why a staged copy is not published over a mirror another run refreshed.
OVERTAKEN_REFUSAL = (
    "Another run refreshed the repository while this one staged its copy, "
    "so the copy was not published over it. Run again"
)

#: Why a repository an earlier release filtered is refused a refresh.
EARLIER_RELEASE_REFUSAL = (
    "An earlier gerrit-clone release content-filtered this repository "
    "without recording its filters, so no refresh can be shown to keep "
    "them; refreshing it could bring the filtered content back. Re-clone "
    "it, with the filters, to update it"
)


#: Why a shallow content-filtered mirror is refused history filtering.
SHALLOW_HISTORY_REFUSAL = (
    "--git-filter and --redact-secrets cannot be trusted on a shallow "
    "repository: truncated history can hide older secrets. Re-clone it "
    "without --depth"
)


class FilteredRefreshMixin(ForceModeMixin):
    """Refusal, or staged refresh, of content-filtered repositories."""

    # Supplied by RefreshWorker.__init__; declared here because this layer
    # reads them.
    content_filters: ContentFilterSpec | None
    timeout: int

    def _filtered_refusal(
        self, repo_path: Path, recorded: FilterPolicy, *, bare: bool
    ) -> str | None:
        """Why *repo_path*, filtered under *recorded*, may not be refreshed.

        Args:
            repo_path: Repository path
            recorded: Policy it was last filtered under
            bare: Whether it is a bare repository

        Returns:
            The reason, or ``None`` if a staged refresh may go ahead.
        """
        if recorded.earlier_release:
            return EARLIER_RELEASE_REFUSAL
        if not bare and not self._follows_upstream(repo_path):
            return FILTERED_WORKING_COPY_REFUSAL
        if self.content_filters is None:
            return FILTERED_REFRESH_REFUSAL
        requested = self.content_filters.policy_for(repo_path)
        if requested.covers(recorded):
            return None
        return (
            f"Content filtering rewrote this repository under filters this "
            f"run would not re-apply ({requested.describe_missing(recorded)}); "
            f"refreshing it would bring the filtered content back"
        )

    def _refresh_filtered(
        self,
        repo_path: Path,
        result: RefreshResult,
        recorded: FilterPolicy,
        *,
        bare: bool,
        started_at: datetime,
    ) -> RefreshResult:
        """Refuse a content-filtered repository, or refresh it staged.

        Args:
            repo_path: Repository path
            result: Result object to update
            recorded: Policy it was last filtered under
            bare: Whether it is a bare repository
            started_at: Timestamp the refresh began, for completion metadata

        Returns:
            The completed *result*.
        """
        refusal = self._staged_refusal(repo_path, recorded, bare=bare)
        if refusal is not None:
            result.status = RefreshStatus.SKIPPED
            result.error_message = refusal
            self._stamp_completion(result, started_at)
            logger.warning(f"⚠️ {result.project_name}: {refusal}")
            return result

        result.status = RefreshStatus.REFRESHING
        success = self._staged_refresh(repo_path, result)
        if not success:
            result.status = RefreshStatus.FAILED
            result.error_message = result.error_message or "Staged refresh failed"
        else:
            result.was_behind = result.commits_pulled > 0
            result.status = (
                RefreshStatus.SUCCESS if result.was_behind else RefreshStatus.UP_TO_DATE
            )
        self._stamp_completion(result, started_at)
        return result

    def _staged_refusal(
        self, repo_path: Path, recorded: FilterPolicy, *, bare: bool
    ) -> str | None:
        """Every reason a content-filtered *repo_path* is not refreshed.

        One method for the refresh and the dry run alike, so the dry
        run's prediction cannot drift from what the refresh does.

        Returns:
            The first reason found, or ``None`` if a staged refresh may go
            ahead.
        """
        refusal = None
        if not recorded.empty:
            refusal = self._filtered_refusal(repo_path, recorded, bare=bare)
        if refusal is None:
            # After the refusals no option can lift: passing the tokens
            # would only meet them next.
            refusal = self._missing_tokens_refusal(repo_path)
        if refusal is None and bare:
            refusal = self._bare_refresh_obstacle(repo_path)
        if refusal is None and self._shallow_history_filtered(repo_path):
            refusal = SHALLOW_HISTORY_REFUSAL
        return refusal

    def _needs_staging(
        self, repo_path: Path, recorded: FilterPolicy, *, bare: bool
    ) -> bool:
        """Whether *repo_path* is refreshed through a staged copy, or refused.

        One the filters rewrote always is.  So is a mirror this run
        filters, though nothing rewrote it yet: fetched in place, it would
        hold whatever arrived until the filters ran, and keep it if they
        then failed.  One lacking tokens its intent replaces is refused.
        A working copy is staged only after it is prepared (see
        :mod:`gerrit_clone.refresh_checkout_stage`), so here it only meets
        its refusals.
        """
        if not bare:
            # Its refusals, the dry run's prediction included, whatever
            # filtered it before and whatever this run filters.
            return self._staged_refusal(repo_path, recorded, bare=False) is not None
        if not recorded.empty or self._missing_tokens_refusal(repo_path):
            return True
        spec = self.content_filters
        if spec is None:
            return False
        return not spec.filters_for(spec.project_name(repo_path)).empty

    @staticmethod
    def _follows_upstream(repo_path: Path) -> bool:
        """Whether working copy *repo_path*'s branch has a remote-tracking ref.

        Filtered where it stands, a working copy loses them: ``git
        filter-repo`` folds ``origin``'s into local branches.  One a staged
        refresh filtered keeps them, and can follow upstream again.
        """
        found = run_git(
            ["git", "rev-parse", "--symbolic-full-name", "@{upstream}"],
            repo_path,
            timeout=10,
        )
        # Tracking a local branch, it would take nothing the copy fetched.
        return found.returncode == 0 and found.stdout.startswith("refs/remotes/")

    def _missing_tokens_refusal(self, repo_path: Path) -> str | None:
        """Why *repo_path* is refused for lacking tokens its intent replaces.

        Checked before any fetch: a token the tree's intent replaces but
        the run does not supply cannot be replaced in what arrives.
        """
        spec = self.content_filters
        missing = spec.missing_tokens(repo_path) if spec is not None else 0
        return missing_tokens_refusal(missing) if missing else None

    def _shallow_history_filtered(self, repo_path: Path) -> bool:
        """Whether history filters would run on a shallow *repo_path*.

        ``--git-filter`` and ``--redact-secrets`` scan the whole history,
        and a shallow repository hides part of it, so they would mark it
        fully filtered having seen only some of it -- the same reason the
        command's own filter stage refuses them there.
        """
        spec = self.content_filters
        if spec is None:
            return False
        history = spec.filters_for(spec.project_name(repo_path)).history
        return history and is_shallow_repository(repo_path)

    def _staged_refresh(self, repo_path: Path, result: RefreshResult) -> bool:
        """Fetch and filter a copy of *repo_path*, then publish its refs.

        The copy sits beside the mirror, so a local clone hard-links its
        objects rather than copying them, under a hidden name discovery
        never reports.

        Returns:
            True if the mirror now holds the refreshed, filtered refs, and
            the copy has gone.
        """
        stage_parent = Path(
            tempfile.mkdtemp(prefix=".gerrit-clone-stage-", dir=repo_path.parent)
        )
        try:
            refreshed = self._refresh_stage(
                repo_path, stage_parent / "repo.git", result
            )
        finally:
            removed = self._remove_stage(stage_parent, result)
        return refreshed and removed

    def _refresh_stage(
        self, repo_path: Path, stage: Path, result: RefreshResult
    ) -> bool:
        """Stage, fetch and filter *repo_path* at *stage*; publish if all worked."""
        spec = self.content_filters
        assert spec is not None  # Checked by _filtered_refusal.
        # What the copy starts from: published over refs another run moved
        # since, its forced fetch would roll them back.
        base = refs_digest(repo_path)
        if not self._prepare_stage(repo_path, stage, result):
            return False
        if not self._execute_adaptive_refresh(stage, result, bare=True):
            return False
        # Looked up here, so a test patching this module's name sees it.
        error = filter_repository(
            spec,
            stage,
            spec.project_name(repo_path),
            self.timeout,
            apply=apply_content_filters,
        )
        if error is not None:
            result.error_message = (
                f"Re-filtering the refreshed copy failed, so the "
                f"repository was left as it was: {error}"
            )
            return False
        # The copy records the run's filters if they rewrote anything in
        # it, and nothing if they did not.  They are added to the mirror's
        # record, which only ever grows.
        try:
            with publishing(spec, spec.project_name(repo_path)) as stale:
                overtaken = base is None or refs_digest(repo_path) != base
                refusal = stale or (OVERTAKEN_REFUSAL if overtaken else None)
                if refusal is not None:
                    result.error_message = refusal
                    return False
                return self._publish_stage(
                    repo_path, stage, result, recorded_policy(stage)
                )
        except IntentError as exc:
            result.error_message = f"Could not publish the refreshed refs: {exc}"
            return False

    @staticmethod
    def _remove_stage(stage_parent: Path, result: RefreshResult) -> bool:
        """Delete the staging copy, or fail the refresh saying where it is.

        Never silently: after a failed re-filter the copy holds the
        freshly fetched history unfiltered, the very content the mirror
        exists to exclude.
        """
        try:
            shutil.rmtree(stage_parent)
        except OSError as exc:
            if not stage_parent.exists():
                return True
            message = (
                f"Could not remove the staging copy {stage_parent}, which may "
                f"hold unfiltered history; delete it by hand: {exc}"
            )
            logger.error(message)
            result.error_message = "; ".join(
                part for part in (result.error_message, message) if part
            )
            return False
        return True

    def _prepare_stage(
        self, repo_path: Path, stage: Path, result: RefreshResult
    ) -> bool:
        """Copy *repo_path* to *stage*, fetching from where it fetches."""
        cloned = run_git(
            ["git", "clone", "--mirror", "--quiet", str(repo_path), str(stage)],
            repo_path.parent,
            timeout=self.timeout,
        )
        if cloned.returncode != 0:
            result.error_message = f"Could not stage a copy: {cloned.stderr.strip()}"
            return False
        # The mirror's record is known; the copy's tips are not evidence
        # of an earlier release, whatever commits they carry.
        if not mark_recorded(stage):
            result.error_message = "Could not stage a copy: its config was refused"
            return False
        remotes = run_git(
            ["git", "config", "--get-regexp", r"^remote\."], repo_path, timeout=10
        )
        if remotes.returncode != 0:
            result.error_message = (
                f"Could not read the remotes to stage a copy: {remotes.stderr.strip()}"
            )
            return False
        # The copy's origin is the mirror itself; it fetches from the
        # mirror's own remotes instead.  Left in place, the mirror's URL
        # would stay first under origin, and the fetch would read the
        # mirror rather than upstream.
        detached = run_git(["git", "remote", "remove", "origin"], stage, timeout=10)
        if detached.returncode != 0:
            result.error_message = (
                f"Could not detach the staged copy from the mirror: "
                f"{detached.stderr.strip()}"
            )
            return False
        for line in remotes.stdout.splitlines():
            key, _, value = line.partition(" ")
            added = run_git(["git", "config", "--add", key, value], stage, timeout=10)
            if added.returncode != 0:
                result.error_message = f"Could not stage a copy: {added.stderr.strip()}"
                return False
        return True

    def _publish_stage(
        self,
        repo_path: Path,
        stage: Path,
        result: RefreshResult,
        policy: FilterPolicy,
    ) -> bool:
        """Move the filtered copy's refs into *repo_path*, all or none.

        The policy is recorded and pushing blocked first: refs published
        without them would be filtered content nothing knows to protect.
        """
        try:
            # Undone unless the refs are published too.
            with recorded_until_published(repo_path, policy) as completed:
                published = run_git(
                    [
                        "git",
                        "fetch",
                        "--atomic",
                        "--prune",
                        str(stage),
                        "+refs/*:refs/*",
                    ],
                    repo_path,
                    timeout=self.timeout,
                )
                if published.returncode != 0:
                    result.error_message = (
                        f"Could not publish the refreshed refs: "
                        f"{published.stderr.strip()}"
                    )
                    return False
                completed()
        except PolicyRecordError as exc:
            result.error_message = f"{exc}, so the repository was left as it was"
            return False
        result.commits_pulled = self._count_fetched_commits(published.stderr)
        result.content_filtered = True
        return True
