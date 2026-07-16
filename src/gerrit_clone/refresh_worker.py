# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2025 Matthew Watkins <mwatkins@linuxfoundation.org>

"""Refresh worker for individual repository update operations."""

from __future__ import annotations

import os
import random
import re
import subprocess
import time
from datetime import UTC, datetime
from enum import Enum
from typing import TYPE_CHECKING, Any

from gerrit_clone.git_utils import is_git_repository
from gerrit_clone.logging import get_logger
from gerrit_clone.models import Config, RefreshResult, RefreshStatus, RetryPolicy

if TYPE_CHECKING:
    from pathlib import Path

logger = get_logger(__name__)

# Maximum random delay (seconds) inserted before each SSH-backed git network
# operation. Spreading handshakes across a small window de-synchronises worker
# threads so we do not open many simultaneous SSH connections to Gerrit, which
# is a common cause of transient "Could not read from remote repository"
# throttling failures.
SSH_HANDSHAKE_JITTER_SECONDS = 0.25

# Transient SSH / network failures that are safe to retry. Gerrit prints some of
# these (notably "could not read from remote repository") for genuine
# authentication failures too, so callers MUST check for authentication markers
# (see ``_AUTH_ERROR_PATTERNS``) BEFORE treating an error as transient.
_TRANSIENT_GIT_ERROR_PATTERNS = (
    "could not read from remote repository",
    "early eof",
    "the remote end hung up unexpectedly",
    "kex_exchange_identification",
    "ssh_exchange_identification",
    "connection closed by remote host",
    "connection reset by peer",
    "connection reset",
    "broken pipe",
)

# Markers that unambiguously indicate an authentication / authorization failure.
_AUTH_ERROR_PATTERNS = (
    "permission denied",
    "publickey",
    "authentication failed",
    "access denied",
)

# Markers indicating the remote repository does not exist (or is not visible as
# a project). Gerrit and GitHub also print the generic "could not read from
# remote repository" line for a missing repo, so callers MUST check these
# BEFORE the transient patterns to avoid misclassifying a permanently missing
# repository as a retryable network blip.
_NOT_FOUND_GIT_ERROR_PATTERNS = (
    "repository not found",
    "does not exist",
)

# Markers indicating the local branch has diverged from upstream and a
# fast-forward-only update is not possible (i.e. local commits exist).
_DIVERGED_BRANCH_PATTERNS = (
    "diverging branches",
    "not possible to fast-forward",
    "can't be fast-forwarded",
    "cannot be fast-forwarded",
)

# Auth-classified failures are normally fatal, but a Gerrit server throttling a
# burst of concurrent SSH connections can reject a valid key with "Permission
# denied (publickey)" while it drops the connection. Retrying such a failure a
# small, bounded number of times recovers these transient throttles, whereas a
# genuinely misconfigured key still fails quickly instead of consuming the full
# network-retry budget across every repository.
_MAX_AUTH_RETRY_ATTEMPTS = 2


class StashOutcome(Enum):
    """Result of attempting to stash a working tree.

    ``git stash push`` exits 0 both when it stashes changes and when it finds
    nothing to stash (for example a working tree whose only modification is a
    submodule gitlink, which git stash does not capture). Distinguishing these
    lets callers avoid both a spurious "failed to stash" error and a later
    "failed to restore stash" warning for a stash that was never created.
    """

    CREATED = "created"
    """A new stash entry was created."""

    NOTHING_TO_STASH = "nothing_to_stash"
    """The command succeeded but there was nothing git could stash."""

    FAILED = "failed"
    """The stash command errored."""


class RefreshError(Exception):
    """Base exception for refresh operations."""


class RefreshTimeoutError(RefreshError):
    """Raised when refresh operation times out."""


class RefreshAuthError(RefreshError):
    """Raised when a refresh fails with an authentication-style error.

    Kept distinct from :class:`RefreshError` so the retry loop can apply a
    small, dedicated retry budget: a throttled Gerrit may reject a valid key
    with "Permission denied (publickey)" while dropping a connection, which a
    couple of retries recover, whereas a genuine auth misconfiguration should
    fail without consuming the full network-retry budget.
    """


