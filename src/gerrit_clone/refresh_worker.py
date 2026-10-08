# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2025 Matthew Watkins <mwatkins@linuxfoundation.org>

"""Refresh worker for individual repository update operations.

This module holds the top-level refresh flow. The mechanics it orchestrates
live in a stack of focused mixins, each of which owns one responsibility:

* :mod:`gerrit_clone.refresh_git_env` — remotes, environment, tracked git launches
* :mod:`gerrit_clone.refresh_repo_state` — bare or working tree, and stashing
* :mod:`gerrit_clone.refresh_branch_repair` — default-branch and upstream repair
* :mod:`gerrit_clone.refresh_execution` — fetch/pull execution and retries
* :mod:`gerrit_clone.refresh_force` — force-mode repository repair
* :mod:`gerrit_clone.refresh_filtered` — content-filtered repositories
* :mod:`gerrit_clone.refresh_checkout_stage` — filtered working copies
* :mod:`gerrit_clone.refresh_output` — git output classification and counting

Their public names are re-exported here so ``gerrit_clone.refresh_worker``
remains the single import surface for refresh behaviour.
"""

from __future__ import annotations

import time
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from gerrit_clone.content_policy import recorded_policy
from gerrit_clone.logging import get_logger
from gerrit_clone.models import Config, RefreshResult, RefreshStatus, RetryPolicy
from gerrit_clone.refresh_checkout_stage import (
    CANNOT_FOLLOW_REFUSAL,
    StagedCheckoutMixin,
)
from gerrit_clone.refresh_filtered import (
    EARLIER_RELEASE_REFUSAL,
    FILTERED_REFRESH_REFUSAL,
    FILTERED_WORKING_COPY_REFUSAL,
)
from gerrit_clone.refresh_git_env import SSH_HANDSHAKE_JITTER_SECONDS
from gerrit_clone.refresh_output import (
    RefreshAuthError,
    RefreshError,
    RefreshTimeoutError,
)
from gerrit_clone.refresh_repo_state import StashOutcome
from gerrit_clone.subprocess_tracking import ProcessAbandonedError
from gerrit_clone.url_credentials import redact_url

if TYPE_CHECKING:
    from pathlib import Path

    from gerrit_clone.content_spec import ContentFilterSpec

logger = get_logger(__name__)

# ``time`` is re-exported deliberately: the refresh sleeps live in the
# mixin modules, but the existing patch target
# ``gerrit_clone.refresh_worker.time.sleep`` must keep resolving.
__all__ = [
    "CANNOT_FOLLOW_REFUSAL",
    "EARLIER_RELEASE_REFUSAL",
    "FILTERED_REFRESH_REFUSAL",
    "FILTERED_WORKING_COPY_REFUSAL",
    "SSH_HANDSHAKE_JITTER_SECONDS",
    "RefreshAuthError",
    "RefreshError",
    "RefreshTimeoutError",
    "RefreshWorker",
    "StashOutcome",
    "time",
]


def _keeping_earlier(message: str, earlier: str | None) -> str:
    """*message*, followed by any *earlier* error it would otherwise replace.

    A staged refresh that could not remove its copy has said where it
    is, and that it may hold unfiltered history; an abandon or error
    after that must not lose it.
    """
    return f"{message}; {earlier}" if earlier else message


