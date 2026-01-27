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
from pathlib import Path
from typing import Any

from gerrit_clone.git_utils import is_git_repository
from gerrit_clone.logging import get_logger
from gerrit_clone.models import Config, RefreshResult, RefreshStatus, RetryPolicy

logger = get_logger(__name__)


class RefreshError(Exception):
    """Base exception for refresh operations."""


class RefreshTimeoutError(RefreshError):
    """Raised when refresh operation times out."""


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
        self.force = force

    def refresh_repository(self, repo_path: Path) -> RefreshResult:
        """Refresh a single repository.

        Args:
            repo_path: Path to repository root

        Returns:
            RefreshResult with operation details
        """
        started_at = datetime.now(UTC)
        project_name = self._get_project_name(repo_path)

        # Initialize result object
        result = RefreshResult(
            path=repo_path,
            project_name=project_name,
            status=RefreshStatus.PENDING,
            started_at=started_at,
            first_started_at=started_at,
        )

        try:
            # Validate it's a Git repository
            if not self._is_git_repository(repo_path):
                result.status = RefreshStatus.NOT_GIT_REPO
                result.error_message = "Not a Git repository"
                result.completed_at = datetime.now(UTC)
                result.duration_seconds = (
                    result.completed_at - started_at
                ).total_seconds()
                logger.debug(f"⊘ {project_name}: Not a Git repository")
                return result

            # Get repository state
            state = self._check_repository_state(repo_path)
            result.current_branch = state.get("branch")
            result.detached_head = state.get("detached_head", False)
            result.had_uncommitted_changes = state.get("has_uncommitted", False)

            # Get remote URL
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
                        logger.debug(f"🔧 {project_name}: On Gerrit meta/config branch, switching to code branch")
                    else:
                        logger.debug(f"🔧 {project_name}: Fixing detached HEAD state")

                    if self._fix_detached_head(repo_path, result):
                        # Re-check state after fix
                        state = self._check_repository_state(repo_path)
                        result.current_branch = state.get("branch")
                        result.detached_head = state.get("detached_head", False)
                        logger.debug(f"✓ {project_name}: Checked out branch '{result.current_branch}'")
                    else:
                        # Check if this is a meta-only repo (parent project)
                        if result.error_message and "meta-only" in result.error_message:
                            result.status = RefreshStatus.SKIPPED
                            result.completed_at = datetime.now(UTC)
                            result.duration_seconds = (
                                result.completed_at - started_at
                            ).total_seconds()
                            logger.debug(f"⊘ {project_name}: Skipping Gerrit parent project (no code branches)")
                            return result
                        else:
                            result.status = RefreshStatus.FAILED
                            result.error_message = result.error_message or "Failed to fix detached HEAD state"
                            result.completed_at = datetime.now(UTC)
                            result.duration_seconds = (
                                result.completed_at - started_at
                            ).total_seconds()
                            logger.error(f"❌ {project_name}: Failed to fix detached HEAD")
                            return result

                # Fix upstream tracking
                if not state.get("has_upstream", False) and result.current_branch:
                    logger.debug(f"🔧 {project_name}: Fixing upstream tracking for '{result.current_branch}'")
                    if self._fix_upstream_tracking(repo_path, result):
                        # Re-check state after fix
                        state = self._check_repository_state(repo_path)
                        result.current_branch = state.get("branch")
                        result.detached_head = state.get("detached_head", False)
                        result.had_uncommitted_changes = state.get("has_uncommitted", False)
                        logger.debug(f"✓ {project_name}: Set upstream tracking")
                    else:
                        logger.warning(f"⚠️ {project_name}: Could not set upstream, will try default branch")
                        # Try switching to default branch as fallback
                        if self._fix_detached_head(repo_path, result):
                            # Update state after successful default branch checkout
                            state = self._check_repository_state(repo_path)
                            result.current_branch = state.get("branch")
                            result.detached_head = state.get("detached_head", False)
                            result.had_uncommitted_changes = state.get("has_uncommitted", False)
                            logger.debug(f"✓ {project_name}: Switched to default branch '{result.current_branch}'")
                        else:
                            # Both upstream fix and default branch checkout failed
                            result.status = RefreshStatus.FAILED
                            result.error_message = "Failed to fix upstream tracking and could not switch to default branch"
                            result.completed_at = datetime.now(UTC)
                            result.duration_seconds = (
                                result.completed_at - started_at
                            ).total_seconds()
                            logger.error(f"❌ {project_name}: Could not fix repository state")
                            return result

                # Always stash in force mode
                if result.had_uncommitted_changes:
                    logger.debug(f"💾 {project_name}: Force stashing uncommitted changes")
                    if self._stash_changes(repo_path):
                        result.stash_created = True
                    else:
                        result.status = RefreshStatus.FAILED
                        result.error_message = "Failed to stash uncommitted changes in force mode"
                        result.completed_at = datetime.now(UTC)
                        result.duration_seconds = (
                            result.completed_at - started_at
                        ).total_seconds()
                        logger.error(f"❌ {project_name}: Failed to stash changes")
                        return result
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

                # Handle branch without upstream tracking
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
                    if self._stash_changes(repo_path):
                        result.stash_created = True
                        logger.debug(f"💾 {project_name}: Stashed uncommitted changes")
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

            # Update status to refreshing
            result.status = RefreshStatus.REFRESHING

            # Execute refresh with retry logic
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

                # Pop stash if we created one
                if result.stash_created:
                    if self._pop_stash(repo_path):
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
            result.duration_seconds = (
                result.completed_at - started_at
            ).total_seconds()
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
        attempt = 0

        while attempt < max_attempts:
            attempt += 1
            try:
                success = self._perform_refresh(repo_path, result)
                if success:
                    return True

                # If we get here, refresh failed but didn't raise exception
                # (non-retryable error)
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

        try:
            if self.fetch_only:
                # Fetch only, don't merge
                success = self._execute_git_fetch(repo_path, result)
            else:
                # Fetch and merge/rebase
                success = self._execute_git_pull(repo_path, result)

            attempt_duration = (datetime.now(UTC) - attempt_start).total_seconds()
            result.last_attempt_duration = attempt_duration

            return success

        except subprocess.TimeoutExpired:
            error_msg = f"Git operation timeout after {self.timeout}s"
            result.error_message = error_msg
            raise RefreshTimeoutError(error_msg)

        except Exception as e:
            error_msg = f"Unexpected error during refresh: {e}"
            result.error_message = error_msg
            raise RefreshError(error_msg)

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
                result.error_message = error_msg

                if self._is_retryable_git_error(process_result):
                    raise RefreshError(error_msg)
                else:
                    return False

        except subprocess.TimeoutExpired:
            raise RefreshTimeoutError(f"Fetch timeout after {self.timeout}s")

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
                timeout=self.timeout,
                check=False,
            )

            if process_result.returncode == 0:
                # Parse pull output to count commits and files
                output = process_result.stdout + process_result.stderr
                result.commits_pulled = self._count_pulled_commits(output)
                result.files_changed = self._count_changed_files(output)
                return True
            else:
                error_msg = self._analyze_git_error(process_result, "pull")
                result.error_message = error_msg

                # Check for conflicts
                if "CONFLICT" in process_result.stdout or "CONFLICT" in process_result.stderr:
                    result.status = RefreshStatus.CONFLICTS
                    logger.error(f"⚠️ {result.project_name}: Merge conflicts detected")
                    return False

                if self._is_retryable_git_error(process_result):
                    raise RefreshError(error_msg)
                else:
                    return False

        except subprocess.TimeoutExpired:
            raise RefreshTimeoutError(f"Pull timeout after {self.timeout}s")

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
        for host in gerrit_hosts:
            if host in remote_url.lower():
                return True

        return False

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
            # Get current branch
            branch_result = subprocess.run(
                ["git", "rev-parse", "--abbrev-ref", "HEAD"],
                cwd=repo_path,
                capture_output=True,
                text=True,
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
                        timeout=5,
                        check=False,
                    )

                    if upstream_result.returncode == 0:
                        state["has_upstream"] = True

            # Check for uncommitted changes
            status_result = subprocess.run(
                ["git", "status", "--porcelain"],
                cwd=repo_path,
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
            )

            if status_result.returncode == 0:
                state["has_uncommitted"] = bool(status_result.stdout.strip())

        except Exception as e:
            logger.debug(f"Failed to check repository state: {e}")

        return state

    def _stash_changes(self, repo_path: Path) -> bool:
        """Stash uncommitted changes.

        Args:
            repo_path: Repository path

        Returns:
            True if stash succeeded
        """
        try:
            result = subprocess.run(
                ["git", "stash", "push", "--include-untracked", "-m", "gerrit-clone refresh auto-stash"],
                cwd=repo_path,
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )

            return result.returncode == 0

        except Exception as e:
            logger.debug(f"Failed to stash changes: {e}")
            return False

    def _is_on_meta_config(self, repo_path: Path) -> bool:
        """Check if repository is currently on Gerrit's meta/config branch.

        Args:
            repo_path: Repository path

        Returns:
            True if on meta/config branch
        """
        try:
            # Get the full symbolic ref
            result = subprocess.run(
                ["git", "symbolic-ref", "-q", "HEAD"],
                cwd=repo_path,
                capture_output=True,
                text=True,
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
            # List all remote heads (branches)
            result = subprocess.run(
                ["git", "ls-remote", "--heads", "origin"],
                cwd=repo_path,
                capture_output=True,
                text=True,
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
                    timeout=10,
                    check=False,
                )

                if meta_result.returncode == 0 and meta_result.stdout.strip():
                    logger.debug(f"{repo_path.name}: Confirmed as Gerrit parent project (has meta/config, no heads)")
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
            # This works even if we haven't fetched recently
            ls_remote_result = subprocess.run(
                ["git", "ls-remote", "--symref", "origin", "HEAD"],
                cwd=repo_path,
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )

            if ls_remote_result.returncode == 0:
                # Parse output like "ref: refs/heads/master	HEAD"
                for line in ls_remote_result.stdout.strip().split('\n'):
                    if line.startswith("ref:"):
                        ref = line.split()[1]
                        if ref.startswith("refs/heads/"):
                            branch_name = ref.replace("refs/heads/", "")
                            # Verify this isn't a Gerrit meta ref
                            if not branch_name.startswith("meta/"):
                                logger.debug(f"Found default branch via ls-remote: {branch_name}")
                                return branch_name

            # Try to get origin/HEAD symbolic ref
            result = subprocess.run(
                ["git", "symbolic-ref", "refs/remotes/origin/HEAD"],
                cwd=repo_path,
                capture_output=True,
                text=True,
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
                logger.debug(f"🔧 {repo_path.name}: Detected Gerrit meta/config branch, switching to code branch")

            # Fetch remote to ensure we have latest branch info
            # This is crucial for repos that might not have been fetched recently
            fetch_result = subprocess.run(
                ["git", "fetch", "--quiet", "origin"],
                cwd=repo_path,
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )

            if fetch_result.returncode != 0:
                logger.debug(f"Fetch failed but continuing: {fetch_result.stderr}")

            # Check if this is a Gerrit parent project (meta-only, no code branches)
            if self._is_meta_only_repo(repo_path):
                logger.debug(f"{repo_path.name}: Gerrit parent project (meta-only), no code branches to refresh")
                # Update result to indicate this is a skip, not a failure
                result.error_message = "Gerrit parent project (meta-only, no code branches)"
                return False

            # Get default branch
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
                timeout=30,
                check=False,
            )

            if checkout_result.returncode == 0:
                logger.debug(f"Checked out branch '{default_branch}' in {repo_path.name}")

                # Set upstream tracking if not already set
                set_upstream_result = subprocess.run(
                    ["git", "branch", f"--set-upstream-to=origin/{default_branch}", default_branch],
                    cwd=repo_path,
                    capture_output=True,
                    text=True,
                    timeout=10,
                    check=False,
                )

                if set_upstream_result.returncode == 0:
                    logger.debug(f"Set upstream tracking for '{default_branch}'")

                return True
            else:
                logger.debug(f"Failed to checkout '{default_branch}': {checkout_result.stderr}")
                return False

        except Exception as e:
            logger.debug(f"Failed to fix detached HEAD: {e}")
            return False

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
                timeout=5,
                check=False,
            )

            if check_result.returncode != 0:
                logger.debug(f"Remote branch origin/{result.current_branch} does not exist")
                return False

            # Set upstream tracking
            upstream_result = subprocess.run(
                ["git", "branch", f"--set-upstream-to=origin/{result.current_branch}", result.current_branch],
                cwd=repo_path,
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )

            if upstream_result.returncode == 0:
                logger.debug(f"Set upstream tracking for '{result.current_branch}' to 'origin/{result.current_branch}'")
                return True
            else:
                logger.debug(f"Failed to set upstream: {upstream_result.stderr}")
                return False

        except Exception as e:
            logger.debug(f"Failed to fix upstream tracking: {e}")
            return False

    def _pop_stash(self, repo_path: Path) -> bool:
        """Pop stashed changes.

        Args:
            repo_path: Repository path

        Returns:
            True if pop succeeded
        """
        try:
            result = subprocess.run(
                ["git", "stash", "pop"],
                cwd=repo_path,
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )

            return result.returncode == 0

        except Exception as e:
            logger.debug(f"Failed to pop stash: {e}")
            return False

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
                "-o", "BatchMode=yes",
                "-o", "ControlMaster=no",  # Disable multiplexing for thread safety
                "-o", "ConnectTimeout=10",
                "-o", "ServerAliveInterval=5",
                "-o", "ServerAliveCountMax=3",
                "-o", "ConnectionAttempts=2",
                "-o", "StrictHostKeyChecking=accept-new",
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

        # Authentication errors
        if any(
            phrase in combined
            for phrase in ["permission denied", "authentication failed", "could not read"]
        ):
            return f"Authentication error during {operation}"

        # Repository errors
        if "repository not found" in combined or "does not exist" in combined:
            return f"Repository not found during {operation}"

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

        # Retryable: network errors
        retryable_patterns = [
            "could not resolve host",
            "failed to connect",
            "connection timed out",
            "connection refused",
            "connection reset",
            "broken pipe",
            "temporary failure",
            "try again",
        ]

        for pattern in retryable_patterns:
            if pattern in combined:
                return True

        # Non-retryable: authentication, conflicts, etc.
        non_retryable_patterns = [
            "permission denied",
            "authentication failed",
            "conflict",
            "non-fast-forward",
            "rejected",
            "repository not found",
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
            jitter_factor = 0.2  # 20% jitter
            jitter = random.uniform(-jitter_factor * delay, jitter_factor * delay)
            delay = max(0.1, delay + jitter)

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
        # "1 file changed, 2 insertions(+), 3 deletions(-)"

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