class RefreshWorker:
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
                result.completed_at = datetime.now(UTC)
                result.duration_seconds = (
                    result.completed_at - started_at
                ).total_seconds()
                logger.debug(f"⊘ {project_name}: Not a Git repository")
                return result

            state = self._check_repository_state(repo_path)
            result.current_branch = state.get("branch")
            result.detached_head = state.get("detached_head", False)
            result.had_uncommitted_changes = state.get("has_uncommitted", False)

            remote_url = self._get_remote_url(repo_path)
            result.remote_url = remote_url

            # Check if it's a Gerrit repository
            if self.filter_gerrit_only and not self._is_gerrit_repository(remote_url):
                result.status = RefreshStatus.NOT_GERRIT_REPO
                result.error_message = f"Not a Gerrit repository (remote: {remote_url})"
                result.completed_at = datetime.now(UTC)
                result.duration_seconds = (
                    result.completed_at - started_at
                ).total_seconds()
                logger.debug(f"⊘ {project_name}: Not a Gerrit repository")
                return result

            # Force mode: Fix issues automatically
            if self.force:
                # Fix detached HEAD
                if result.detached_head:
                    # Check if we're on Gerrit's meta/config branch
                    if state.get("on_meta_config", False):
                        logger.debug(
                            f"🔧 {project_name}: On Gerrit meta/config branch, switching to code branch"
                        )
                    else:
                        logger.debug(f"🔧 {project_name}: Fixing detached HEAD state")

                    if self._fix_detached_head(repo_path, result):
                        # Re-check state after fix
                        state = self._check_repository_state(repo_path)
                        result.current_branch = state.get("branch")
                        result.detached_head = state.get("detached_head", False)
                        logger.debug(
                            f"✓ {project_name}: Checked out branch '{result.current_branch}'"
                        )
                    # Check if this is a meta-only repo (parent project)
                    elif result.error_message and "meta-only" in result.error_message:
                        result.status = RefreshStatus.SKIPPED
                        result.completed_at = datetime.now(UTC)
                        result.duration_seconds = (
                            result.completed_at - started_at
                        ).total_seconds()
                        logger.debug(
                            f"⊘ {project_name}: Skipping Gerrit parent project (no code branches)"
                        )
                        return result
                    else:
                        result.status = RefreshStatus.FAILED
                        result.error_message = (
                            result.error_message or "Failed to fix detached HEAD state"
                        )
                        result.completed_at = datetime.now(UTC)
                        result.duration_seconds = (
                            result.completed_at - started_at
                        ).total_seconds()
                        logger.error(f"❌ {project_name}: Failed to fix detached HEAD")
                        return result

                # Force mode: if parked on a non-default feature branch, switch
                # back to the default branch so we refresh the mainline rather
                # than local feature work. Use a local-only default-branch lookup
                # first to avoid an extra networked ls-remote per repository.
                default_branch: str | None = None
                if result.current_branch and not result.detached_head:
                    default_branch = self._get_default_branch_local(repo_path)
                    if default_branch is None:
                        default_branch = self._get_default_branch(repo_path)
                    if default_branch and result.current_branch != default_branch:
                        logger.debug(
                            f"🔧 {project_name}: Switching from feature branch "
                            f"'{result.current_branch}' to default branch '{default_branch}'"
                        )
                        # Stash first so an unclean tree cannot block checkout.
                        if (
                            result.had_uncommitted_changes
                            and not result.stash_created
                            and self._stash_changes(repo_path) is StashOutcome.CREATED
                        ):
                            result.stash_created = True
                            # Record the branch the stash came from so it is
                            # not auto-popped onto the default branch after
                            # the switch below (that would apply
                            # feature-branch work to the wrong branch).
                            result.stash_branch = result.current_branch
                            # Tree is now clean; clear the dirty flag so the
                            # later force-mode stash does not try to re-stash
                            # a clean tree if the checkout below fails.
                            result.had_uncommitted_changes = False
                        if self._switch_to_default_branch(repo_path, default_branch):
                            state = self._check_repository_state(repo_path)
                            result.current_branch = state.get("branch")
                            result.detached_head = state.get("detached_head", False)
                            result.had_uncommitted_changes = state.get(
                                "has_uncommitted", False
                            )
                            logger.debug(
                                f"✓ {project_name}: Switched to default branch "
                                f"'{result.current_branch}'"
                            )
                        else:
                            logger.warning(
                                f"⚠️ {project_name}: Could not switch to default "
                                f"branch '{default_branch}', refreshing current branch"
                            )

                # Fix upstream tracking
                if not state.get("has_upstream", False) and result.current_branch:
                    logger.debug(
                        f"🔧 {project_name}: Fixing upstream tracking for '{result.current_branch}'"
                    )
                    if self._fix_upstream_tracking(repo_path, result):
                        # Re-check state after fix
                        state = self._check_repository_state(repo_path)
                        result.current_branch = state.get("branch")
                        result.detached_head = state.get("detached_head", False)
                        result.had_uncommitted_changes = state.get(
                            "has_uncommitted", False
                        )
                        logger.debug(f"✓ {project_name}: Set upstream tracking")
                    else:
                        logger.warning(
                            f"⚠️ {project_name}: Could not set upstream, will try default branch"
                        )
                        # Try switching to default branch as fallback
                        if self._fix_detached_head(repo_path, result):
                            state = self._check_repository_state(repo_path)
                            result.current_branch = state.get("branch")
                            result.detached_head = state.get("detached_head", False)
                            result.had_uncommitted_changes = state.get(
                                "has_uncommitted", False
                            )
                            logger.debug(
                                f"✓ {project_name}: Switched to default branch '{result.current_branch}'"
                            )
                        else:
                            # Both upstream fix and default branch checkout failed
                            result.status = RefreshStatus.FAILED
                            result.error_message = "Failed to fix upstream tracking and could not switch to default branch"
                            result.completed_at = datetime.now(UTC)
                            result.duration_seconds = (
                                result.completed_at - started_at
                            ).total_seconds()
                            logger.error(
                                f"❌ {project_name}: Could not fix repository state"
                            )
                            return result

                # Always stash in force mode
                if result.had_uncommitted_changes:
                    logger.debug(
                        f"💾 {project_name}: Force stashing uncommitted changes"
                    )
                    stash_outcome = self._stash_changes(repo_path)
                    if stash_outcome is StashOutcome.CREATED:
                        result.stash_created = True
                        result.stash_branch = result.current_branch
                    elif stash_outcome is StashOutcome.NOTHING_TO_STASH:
                        # Nothing git could stash (e.g. a modified submodule
                        # gitlink). Not an error and nothing to restore later.
                        result.had_uncommitted_changes = False
                        logger.debug(
                            f"💾 {project_name}: Nothing to stash "
                            f"(e.g. submodule-only change)"
                        )
                    else:
                        result.status = RefreshStatus.FAILED
                        result.error_message = (
                            "Failed to stash uncommitted changes in force mode"
                        )
                        result.completed_at = datetime.now(UTC)
                        result.duration_seconds = (
                            result.completed_at - started_at
                        ).total_seconds()
                        logger.error(f"❌ {project_name}: Failed to stash changes")
                        return result

                # Force-hard mode: discard any local commits / divergence by
                # hard-resetting the default branch to its upstream ref. This
                # is the single, explicit way to make local content exactly
                # match the remote; the normal pull that follows is then a
                # no-op fast-forward.
                #
                # Guard the reset so it only ever touches the default branch.
                # If the default branch could not be determined or the switch
                # to it failed above, we are still parked on a feature branch;
                # hard-resetting it would silently discard local-only commits,
                # which contradicts the documented "default branch only"
                # contract. Skip the reset in that case.
                if self.force_hard and result.current_branch:
                    on_default_branch = (
                        default_branch is not None
                        and result.current_branch == default_branch
                    )
                    if not on_default_branch:
                        logger.warning(
                            f"⚠️ {project_name}: Not on the default branch "
                            f"(on '{result.current_branch}', default "
                            f"'{default_branch}'); skipping hard reset to avoid "
                            f"discarding local commits"
                        )
                    elif self._reset_to_upstream(repo_path, result):
                        result.hard_reset = True
                        logger.debug(
                            f"🧨 {project_name}: Hard reset '{result.current_branch}' "
                            f"to upstream"
                        )
                    else:
                        logger.warning(
                            f"⚠️ {project_name}: Hard reset to upstream failed "
                            f"(no upstream?), continuing with pull"
                        )
            else:
                # Normal mode: Skip problematic repos
                # Handle detached HEAD state
                if result.detached_head:
                    result.status = RefreshStatus.DETACHED_HEAD
                    result.error_message = "Repository in detached HEAD state"
                    result.completed_at = datetime.now(UTC)
                    result.duration_seconds = (
                        result.completed_at - started_at
                    ).total_seconds()
                    logger.warning(
                        f"⚠️ {project_name}: Detached HEAD state, skipping refresh"
                    )
                    return result

                if not state.get("has_upstream", False):
                    result.status = RefreshStatus.SKIPPED
                    result.error_message = f"Branch '{result.current_branch}' has no upstream tracking branch"
                    result.completed_at = datetime.now(UTC)
                    result.duration_seconds = (
                        result.completed_at - started_at
                    ).total_seconds()
                    logger.warning(
                        f"⚠️ {project_name}: No upstream tracking branch, skipping refresh"
                    )
                    return result

            # Handle uncommitted changes (non-force mode)
            if result.had_uncommitted_changes and not self.force:
                if self.skip_conflicts and not self.auto_stash:
                    result.status = RefreshStatus.UNCOMMITTED_CHANGES
                    result.error_message = "Uncommitted changes present"
                    result.completed_at = datetime.now(UTC)
                    result.duration_seconds = (
                        result.completed_at - started_at
                    ).total_seconds()
                    logger.warning(
                        f"⚠️ {project_name}: Uncommitted changes, skipping refresh"
                    )
                    return result
                elif self.auto_stash:
                    # Stash uncommitted changes
                    stash_outcome = self._stash_changes(repo_path)
                    if stash_outcome is StashOutcome.CREATED:
                        result.stash_created = True
                        result.stash_branch = result.current_branch
                        logger.debug(f"💾 {project_name}: Stashed uncommitted changes")
                    elif stash_outcome is StashOutcome.NOTHING_TO_STASH:
                        # Nothing git could stash (e.g. a modified submodule
                        # gitlink); proceed with the refresh as if clean.
                        logger.debug(
                            f"💾 {project_name}: Nothing to stash "
                            f"(e.g. submodule-only change)"
                        )
                    else:
                        result.status = RefreshStatus.FAILED
                        result.error_message = "Failed to stash uncommitted changes"
                        result.completed_at = datetime.now(UTC)
                        result.duration_seconds = (
                            result.completed_at - started_at
                        ).total_seconds()
                        logger.error(
                            f"❌ {project_name}: Failed to stash uncommitted changes"
                        )
                        return result

            result.status = RefreshStatus.REFRESHING

            success = self._execute_adaptive_refresh(repo_path, result)

            if success:
                # Check if we pulled any commits
                if result.commits_pulled > 0:
                    result.status = RefreshStatus.SUCCESS
                    result.was_behind = True
                    logger.debug(
                        f"✅ {project_name}: Updated ({result.commits_pulled} commits, {result.files_changed} files)"
                    )
                else:
                    result.status = RefreshStatus.UP_TO_DATE
                    logger.debug(f"✓ {project_name}: Already up-to-date")

                # Pop stash if we created one, but only back onto the branch it
                # came from. In force mode the stash may have been taken on a
                # feature branch before switching to the default branch; popping
                # it here would apply that work to the wrong branch (and drop
                # the stash entry). In that case leave the stash intact for
                # manual recovery.
                if result.stash_created:
                    stashed_elsewhere = (
                        result.stash_branch is not None
                        and result.current_branch != result.stash_branch
                    )
                    if stashed_elsewhere:
                        logger.warning(
                            f"⚠️ {project_name}: Stash was created on "
                            f"'{result.stash_branch}' but the working tree is now "
                            f"on '{result.current_branch}'; leaving the stash "
                            f"intact for manual recovery (git stash list)"
                        )
                    elif self._pop_stash(repo_path):
                        result.stash_popped = True
                        logger.debug(f"💾 {project_name}: Restored stashed changes")
                    else:
                        logger.warning(
                            f"⚠️ {project_name}: Failed to restore stash (may have conflicts)"
                        )
            else:
                result.status = RefreshStatus.FAILED
                if not result.error_message:
                    result.error_message = "Refresh failed for unknown reason"

        except Exception as e:
            result.status = RefreshStatus.FAILED
            result.error_message = f"Unexpected error: {e}"
            result.completed_at = datetime.now(UTC)
            result.duration_seconds = (result.completed_at - started_at).total_seconds()
            logger.error(f"❌ {project_name}: {e}")
            return result

        # Set completion metadata
        result.completed_at = datetime.now(UTC)
        result.duration_seconds = (result.completed_at - started_at).total_seconds()

        return result

    def _execute_adaptive_refresh(self, repo_path: Path, result: RefreshResult) -> bool:
        """Execute refresh with adaptive retry logic.

        Args:
            repo_path: Repository path
            result: Result object to update

        Returns:
            True if refresh succeeded, False otherwise
        """
        max_attempts = self.retry_policy.max_attempts
        # Auth-style failures get a smaller, dedicated retry budget (see
        # _MAX_AUTH_RETRY_ATTEMPTS): a throttled Gerrit can reject a valid key
        # while dropping a connection, which a couple of retries recover, but a
        # real auth misconfiguration should not consume the full network-retry
        # budget.
        max_auth_attempts = min(max_attempts, _MAX_AUTH_RETRY_ATTEMPTS)
        attempt = 0
        auth_attempt = 0

        while attempt < max_attempts:
            attempt += 1
            try:
                success = self._perform_refresh(repo_path, result)
                if success:  # noqa: SIM103
                    return True

                # If we get here, refresh failed but didn't raise exception
                # (non-retryable error)
                return False

            except RefreshAuthError as e:
                auth_attempt += 1
                result.retry_count += 1
                if auth_attempt < max_auth_attempts:
                    # Base the backoff on the overall attempt counter, not the
                    # smaller auth counter, so an auth failure following earlier
                    # network retries does not reset exponential backoff and
                    # re-collide with a throttled Gerrit.
                    delay = self._calculate_adaptive_delay(attempt)
                    logger.warning(
                        f"⚠️ {result.project_name}: {e} (auth attempt {auth_attempt}/{max_auth_attempts}), retrying in {delay:.1f}s"
                    )
                    time.sleep(delay)
                else:
                    logger.error(
                        f"❌ {result.project_name}: {e} (auth retries exhausted)"
                    )
                    result.error_message = str(e)
                    return False

            except RefreshTimeoutError:
                result.retry_count += 1
                if attempt < max_attempts:
                    delay = self._calculate_adaptive_delay(attempt)
                    logger.warning(
                        f"⏱️ {result.project_name}: Timeout (attempt {attempt}/{max_attempts}), retrying in {delay:.1f}s"
                    )
                    time.sleep(delay)
                else:
                    logger.error(
                        f"❌ {result.project_name}: Timeout after {max_attempts} attempts"
                    )
                    return False

            except RefreshError as e:
                result.retry_count += 1
                if attempt < max_attempts and self._is_retryable_error(str(e)):
                    delay = self._calculate_adaptive_delay(attempt)
                    logger.warning(
                        f"⚠️ {result.project_name}: {e} (attempt {attempt}/{max_attempts}), retrying in {delay:.1f}s"
                    )
                    time.sleep(delay)
                else:
                    logger.error(
                        f"❌ {result.project_name}: {e} (non-retryable or max attempts reached)"
                    )
                    result.error_message = str(e)
                    return False

        return False

    def _perform_refresh(self, repo_path: Path, result: RefreshResult) -> bool:
        """Perform the actual refresh operation.

        Args:
            repo_path: Repository path
            result: Result object to update

        Returns:
            True if refresh succeeded, False otherwise

        Raises:
            RefreshError: If refresh fails with retryable error
            RefreshTimeoutError: If refresh times out
        """
        result.attempts += 1
        attempt_start = datetime.now(UTC)

        # Spread out SSH handshakes across concurrent workers to avoid Gerrit
        # throttling a burst of simultaneous connections.
        self._ssh_handshake_jitter(repo_path)

        try:
            if self.fetch_only:
                # Fetch only, don't merge
                success = self._execute_git_fetch(repo_path, result)
            else:
                success = self._execute_git_pull(repo_path, result)

            attempt_duration = (datetime.now(UTC) - attempt_start).total_seconds()
            result.last_attempt_duration = attempt_duration

            return success

        except RefreshError:
            # Already-classified refresh errors (auth, timeout, transient)
            # propagate unchanged so the retry loop applies the correct retry
            # budget instead of re-wrapping them as a generic error.
            # RefreshTimeoutError and RefreshAuthError both subclass
            # RefreshError, so catching the base class covers all three.
            raise

        except subprocess.TimeoutExpired as err:
            error_msg = f"Git operation timeout after {self.timeout}s"
            result.error_message = error_msg
            raise RefreshTimeoutError(error_msg) from err

        except Exception as e:
            error_msg = f"Unexpected error during refresh: {e}"
            result.error_message = error_msg
            raise RefreshError(error_msg) from e

    def _execute_git_fetch(self, repo_path: Path, result: RefreshResult) -> bool:
        """Execute git fetch operation.

        Args:
            repo_path: Repository path
            result: Result object to update

        Returns:
            True if fetch succeeded
        """
        cmd = ["git", "fetch"]

        if self.prune:
            cmd.append("--prune")

        cmd.extend(["--all", "--tags"])

        env = self._build_git_environment()

        logger.debug(f"🔄 Fetching {result.project_name}")

        try:
            process_result = subprocess.run(
                cmd,
                cwd=repo_path,
                env=env,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=self.timeout,
                check=False,
            )

            if process_result.returncode == 0:
                # Parse fetch output to see if anything was updated
                output = process_result.stderr  # Git fetch writes to stderr
                result.commits_pulled = self._count_fetched_commits(output)
                return True
            else:
                error_msg = self._analyze_git_error(process_result, "fetch")

                # Raise first for retryable errors: the same RefreshResult
                # is reused across retry attempts, so recording the message
                # now would leave it stale on a later successful attempt.
                # Only record it for non-retryable (hard) failures, which
                # is when this call returns normally.
                self._raise_for_retryable_git_error(process_result, error_msg)
                result.error_message = error_msg
                return False

        except subprocess.TimeoutExpired as err:
            raise RefreshTimeoutError(f"Fetch timeout after {self.timeout}s") from err

    def _execute_git_pull(self, repo_path: Path, result: RefreshResult) -> bool:
        """Execute git pull operation.

        Args:
            repo_path: Repository path
            result: Result object to update

        Returns:
            True if pull succeeded
        """
        cmd = ["git", "pull"]

        # Add strategy option
        if self.strategy == "rebase":
            cmd.append("--rebase")
        elif self.strategy == "merge":
            # Fast-forward only for safety
            cmd.append("--ff-only")

        if self.prune:
            cmd.append("--prune")

        env = self._build_git_environment()

        logger.debug(f"🔄 Pulling {result.project_name}")

        try:
            process_result = subprocess.run(
                cmd,
                cwd=repo_path,
                env=env,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=self.timeout,
                check=False,
            )

            if process_result.returncode == 0:
                output = process_result.stdout + process_result.stderr
                result.commits_pulled = self._count_pulled_commits(output)
                result.files_changed = self._count_changed_files(output)
                return True
            else:
                error_msg = self._analyze_git_error(process_result, "pull")

                # Check for conflicts (a hard failure: always record the
                # message).
                if (
                    "CONFLICT" in process_result.stdout
                    or "CONFLICT" in process_result.stderr
                ):
                    result.error_message = error_msg
                    result.status = RefreshStatus.CONFLICTS
                    logger.error(f"⚠️ {result.project_name}: Merge conflicts detected")
                    return False

                # Raise first for retryable errors: the same RefreshResult
                # is reused across retry attempts, so recording the message
                # now would leave it stale on a later successful attempt.
                # Only record it for non-retryable (hard) failures, which
                # is when this call returns normally.
                self._raise_for_retryable_git_error(process_result, error_msg)
                result.error_message = error_msg
                return False

        except subprocess.TimeoutExpired as err:
            raise RefreshTimeoutError(f"Pull timeout after {self.timeout}s") from err

    def _is_git_repository(self, path: Path) -> bool:
        """Check if path is a valid Git repository (regular or bare).

        Args:
            path: Path to check

        Returns:
            True if path is a Git repository (regular or bare)
        """
        # Use shared utility that detects both regular and bare repositories
        return is_git_repository(path)

    def _get_remote_url(self, repo_path: Path) -> str | None:
        """Get the remote URL for the repository.

        Args:
            repo_path: Repository path

        Returns:
            Remote URL or None if not found
        """
        try:
            result = subprocess.run(
                ["git", "config", "--get", "remote.origin.url"],
                cwd=repo_path,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=5,
                check=False,
            )

            if result.returncode == 0:
                return result.stdout.strip()
            return None

        except Exception as e:
            logger.debug(f"Failed to get remote URL: {e}")
            return None

    def _is_gerrit_repository(self, remote_url: str | None) -> bool:
        """Check if remote URL looks like a Gerrit repository.

        Args:
            remote_url: Remote URL to check

        Returns:
            True if URL looks like Gerrit
        """
        if not remote_url:
            return False

        # Gerrit-specific patterns
        gerrit_patterns = [
            r"ssh://.*:\d+/",  # SSH with port (typical Gerrit: ssh://host:29418/project)
            r"https?://.*/r/",  # HTTPS with /r/ prefix
            r"https?://.*/gerrit/",  # HTTPS with /gerrit/ prefix
        ]

        for pattern in gerrit_patterns:
            if re.search(pattern, remote_url):
                return True

        # Additional check: Gerrit servers often have specific hostnames
        gerrit_hosts = ["gerrit", "review", "code-review"]
        return any(host in remote_url.lower() for host in gerrit_hosts)

    def _check_repository_state(self, repo_path: Path) -> dict[str, Any]:
        """Check the state of the repository.

        Args:
            repo_path: Repository path

        Returns:
            Dictionary with state information
        """
        state: dict[str, Any] = {
            "branch": None,
            "detached_head": False,
            "has_uncommitted": False,
            "has_upstream": False,
            "on_meta_config": False,
        }

        try:
            branch_result = subprocess.run(
                ["git", "rev-parse", "--abbrev-ref", "HEAD"],
                cwd=repo_path,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=5,
                check=False,
            )

            if branch_result.returncode == 0:
                branch = branch_result.stdout.strip()
                if branch == "HEAD":
                    state["detached_head"] = True
                    # Check if we're on Gerrit's meta/config branch
                    state["on_meta_config"] = self._is_on_meta_config(repo_path)
                else:
                    state["branch"] = branch

                    # Check if branch has upstream tracking
                    upstream_result = subprocess.run(
                        ["git", "rev-parse", "--abbrev-ref", f"{branch}@{{upstream}}"],
                        cwd=repo_path,
                        capture_output=True,
                        text=True,
                        encoding="utf-8",
                        errors="replace",
                        timeout=5,
                        check=False,
                    )

                    if upstream_result.returncode == 0:
                        state["has_upstream"] = True

            status_result = subprocess.run(
                ["git", "status", "--porcelain"],
                cwd=repo_path,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=5,
                check=False,
            )

            if status_result.returncode == 0:
                state["has_uncommitted"] = bool(status_result.stdout.strip())

        except Exception as e:
            logger.debug(f"Failed to check repository state: {e}")

        return state

    def _stash_changes(self, repo_path: Path) -> StashOutcome:
        """Stash uncommitted changes.

        ``git stash push`` exits 0 even when it stashes nothing (most commonly
        when the only change is a modified submodule gitlink, which git stash
        does not capture). We therefore confirm a new stash entry was actually
        created so callers can distinguish CREATED from NOTHING_TO_STASH and
        never attempt to pop a stash that does not exist.

        Args:
            repo_path: Repository path

        Returns:
            The :class:`StashOutcome` describing what happened.
        """
        try:
            before = self._stash_count(repo_path)
            result = subprocess.run(
                [
                    "git",
                    "stash",
                    "push",
                    "--include-untracked",
                    "-m",
                    "gerrit-clone refresh auto-stash",
                ],
                cwd=repo_path,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=30,
                check=False,
            )

            if result.returncode != 0:
                return StashOutcome.FAILED

            # Confirm a stash entry actually appeared. git prints "No local
            # changes to save" and exits 0 when there was nothing to stash.
            after = self._stash_count(repo_path)
            if before >= 0 and after >= 0:
                return (
                    StashOutcome.CREATED
                    if after > before
                    else StashOutcome.NOTHING_TO_STASH
                )

            # Counts unavailable: fall back to sniffing git's message.
            if "no local changes to save" in result.stdout.lower():
                return StashOutcome.NOTHING_TO_STASH
            return StashOutcome.CREATED

        except Exception as e:
            logger.debug(f"Failed to stash changes: {e}")
            return StashOutcome.FAILED

    def _is_on_meta_config(self, repo_path: Path) -> bool:
        """Check if repository is currently on Gerrit's meta/config branch.

        Args:
            repo_path: Repository path

        Returns:
            True if on meta/config branch
        """
        try:
            result = subprocess.run(
                ["git", "symbolic-ref", "-q", "HEAD"],
                cwd=repo_path,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=5,
                check=False,
            )

            if result.returncode == 0:
                ref = result.stdout.strip()
                return ref == "refs/meta/config"

            # If not a symbolic ref, check with rev-parse
            result = subprocess.run(
                ["git", "rev-parse", "--symbolic-full-name", "HEAD"],
                cwd=repo_path,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=5,
                check=False,
            )

            if result.returncode == 0:
                ref = result.stdout.strip()
                return ref == "refs/meta/config" or ref.startswith("refs/meta/")

            return False

        except Exception as e:
            logger.debug(f"Failed to check meta/config state: {e}")
            return False

    def _is_meta_only_repo(self, repo_path: Path) -> bool:
        """Check if repository is a Gerrit parent project with only meta refs.

        Gerrit parent projects are used for organizational hierarchy and
        access control, but don't contain actual code branches.

        Args:
            repo_path: Repository path

        Returns:
            True if repo only has meta/* refs and no regular branches
        """
        try:
            # List all remote heads (branches). ls-remote is an SSH-backed
            # network operation for Gerrit, so de-sync the handshake to avoid
            # bursty concurrent connections under high worker counts.
            self._ssh_handshake_jitter(repo_path)
            result = subprocess.run(
                ["git", "ls-remote", "--heads", "origin"],
                cwd=repo_path,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=10,
                check=False,
            )

            if result.returncode != 0:
                return False

            # If there are no heads at all, this is likely a meta-only repo
            output = result.stdout.strip()
            if not output:
                # Double-check that meta/config exists
                meta_result = subprocess.run(
                    ["git", "ls-remote", "origin", "refs/meta/config"],
                    cwd=repo_path,
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    timeout=10,
                    check=False,
                )

                if meta_result.returncode == 0 and meta_result.stdout.strip():
                    logger.debug(
                        f"{repo_path.name}: Confirmed as Gerrit parent project (has meta/config, no heads)"
                    )
                    return True

            return False

        except Exception as e:
            logger.debug(f"Failed to check meta-only status: {e}")
            return False

    def _get_default_branch(self, repo_path: Path) -> str | None:
        """Get the default branch name for the repository.

        Tries to determine the default branch by checking:
        1. Fetch remote to ensure we have latest refs
        2. Query remote HEAD directly via ls-remote
        3. origin/HEAD symbolic ref
        4. Common branch names (master, main, develop)

        Args:
            repo_path: Repository path

        Returns:
            Default branch name or None if not found
        """
        try:
            # First, try to query the remote directly for HEAD
            # This works even if we haven't fetched recently. ls-remote is an
            # SSH-backed network operation for Gerrit remotes, so de-sync the
            # handshake here too to avoid the same throttling _perform_refresh
            # guards against under high concurrency.
            self._ssh_handshake_jitter(repo_path)
            ls_remote_result = subprocess.run(
                ["git", "ls-remote", "--symref", "origin", "HEAD"],
                cwd=repo_path,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=10,
                check=False,
            )

            if ls_remote_result.returncode == 0:
                for line in ls_remote_result.stdout.strip().split("\n"):
                    if line.startswith("ref:"):
                        ref = line.split()[1]
                        if ref.startswith("refs/heads/"):
                            branch_name = ref.replace("refs/heads/", "")
                            # Verify this isn't a Gerrit meta ref
                            if not branch_name.startswith("meta/"):
                                logger.debug(
                                    f"Found default branch via ls-remote: {branch_name}"
                                )
                                return branch_name

            # Try to get origin/HEAD symbolic ref
            result = subprocess.run(
                ["git", "symbolic-ref", "refs/remotes/origin/HEAD"],
                cwd=repo_path,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=5,
                check=False,
            )

            if result.returncode == 0:
                # Output is like "refs/remotes/origin/master"
                ref = result.stdout.strip()
                if ref.startswith("refs/remotes/origin/"):
                    branch_name = ref.replace("refs/remotes/origin/", "")
                    if not branch_name.startswith("meta/"):
                        return branch_name

            # Fallback: check common branch names in remote
            for branch_name in ["master", "main", "develop"]:
                result = subprocess.run(
                    ["git", "ls-remote", "--heads", "origin", branch_name],
                    cwd=repo_path,
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    timeout=5,
                    check=False,
                )

                if result.returncode == 0 and result.stdout.strip():
                    logger.debug(f"Found branch via ls-remote: {branch_name}")
                    return branch_name

            logger.debug(f"No default branch found for {repo_path.name}")
            return None

        except Exception as e:
            logger.debug(f"Failed to get default branch: {e}")
            return None

    def _fix_detached_head(self, repo_path: Path, result: RefreshResult) -> bool:
        """Fix detached HEAD by checking out the default branch.

        Special handling for Gerrit's meta/config branch - detects when user
        is on the project configuration branch and switches to the actual code branch.

        Also detects Gerrit parent projects that only have meta/config and no code branches.

        Args:
            repo_path: Repository path
            result: Result object to update

        Returns:
            True if fixed successfully
        """
        try:
            # Check if we're on Gerrit's meta/config branch
            if self._is_on_meta_config(repo_path):
                logger.debug(
                    f"🔧 {repo_path.name}: Detected Gerrit meta/config branch, switching to code branch"
                )

            # Fetch remote to ensure we have latest branch info
            # This is crucial for repos that might not have been fetched recently.
            # git fetch opens an SSH connection for Gerrit, so de-sync the
            # handshake to avoid contributing to concurrent-connection throttling.
            self._ssh_handshake_jitter(repo_path)
            fetch_result = subprocess.run(
                ["git", "fetch", "--quiet", "origin"],
                cwd=repo_path,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=30,
                check=False,
            )

            if fetch_result.returncode != 0:
                logger.debug(f"Fetch failed but continuing: {fetch_result.stderr}")

            # Check if this is a Gerrit parent project (meta-only, no code branches)
            if self._is_meta_only_repo(repo_path):
                logger.debug(
                    f"{repo_path.name}: Gerrit parent project (meta-only), no code branches to refresh"
                )
                result.error_message = (
                    "Gerrit parent project (meta-only, no code branches)"
                )
                return False

            default_branch = self._get_default_branch(repo_path)

            if not default_branch:
                logger.debug(f"Could not determine default branch for {repo_path.name}")
                return False

            # Checkout the default branch
            checkout_result = subprocess.run(
                ["git", "checkout", default_branch],
                cwd=repo_path,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=30,
                check=False,
            )

            if checkout_result.returncode == 0:
                logger.debug(
                    f"Checked out branch '{default_branch}' in {repo_path.name}"
                )

                # Set upstream tracking if not already set
                set_upstream_result = subprocess.run(
                    [
                        "git",
                        "branch",
                        f"--set-upstream-to=origin/{default_branch}",
                        default_branch,
                    ],
                    cwd=repo_path,
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    timeout=10,
                    check=False,
                )

                if set_upstream_result.returncode == 0:
                    logger.debug(f"Set upstream tracking for '{default_branch}'")

                return True
            else:
                logger.debug(
                    f"Failed to checkout '{default_branch}': {checkout_result.stderr}"
                )
                return False

        except Exception as e:
            logger.debug(f"Failed to fix detached HEAD: {e}")
            return False

    def _get_default_branch_local(self, repo_path: Path) -> str | None:
        """Determine the default branch using only local refs (no network).

        Reads the locally cached ``refs/remotes/origin/HEAD`` symbolic ref, which
        gerrit-clone sets at clone time. Returns None if it is not available so
        callers can decide whether a networked lookup is warranted.

        Args:
            repo_path: Repository path

        Returns:
            Default branch name, or None if it cannot be determined locally
        """
        try:
            result = subprocess.run(
                ["git", "symbolic-ref", "--short", "refs/remotes/origin/HEAD"],
                cwd=repo_path,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=5,
                check=False,
            )
            if result.returncode == 0:
                ref = result.stdout.strip()
                # "origin/master" -> "master"
                branch = ref.split("/", 1)[1] if ref.startswith("origin/") else ref
                if branch and not branch.startswith("meta/"):
                    return branch
            return None
        except Exception as e:
            logger.debug(f"Failed to get local default branch: {e}")
            return None

    def _switch_to_default_branch(self, repo_path: Path, default_branch: str) -> bool:
        """Check out the default branch and set its upstream tracking.

        Unlike ``_fix_detached_head`` this is intended for repositories that are
        on a (non-default) local feature branch rather than in a detached HEAD
        state, and it does not perform meta/config or parent-project detection.

        Args:
            repo_path: Repository path
            default_branch: Name of the branch to check out

        Returns:
            True if the branch was checked out successfully
        """
        try:
            checkout_result = subprocess.run(
                ["git", "checkout", default_branch],
                cwd=repo_path,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=30,
                check=False,
            )

            if checkout_result.returncode != 0:
                logger.debug(
                    f"Failed to checkout '{default_branch}': {checkout_result.stderr}"
                )
                return False

            # Best-effort: ensure upstream tracking is set for the default branch.
            subprocess.run(
                [
                    "git",
                    "branch",
                    f"--set-upstream-to=origin/{default_branch}",
                    default_branch,
                ],
                cwd=repo_path,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=10,
                check=False,
            )
            return True

        except Exception as e:
            logger.debug(f"Failed to switch to default branch: {e}")
            return False

    def _reset_to_upstream(self, repo_path: Path, result: RefreshResult) -> bool:
        """Hard-reset the current branch to its upstream tracking ref.

        This discards any local commits and divergence so that local content
        exactly matches the remote. Used by force-hard mode. The subsequent pull
        then fast-forwards cleanly (typically a no-op).

        Args:
            repo_path: Repository path
            result: Result object with current branch info

        Returns:
            True if the reset succeeded
        """
        if not result.current_branch:
            return False

        try:
            # Verify the branch has an upstream to reset to.
            upstream_check = subprocess.run(
                [
                    "git",
                    "rev-parse",
                    "--abbrev-ref",
                    f"{result.current_branch}@{{upstream}}",
                ],
                cwd=repo_path,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=5,
                check=False,
            )
            if upstream_check.returncode != 0:
                logger.debug(
                    f"No upstream for '{result.current_branch}', cannot hard reset"
                )
                return False

            reset_result = subprocess.run(
                ["git", "reset", "--hard", f"{result.current_branch}@{{upstream}}"],
                cwd=repo_path,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=30,
                check=False,
            )

            if reset_result.returncode == 0:
                return True

            logger.debug(f"Hard reset failed: {reset_result.stderr}")
            return False

        except Exception as e:
            logger.debug(f"Failed to hard reset to upstream: {e}")
            return False

    @staticmethod
    def _remote_uses_ssh(remote_url: str | None) -> bool:
        """Return True if the origin remote performs an SSH handshake.

        Only SSH-backed remotes benefit from handshake jitter. HTTP(S), the
        anonymous git protocol, ``file://`` URLs and local filesystem paths
        never open an SSH connection, so jittering them just adds latency. An
        unknown/empty remote is treated as SSH so the throttling protection is
        preserved when the URL cannot be read.

        Args:
            remote_url: The origin remote URL, or None if it is unknown.

        Returns:
            True if a handshake (and therefore jitter) is warranted.
        """
        if not remote_url:
            return True
        url = remote_url.strip()
        lowered = url.lower()
        if lowered.startswith("ssh://"):
            return True
        # Non-SSH transports and local paths never open an SSH handshake.
        if lowered.startswith(("http://", "https://", "git://", "file://")):
            return False
        if url.startswith(("/", "./", "../", "~")):
            return False
        # scp-like syntax (``[user@]host:path``) is SSH. git only recognises it
        # when a colon appears before the first slash; a colon after a slash
        # (or no colon at all) denotes a local filesystem path.
        colon = url.find(":")
        slash = url.find("/")
        return colon != -1 and (slash == -1 or colon < slash)

    def _ssh_handshake_jitter(self, repo_path: Path) -> None:
        """Sleep a small random interval before an SSH-backed git operation.

        De-synchronises concurrent worker threads so we avoid opening many
        simultaneous SSH connections to Gerrit, which is a common cause of
        transient "Could not read from remote repository" throttling. The
        sleep is skipped for HTTP(S)/git-protocol remotes, which perform no
        SSH handshake and so gain nothing from jitter.

        Args:
            repo_path: Repository whose origin remote is about to be contacted.
        """
        if self.ssh_jitter_seconds <= 0:
            return
        if not self._remote_uses_ssh(self._get_remote_url(repo_path)):
            return
        time.sleep(random.uniform(0, self.ssh_jitter_seconds))

    def _fix_upstream_tracking(self, repo_path: Path, result: RefreshResult) -> bool:
        """Fix upstream tracking by setting it to origin/<branch>.

        Args:
            repo_path: Repository path
            result: Result object with current branch info

        Returns:
            True if fixed successfully
        """
        if not result.current_branch:
            return False

        try:
            # Check if origin/<branch> exists
            check_result = subprocess.run(
                ["git", "rev-parse", "--verify", f"origin/{result.current_branch}"],
                cwd=repo_path,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=5,
                check=False,
            )

            if check_result.returncode != 0:
                logger.debug(
                    f"Remote branch origin/{result.current_branch} does not exist"
                )
                return False

            # Set upstream tracking
            upstream_result = subprocess.run(
                [
                    "git",
                    "branch",
                    f"--set-upstream-to=origin/{result.current_branch}",
                    result.current_branch,
                ],
                cwd=repo_path,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=10,
                check=False,
            )

            if upstream_result.returncode == 0:
                logger.debug(
                    f"Set upstream tracking for '{result.current_branch}' to 'origin/{result.current_branch}'"
                )
                return True
            else:
                logger.debug(f"Failed to set upstream: {upstream_result.stderr}")
                return False

        except Exception as e:
            logger.debug(f"Failed to fix upstream tracking: {e}")
            return False

    def _pop_stash(self, repo_path: Path) -> bool:
        """Pop stashed changes.

        ``git stash pop`` can exit non-zero even when the working-tree changes
        were applied and the stash entry was dropped. The most common cause in
        practice is a submodule gitlink whose status reporting produces a
        non-zero exit even though nothing failed (e.g. a repository with a
        dirty or advanced submodule pointer). A genuine failure (a merge
        conflict) leaves the stash entry in place, so we treat a dropped stash
        as success regardless of the exit status.

        Args:
            repo_path: Repository path

        Returns:
            True if pop succeeded (changes applied and stash dropped)
        """
        try:
            before = self._stash_count(repo_path)
            result = subprocess.run(
                ["git", "stash", "pop"],
                cwd=repo_path,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=30,
                check=False,
            )

            if result.returncode == 0:
                return True

            # Non-zero exit: fall back to checking whether the stash entry was
            # actually consumed. If it was, the changes were applied and the
            # non-zero status is spurious (typically submodule status noise).
            after = self._stash_count(repo_path)
            if before > 0 and 0 <= after < before:
                logger.debug(
                    "Stash applied despite non-zero git exit "
                    "(likely submodule status noise)"
                )
                return True

            return False

        except Exception as e:
            logger.debug(f"Failed to pop stash: {e}")
            return False

    def _stash_count(self, repo_path: Path) -> int:
        """Return the number of entries in the repository's stash list.

        Args:
            repo_path: Repository path

        Returns:
            Number of stash entries, or -1 if the count could not be
            determined.
        """
        try:
            result = subprocess.run(
                ["git", "stash", "list"],
                cwd=repo_path,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=30,
                check=False,
            )
            if result.returncode != 0:
                return -1
            return sum(1 for line in result.stdout.splitlines() if line.strip())
        except Exception as e:
            logger.debug(f"Failed to count stash entries: {e}")
            return -1

    def _build_git_environment(self) -> dict[str, str]:
        """Build environment for Git operations.

        Returns:
            Environment dictionary
        """
        env = os.environ.copy()

        # Add Git SSH command if config is provided, otherwise use safe defaults
        if self.config and self.config.git_ssh_command:
            env["GIT_SSH_COMMAND"] = self.config.git_ssh_command
        else:
            # SSH Configuration Trade-offs:
            #
            # We explicitly disable SSH multiplexing (ControlMaster=no) for thread safety.
            # This prevents race conditions when multiple threads connect to the same host
            # simultaneously, which can cause:
            # - Socket file conflicts in ~/.ssh/
            # - Connection hangs or failures
            # - Unpredictable behavior in parallel operations
            #
            # PERFORMANCE TRADE-OFF:
            # Disabling multiplexing means each git operation requires a new SSH handshake,
            # adding ~100-500ms latency per operation. However, in practice:
            # - Most operations are I/O bound (git fetch/pull), not connection-bound
            # - Parallel execution across multiple repos still provides significant speedup
            # - The reliability gain outweighs the connection overhead
            # - Real-world testing shows acceptable performance for typical use cases
            #
            # Alternative approaches considered:
            # - Connection pooling: Complex to implement, would require shared state
            # - Single-threaded SSH: Eliminates parallelism benefits entirely
            # - Master socket per thread: Still has filesystem race conditions
            #
            # Current configuration prioritizes reliability and thread safety over
            # optimal SSH connection reuse. If performance becomes an issue, consider:
            # - Using HTTPS instead of SSH (no connection multiplexing issues)
            # - Increasing thread count to compensate for per-connection overhead
            # - Custom connection pooling implementation (significant complexity)
            ssh_opts = [
                "ssh",
                "-o",
                "BatchMode=yes",
                "-o",
                "ControlMaster=no",  # Disable multiplexing for thread safety
                "-o",
                "ConnectTimeout=10",
                "-o",
                "ServerAliveInterval=5",
                "-o",
                "ServerAliveCountMax=3",
                "-o",
                "ConnectionAttempts=2",
                "-o",
                "StrictHostKeyChecking=accept-new",
            ]
            env["GIT_SSH_COMMAND"] = " ".join(ssh_opts)

        # Disable terminal prompts
        env["GIT_TERMINAL_PROMPT"] = "0"

        return env

    def _analyze_git_error(
        self, process_result: subprocess.CompletedProcess[str], operation: str
    ) -> str:
        """Analyze Git error output and generate meaningful error message.

        Args:
            process_result: Completed process result
            operation: Git operation name (fetch/pull)

        Returns:
            Error message string
        """
        stderr = process_result.stderr.lower()
        stdout = process_result.stdout.lower()
        combined = stderr + stdout

        # Network errors
        if any(
            phrase in combined
            for phrase in [
                "could not resolve host",
                "failed to connect",
                "connection timed out",
                "connection refused",
            ]
        ):
            return f"Network error during {operation}"

        # Authentication errors. Checked BEFORE transient SSH errors because
        # Gerrit prints a generic "Could not read from remote repository" line
        # for auth failures too; the "permission denied"/"publickey" markers
        # disambiguate a real auth failure from transient throttling.
        if any(phrase in combined for phrase in _AUTH_ERROR_PATTERNS):
            return f"Authentication error during {operation}"

        # Repository not found. Checked BEFORE the transient SSH patterns
        # because a missing repository also produces the generic "could not
        # read from remote repository" line; without this ordering a
        # permanently missing repo would be misreported as a transient network
        # error and needlessly retried.
        if any(phrase in combined for phrase in _NOT_FOUND_GIT_ERROR_PATTERNS):
            return f"Repository not found during {operation}"

        # Transient SSH / connection failures (e.g. Gerrit throttling a burst of
        # concurrent connections). Reported as a network error so the retry
        # logic treats them as retryable.
        if any(phrase in combined for phrase in _TRANSIENT_GIT_ERROR_PATTERNS):
            return f"Network error during {operation}"

        # Diverging branches: local commits prevent a fast-forward-only update.
        # git's wording ("Diverging branches can't be fast-forwarded") does not
        # contain "non-fast-forward", so it must be matched explicitly.
        if any(phrase in combined for phrase in _DIVERGED_BRANCH_PATTERNS):
            return (
                f"Diverging branches during {operation}: local commits differ "
                f"from upstream; use --force-hard to reset to the remote"
            )

        # Merge conflicts
        if "conflict" in combined:
            return f"Merge conflicts during {operation}"

        # Non-fast-forward
        if "non-fast-forward" in combined or "rejected" in combined:
            return f"Non-fast-forward update rejected during {operation}"

        # Generic error
        error_output = process_result.stderr.strip() or process_result.stdout.strip()
        if error_output:
            # Take first line of error
            first_line = error_output.split("\n")[0]
            return f"Git {operation} failed: {first_line}"

        return f"Git {operation} failed with exit code {process_result.returncode}"

    def _is_auth_git_error(
        self, process_result: subprocess.CompletedProcess[str]
    ) -> bool:
        """Determine if a failed Git result looks like an authentication error.

        A throttled Gerrit can surface a transient connection-limit drop as a
        "Permission denied (publickey)" rejection, so auth-classified errors are
        retried a small, bounded number of times (see
        ``_MAX_AUTH_RETRY_ATTEMPTS``) rather than treated as immediately fatal.

        Args:
            process_result: Completed process result

        Returns:
            True if the failure carries authentication markers
        """
        combined = (process_result.stderr + process_result.stdout).lower()

        # Missing repositories are never auth errors. Check first so a
        # permanently absent repo is not misclassified as a retryable auth
        # throttle.
        if any(pattern in combined for pattern in _NOT_FOUND_GIT_ERROR_PATTERNS):
            return False

        return any(pattern in combined for pattern in _AUTH_ERROR_PATTERNS)

    def _raise_for_retryable_git_error(
        self, process_result: subprocess.CompletedProcess[str], error_msg: str
    ) -> None:
        """Raise the appropriate retryable error for a failed Git result.

        Raises :class:`RefreshAuthError` for auth-style failures (which get a
        small, bounded retry budget) and :class:`RefreshError` for transient
        network/SSH failures (full retry budget). Returns normally when the
        failure is not retryable, letting the caller treat it as a hard
        failure.

        Args:
            process_result: Completed process result
            error_msg: Human-readable error message for the raised exception

        Raises:
            RefreshAuthError: If the failure looks like an auth error
            RefreshError: If the failure is a retryable transient error
        """
        if self._is_auth_git_error(process_result):
            raise RefreshAuthError(error_msg)
        if self._is_retryable_git_error(process_result):
            raise RefreshError(error_msg)

    def _is_retryable_git_error(
        self, process_result: subprocess.CompletedProcess[str]
    ) -> bool:
        """Determine if a Git error is retryable.

        Args:
            process_result: Completed process result

        Returns:
            True if error is retryable
        """
        stderr = process_result.stderr.lower()
        stdout = process_result.stdout.lower()
        combined = stderr + stdout

        # Authentication / authorization failures are never retryable. Check
        # these FIRST: Gerrit prints a generic "could not read from remote
        # repository" line (which also appears for transient throttling) on real
        # auth failures, so the "permission denied"/"publickey" markers are what
        # distinguish them and must take precedence.
        if any(pattern in combined for pattern in _AUTH_ERROR_PATTERNS):
            return False

        # Missing repositories are never retryable. Check BEFORE the transient
        # patterns: Gerrit/GitHub also print "could not read from remote
        # repository" when a project does not exist, so without this ordering a
        # permanently missing repo would match a transient pattern and be
        # retried pointlessly.
        if any(pattern in combined for pattern in _NOT_FOUND_GIT_ERROR_PATTERNS):
            return False

        # Retryable: network and transient SSH handshake failures. The transient
        # SSH patterns (e.g. "could not read from remote repository", "early
        # EOF", "kex_exchange_identification") cover Gerrit throttling a burst of
        # concurrent connections, which succeeds on retry.
        retryable_patterns = [
            "could not resolve host",
            "failed to connect",
            "connection timed out",
            "connection refused",
            "temporary failure",
            "try again",
            *_TRANSIENT_GIT_ERROR_PATTERNS,
        ]

        for pattern in retryable_patterns:
            if pattern in combined:
                return True

        # Non-retryable: conflicts, divergence, etc. (missing repositories are
        # handled earlier, before the transient-pattern check).
        non_retryable_patterns = [
            "authentication failed",
            "conflict",
            "non-fast-forward",
            "rejected",
            *_DIVERGED_BRANCH_PATTERNS,
        ]

        for pattern in non_retryable_patterns:
            if pattern in combined:
                return False

        # Default: do not retry on unknown errors (conservative approach)
        # Only retry on explicitly recognized transient errors
        return False

    def _is_retryable_error(self, error_msg: str) -> bool:
        """Determine if an error message indicates a retryable error.

        Args:
            error_msg: Error message

        Returns:
            True if error is retryable
        """
        error_lower = error_msg.lower()

        retryable_patterns = [
            "network error",
            "timeout",
            "connection",
            "temporary",
        ]

        return any(pattern in error_lower for pattern in retryable_patterns)

    def _calculate_adaptive_delay(self, attempt: int) -> float:
        """Calculate adaptive delay for retry.

        Args:
            attempt: Current attempt number (1-based)

        Returns:
            Delay in seconds
        """
        base_delay = self.retry_policy.base_delay
        factor = self.retry_policy.factor
        max_delay = self.retry_policy.max_delay

        # Exponential backoff
        delay = base_delay * (factor ** (attempt - 1))
        delay = min(delay, max_delay)

        # Add jitter if enabled
        if self.retry_policy.jitter:
            # Full jitter: pick a random point in [0, delay]. This
            # de-synchronises retries from a burst of workers that failed
            # together (e.g. a Gerrit SSH throttle), preventing them from
            # re-colliding on the next attempt. A small floor keeps a minimum
            # spacing between attempts.
            delay = max(0.1, random.uniform(0.0, delay))

        return delay

    def _count_pulled_commits(self, output: str) -> int:
        """Count commits pulled from output.

        Note: This is an approximation based on git pull output.
        Returns number of repositories that received commits, not total commit count.
        Actual commit counting would require additional git commands.

        Args:
            output: Git pull output

        Returns:
            1 if commits were pulled, 0 otherwise (repository count, not commit count)
        """
        # Look for patterns like:
        # "Updating abc123..def456"
        # "Fast-forward"
        # "1 file changed, 2 insertions(+), 3 deletions(-)"  # noqa: ERA001

        if "Already up to date" in output or "Already up-to-date" in output:
            return 0

        # Try to find commit range
        match = re.search(r"Updating\s+([0-9a-f]+)\.\.([0-9a-f]+)", output)
        if match:
            # Indicates at least one commit was pulled
            # (Actual count would require: git rev-list --count old..new)
            return 1

        # Look for "Fast-forward" or merge commit messages
        if "Fast-forward" in output or "Merge made" in output:
            return 1

        return 0

    def _count_fetched_commits(self, output: str) -> int:
        """Count commits fetched from output.

        Args:
            output: Git fetch output

        Returns:
            Number of commits fetched (approximate)
        """
        # Git fetch output shows updated refs
        # Count lines with "->" indicating ref updates
        count = len(re.findall(r"->\s+\S+", output))
        return count if count > 0 else 0

    def _count_changed_files(self, output: str) -> int:
        """Count changed files from output.

        Args:
            output: Git pull output

        Returns:
            Number of files changed
        """
        # Look for pattern like "1 file changed" or "2 files changed"
        match = re.search(r"(\d+)\s+files?\s+changed", output)
        if match:
            return int(match.group(1))

        return 0

    def _get_project_name(self, repo_path: Path) -> str:
        """Get project name from repository path.

        Args:
            repo_path: Repository path

        Returns:
            Project name (directory name)
        """
        return repo_path.name
