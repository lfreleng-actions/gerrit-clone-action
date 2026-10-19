# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""Default-branch resolution and branch repair for refresh operations.

Third layer of the :class:`~gerrit_clone.refresh_worker.RefreshWorker` mixin
stack. Everything here is about getting a repository onto a sane, tracked
branch before a fetch/pull is attempted: working out what the default branch
is (locally first, then over the network), recovering from a detached HEAD,
switching away from a feature branch, restoring upstream tracking and — for
force-hard mode — resetting a branch to its upstream ref.

Every step lets :class:`~gerrit_clone.subprocess_tracking.ProcessAbandonedError`
through: reported as an ordinary failure, it would send force mode on to its
fallback repairs, each a further write to a repository the batch gave up on.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from gerrit_clone.logging import get_logger
from gerrit_clone.refresh_git_env import run_git
from gerrit_clone.refresh_repo_state import RepositoryStateMixin
from gerrit_clone.subprocess_tracking import ProcessAbandonedError

if TYPE_CHECKING:
    from pathlib import Path

    from gerrit_clone.models import RefreshResult

logger = get_logger(__name__)


class BranchRepairMixin(RepositoryStateMixin):
    """Default-branch discovery and branch/upstream repair operations."""

    @staticmethod
    def _parse_symref_default_branch(ls_remote_output: str) -> str | None:
        """Extract the default branch from ``git ls-remote --symref`` output.

        Args:
            ls_remote_output: stdout of ``git ls-remote --symref origin HEAD``

        Returns:
            Default branch name, or None if the output names no usable branch
        """
        for line in ls_remote_output.strip().split("\n"):
            if not line.startswith("ref:"):
                continue
            ref = line.split()[1]
            if not ref.startswith("refs/heads/"):
                continue
            branch_name = ref.replace("refs/heads/", "")
            # Verify this isn't a Gerrit meta ref
            if not branch_name.startswith("meta/"):
                return branch_name
        return None

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
            ls_remote_result = run_git(
                ["git", "ls-remote", "--symref", "origin", "HEAD"],
                repo_path,
                timeout=10,
            )

            if ls_remote_result.returncode == 0:
                branch_name = self._parse_symref_default_branch(ls_remote_result.stdout)
                if branch_name is not None:
                    logger.debug(f"Found default branch via ls-remote: {branch_name}")
                    return branch_name

            # Try to get origin/HEAD symbolic ref
            result = run_git(
                ["git", "symbolic-ref", "refs/remotes/origin/HEAD"],
                repo_path,
                timeout=5,
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
                result = run_git(
                    ["git", "ls-remote", "--heads", "origin", branch_name],
                    repo_path,
                    timeout=5,
                )

                if result.returncode == 0 and result.stdout.strip():
                    logger.debug(f"Found branch via ls-remote: {branch_name}")
                    return branch_name

            logger.debug(f"No default branch found for {repo_path.name}")
            return None

        except ProcessAbandonedError:
            raise

        except Exception as e:
            logger.debug(f"Failed to get default branch: {e}")
            return None

    def _get_default_branch_local(self, repo_path: Path) -> str | None:
        """Determine the default branch using only local refs (no network).

        Reads the locally cached ``refs/remotes/origin/HEAD`` symbolic ref, which
        source-clone sets at clone time. Returns None if it is not available so
        callers can decide whether a networked lookup is warranted.

        Args:
            repo_path: Repository path

        Returns:
            Default branch name, or None if it cannot be determined locally
        """
        try:
            result = run_git(
                ["git", "symbolic-ref", "--short", "refs/remotes/origin/HEAD"],
                repo_path,
                timeout=5,
            )
            if result.returncode == 0:
                ref = result.stdout.strip()
                # "origin/master" -> "master"
                branch = ref.split("/", 1)[1] if ref.startswith("origin/") else ref
                if branch and not branch.startswith("meta/"):
                    return branch
            return None
        except ProcessAbandonedError:
            raise
        except Exception as e:
            logger.debug(f"Failed to get local default branch: {e}")
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
            fetch_result = run_git(
                ["git", "fetch", "--quiet", "origin"], repo_path, timeout=30
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
            checkout_result = run_git(
                ["git", "checkout", default_branch], repo_path, timeout=30
            )

            if checkout_result.returncode == 0:
                logger.debug(
                    f"Checked out branch '{default_branch}' in {repo_path.name}"
                )

                # Set upstream tracking if not already set
                set_upstream_result = run_git(
                    [
                        "git",
                        "branch",
                        f"--set-upstream-to=origin/{default_branch}",
                        default_branch,
                    ],
                    repo_path,
                    timeout=10,
                )

                if set_upstream_result.returncode == 0:
                    logger.debug(f"Set upstream tracking for '{default_branch}'")

                return True
            else:
                logger.debug(
                    f"Failed to checkout '{default_branch}': {checkout_result.stderr}"
                )
                return False

        except ProcessAbandonedError:
            raise

        except Exception as e:
            logger.debug(f"Failed to fix detached HEAD: {e}")
            return False

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
            checkout_result = run_git(
                ["git", "checkout", default_branch], repo_path, timeout=30
            )

            if checkout_result.returncode != 0:
                logger.debug(
                    f"Failed to checkout '{default_branch}': {checkout_result.stderr}"
                )
                return False

            # Best-effort: ensure upstream tracking is set for the default branch.
            run_git(
                [
                    "git",
                    "branch",
                    f"--set-upstream-to=origin/{default_branch}",
                    default_branch,
                ],
                repo_path,
                timeout=10,
            )
            return True

        except ProcessAbandonedError:
            raise

        except Exception as e:
            logger.debug(f"Failed to switch to default branch: {e}")
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
            check_result = run_git(
                ["git", "rev-parse", "--verify", f"origin/{result.current_branch}"],
                repo_path,
                timeout=5,
            )

            if check_result.returncode != 0:
                logger.debug(
                    f"Remote branch origin/{result.current_branch} does not exist"
                )
                return False

            # Set upstream tracking
            upstream_result = run_git(
                [
                    "git",
                    "branch",
                    f"--set-upstream-to=origin/{result.current_branch}",
                    result.current_branch,
                ],
                repo_path,
                timeout=10,
            )

            if upstream_result.returncode == 0:
                logger.debug(
                    f"Set upstream tracking for '{result.current_branch}' to 'origin/{result.current_branch}'"
                )
                return True
            else:
                logger.debug(f"Failed to set upstream: {upstream_result.stderr}")
                return False

        except ProcessAbandonedError:
            raise

        except Exception as e:
            logger.debug(f"Failed to fix upstream tracking: {e}")
            return False
