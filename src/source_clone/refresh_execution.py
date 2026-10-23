# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""Fetch/pull execution and adaptive retry for refresh operations.

Fourth layer of the :class:`~source_clone.refresh_worker.RefreshWorker` mixin
stack. It owns the network-facing half of a refresh: building and running the
``git fetch`` / ``git pull`` commands, translating their exit status into the
refresh exception taxonomy, and driving the retry loop with its two separate
budgets (a full network budget and a smaller auth budget).
"""

from __future__ import annotations

import random
import subprocess
import time
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from source_clone.logging import get_logger
from source_clone.models import RefreshStatus
from source_clone.refresh_branch_repair import BranchRepairMixin
from source_clone.refresh_git_env import run_git
from source_clone.refresh_output import (
    GitOutputAnalysisMixin,
    RefreshAuthError,
    RefreshError,
    RefreshTimeoutError,
)
from source_clone.subprocess_tracking import ProcessAbandonedError

if TYPE_CHECKING:
    from pathlib import Path

    from source_clone.models import RefreshResult, RetryPolicy

logger = get_logger(__name__)

# Auth-classified failures are normally fatal, but a Gerrit server throttling a
# burst of concurrent SSH connections can reject a valid key with "Permission
# denied (publickey)" while it drops the connection. Retrying such a failure a
# small, bounded number of times recovers these transient throttles, whereas a
# genuinely misconfigured key still fails quickly instead of consuming the full
# network-retry budget across every repository.
_MAX_AUTH_RETRY_ATTEMPTS = 2


class RefreshExecutionMixin(BranchRepairMixin, GitOutputAnalysisMixin):
    """Execution of the git network operation, with adaptive retries."""

    # Supplied by RefreshWorker.__init__; declared here because this layer
    # reads them.
    retry_policy: RetryPolicy
    timeout: int
    fetch_only: bool
    prune: bool
    strategy: str

    def _execute_adaptive_refresh(
        self, repo_path: Path, result: RefreshResult, *, bare: bool = False
    ) -> bool:
        """Execute refresh with adaptive retry logic.

        Args:
            repo_path: Repository path
            result: Result object to update
            bare: Whether the repository is bare, and so can only be fetched

        Returns:
            True if refresh succeeded, False otherwise

        Raises:
            ProcessAbandonedError: If the batch was abandoned.  Never
                retried: every later launch would be refused, and a retry
                that slipped past the refusal would outlive the batch.
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
                success = self._perform_refresh(repo_path, result, bare=bare)
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

            except RefreshTimeoutError as e:
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
                    # Record which operation timed out and after how
                    # long, as every sibling branch does. Without it
                    # _apply_refresh_outcome substitutes "Refresh failed
                    # for unknown reason", which cannot distinguish a
                    # slow server from a hung fetch.
                    result.error_message = str(e)
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

    def _perform_refresh(
        self, repo_path: Path, result: RefreshResult, *, bare: bool = False
    ) -> bool:
        """Perform the actual refresh operation.

        Args:
            repo_path: Repository path
            result: Result object to update
            bare: Whether the repository is bare, and so can only be fetched

        Returns:
            True if refresh succeeded, False otherwise

        Raises:
            RefreshError: If refresh fails with retryable error
            RefreshTimeoutError: If refresh times out
            ProcessAbandonedError: If the batch was abandoned
        """
        result.attempts += 1
        attempt_start = datetime.now(UTC)

        # Spread out SSH handshakes across concurrent workers to avoid Gerrit
        # throttling a burst of simultaneous connections.
        self._ssh_handshake_jitter(repo_path)

        try:
            if self.fetch_only or bare:
                # Fetch only, don't merge.  A bare repository has no
                # branch to merge into; a mirror's refspec is
                # +refs/*:refs/*, so the fetch alone updates every ref.
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

        except ProcessAbandonedError:
            # Not a refresh failure: wrapped as a RefreshError it would be
            # judged retryable, recorded as this repository's error, or both.
            raise

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

        Raises:
            ProcessAbandonedError: If the batch was abandoned
        """
        cmd = ["git", "fetch"]

        if self.prune:
            cmd.append("--prune")

        cmd.extend(["--all", "--tags"])

        env = self._build_git_environment()

        logger.debug(f"🔄 Fetching {result.project_name}")

        try:
            process_result = run_git(cmd, repo_path, timeout=self.timeout, env=env)

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

        Raises:
            ProcessAbandonedError: If the batch was abandoned
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
            process_result = run_git(cmd, repo_path, timeout=self.timeout, env=env)

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

    def _stamp_completion(self, result: RefreshResult, started_at: datetime) -> None:
        """Record completion metadata on a result that is about to be returned.

        Args:
            result: Result object to stamp
            started_at: Timestamp the refresh began
        """
        result.completed_at = datetime.now(UTC)
        result.duration_seconds = (result.completed_at - started_at).total_seconds()

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