class RefreshWorker(StagedCheckoutMixin):
    """Worker for refreshing individual repositories."""

    def __init__(
        self,
        config: Config | None = None,
        retry_policy: RetryPolicy | None = None,
        timeout: int = 300,
        fetch_only: bool = False,
        prune: bool = True,
        skip_conflicts: bool = True,
        auto_stash: bool = False,
        strategy: str = "merge",
        filter_gerrit_only: bool = True,
        force: bool = False,
        force_hard: bool = False,
        ssh_jitter_seconds: float = SSH_HANDSHAKE_JITTER_SECONDS,
        content_filters: ContentFilterSpec | None = None,
    ) -> None:
        """Initialize refresh worker.

        Args:
            config: Optional configuration for Git operations (SSH, etc.)
            retry_policy: Retry policy for transient errors
            timeout: Timeout for each git operation in seconds
            fetch_only: Only fetch changes without merging
            prune: Prune deleted remote branches
            skip_conflicts: Skip repositories with uncommitted changes
            auto_stash: Automatically stash uncommitted changes
            strategy: Git pull strategy ('merge' or 'rebase')
            filter_gerrit_only: Only refresh repositories with Gerrit remotes
            force: Force refresh by fixing detached HEAD, upstream tracking, and stashing changes
            force_hard: Superset of ``force`` that additionally hard-resets the
                default branch to its upstream ref, discarding local commits and
                divergence. Implies ``force``.
            ssh_jitter_seconds: Maximum random delay before each SSH-backed git
                network operation, used to de-synchronise concurrent handshakes.
            content_filters: The content filters this run applies to what
                it refreshes, if any.  A repository content filtering has
                rewritten is refreshed only when they cover the filters it
                was rewritten under; see :mod:`gerrit_clone.refresh_filtered`.
        """
        self.config = config
        self.retry_policy = retry_policy or RetryPolicy()
        self.timeout = timeout
        self.fetch_only = fetch_only
        self.prune = prune
        self.skip_conflicts = skip_conflicts
        self.auto_stash = auto_stash
        self.strategy = strategy
        self.filter_gerrit_only = filter_gerrit_only
        # force_hard is a strict superset of force.
        self.force_hard = force_hard
        self.force = force or force_hard
        self.ssh_jitter_seconds = max(0.0, ssh_jitter_seconds)
        self.content_filters = content_filters

    def refresh_repository(self, repo_path: Path) -> RefreshResult:
        """Refresh a single repository.

        Args:
            repo_path: Path to repository root

        Returns:
            RefreshResult with operation details
        """
        started_at = datetime.now(UTC)
        project_name = self._get_project_name(repo_path)

        result = RefreshResult(
            path=repo_path,
            project_name=project_name,
            status=RefreshStatus.PENDING,
            started_at=started_at,
            first_started_at=started_at,
        )

        try:
            if not self._is_git_repository(repo_path):
                result.status = RefreshStatus.NOT_GIT_REPO
                result.error_message = "Not a Git repository"
                self._stamp_completion(result, started_at)
                logger.debug(f"⊘ {project_name}: Not a Git repository")
                return result

            bare = self._is_bare_repository(repo_path)
            state: dict[str, Any] = {}
            if not bare:
                state = self._check_repository_state(repo_path)
                result.current_branch = state.get("branch")
                result.detached_head = state.get("detached_head", False)
                result.had_uncommitted_changes = state.get("has_uncommitted", False)

            remote_url = self._get_remote_url(repo_path)
            # Recorded in the manifest and quoted below: never a credential.
            result.remote_url = redact_url(remote_url) if remote_url else None

            # Read before the Gerrit gate: a config too broken to read the
            # policy hides the remotes too, and must fail the repository,
            # not pass it off as "not Gerrit".
            recorded = recorded_policy(repo_path)

            # Check if it's a Gerrit repository.  Every remote counts, the
            # fetch being --all.
            if self.filter_gerrit_only and not self._has_gerrit_remote(repo_path):
                result.status = RefreshStatus.NOT_GERRIT_REPO
                result.error_message = (
                    f"Not a Gerrit repository (remote: {result.remote_url})"
                )
                self._stamp_completion(result, started_at)
                logger.debug(f"⊘ {project_name}: Not a Gerrit repository")
                return result

            if self._needs_staging(repo_path, recorded, bare=bare):
                return self._refresh_filtered(
                    repo_path, result, recorded, bare=bare, started_at=started_at
                )

            if bare:
                # A bare mirror -- the default clone -- has no working
                # tree: no branch to repair or pull, nothing to stash.
                # It is brought up to date by fetching every ref.
                if not self._prepare_bare_repository(repo_path, result, started_at):
                    return result
            elif not self._prepare_repository(repo_path, result, state, started_at):
                return result

            result.status = RefreshStatus.REFRESHING
            success = self._refresh_in_place(repo_path, result, bare=bare)
            self._apply_refresh_outcome(repo_path, result, success)

        except ProcessAbandonedError:
            # The batch gave up: no failure to log, and nothing more may be
            # launched -- a stash it made is left for `git stash list`.
            result.status = RefreshStatus.FAILED
            result.error_message = _keeping_earlier(
                "Refresh abandoned before it finished", result.error_message
            )
            logger.debug(f"⊘ {project_name}: refresh abandoned")

        except Exception as e:
            result.status = RefreshStatus.FAILED
            result.error_message = _keeping_earlier(
                f"Unexpected error: {e}", result.error_message
            )
            self._stamp_completion(result, started_at)
            logger.error(f"❌ {project_name}: {e}")
            return result

        # Set completion metadata
        self._stamp_completion(result, started_at)

        return result

    def _prepare_repository(
        self,
        repo_path: Path,
        result: RefreshResult,
        state: dict[str, Any],
        started_at: datetime,
    ) -> bool:
        """Bring a repository into a refreshable state.

        Force mode repairs the repository in place; normal mode instead skips
        anything that is not already refreshable.

        Args:
            repo_path: Repository path
            result: Result object to update
            state: Repository state as observed before preparation
            started_at: Timestamp the refresh began, for completion metadata

        Returns:
            True if the repository is ready to refresh, False if the refresh
            must stop and return ``result`` as-is.
        """
        if self.force:
            # Force mode: Fix issues automatically
            if not self._prepare_forced_repository(
                repo_path, result, state, started_at
            ):
                return False
        # Normal mode: Skip problematic repos
        elif not self._guard_unforced_repository(result, state, started_at):
            return False

        return self._handle_uncommitted_changes(repo_path, result, started_at)

    def _prepare_bare_repository(
        self, repo_path: Path, result: RefreshResult, started_at: datetime
    ) -> bool:
        """Skip a bare repository that fetching would not update.

        Args:
            repo_path: Path to a bare repository
            result: Result object to update
            started_at: Timestamp the refresh began, for completion metadata

        Returns:
            True if the repository is ready to refresh, False if it was
            skipped.
        """
        obstacle = self._bare_refresh_obstacle(repo_path)
        if obstacle is None:
            return True
        result.status = RefreshStatus.SKIPPED
        result.error_message = obstacle
        self._stamp_completion(result, started_at)
        logger.warning(f"⚠️ {result.project_name}: {obstacle}, skipping refresh")
        return False

    def _guard_unforced_repository(
        self, result: RefreshResult, state: dict[str, Any], started_at: datetime
    ) -> bool:
        """Skip repositories that normal (non-force) mode must not touch.

        Args:
            result: Result object to update
            state: Repository state as observed before preparation
            started_at: Timestamp the refresh began, for completion metadata

        Returns:
            True if the repository is refreshable, False if it was skipped.
        """
        if result.detached_head:
            result.status = RefreshStatus.DETACHED_HEAD
            result.error_message = "Repository in detached HEAD state"
            self._stamp_completion(result, started_at)
            logger.warning(
                f"⚠️ {result.project_name}: Detached HEAD state, skipping refresh"
            )
            return False

        if not state.get("has_upstream", False):
            result.status = RefreshStatus.SKIPPED
            result.error_message = (
                f"Branch '{result.current_branch}' has no upstream tracking branch"
            )
            self._stamp_completion(result, started_at)
            logger.warning(
                f"⚠️ {result.project_name}: No upstream tracking branch, skipping refresh"
            )
            return False

        return True

    def _handle_uncommitted_changes(
        self, repo_path: Path, result: RefreshResult, started_at: datetime
    ) -> bool:
        """Skip or stash an unclean working tree outside force mode.

        Args:
            repo_path: Repository path
            result: Result object to update
            started_at: Timestamp the refresh began, for completion metadata

        Returns:
            True if the repository is ready to refresh, False if the refresh
            must stop and return ``result`` as-is.
        """
        if not result.had_uncommitted_changes or self.force:
            return True

        if self.skip_conflicts and not self.auto_stash:
            result.status = RefreshStatus.UNCOMMITTED_CHANGES
            result.error_message = "Uncommitted changes present"
            self._stamp_completion(result, started_at)
            logger.warning(
                f"⚠️ {result.project_name}: Uncommitted changes, skipping refresh"
            )
            return False

        if self.auto_stash:
            # Stash uncommitted changes
            stash_outcome = self._stash_changes(repo_path)
            if stash_outcome is StashOutcome.CREATED:
                result.stash_created = True
                result.stash_branch = result.current_branch
                logger.debug(f"💾 {result.project_name}: Stashed uncommitted changes")
            elif stash_outcome is StashOutcome.NOTHING_TO_STASH:
                # Nothing git could stash (e.g. a modified submodule
                # gitlink); proceed with the refresh as if clean.
                logger.debug(
                    f"💾 {result.project_name}: Nothing to stash "
                    f"(e.g. submodule-only change)"
                )
            else:
                result.status = RefreshStatus.FAILED
                result.error_message = "Failed to stash uncommitted changes"
                self._stamp_completion(result, started_at)
                logger.error(
                    f"❌ {result.project_name}: Failed to stash uncommitted changes"
                )
                return False

        return True

    def _apply_refresh_outcome(
        self, repo_path: Path, result: RefreshResult, success: bool
    ) -> None:
        """Record the final status and restore any stash we created.

        Args:
            repo_path: Repository path
            result: Result object to update
            success: Whether the fetch/pull ultimately succeeded
        """
        if not success:
            # A refused working copy keeps SKIPPED, and a conflict CONFLICTS.
            if result.status not in (RefreshStatus.SKIPPED, RefreshStatus.CONFLICTS):
                result.status = RefreshStatus.FAILED
            if not result.error_message:
                result.error_message = "Refresh failed for unknown reason"
            return

        # Check if we pulled any commits
        if result.commits_pulled > 0:
            result.status = RefreshStatus.SUCCESS
            result.was_behind = True
            logger.debug(
                f"✅ {result.project_name}: Updated ({result.commits_pulled} commits, {result.files_changed} files)"
            )
        else:
            result.status = RefreshStatus.UP_TO_DATE
            logger.debug(f"✓ {result.project_name}: Already up-to-date")

        self._restore_stash(repo_path, result)

    def _get_project_name(self, repo_path: Path) -> str:
        """Get project name from repository path.

        Args:
            repo_path: Repository path

        Returns:
            Project name (directory name)
        """
        return repo_path.name
