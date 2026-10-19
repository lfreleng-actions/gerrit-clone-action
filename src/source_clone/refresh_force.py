# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""Force-mode repository preparation for refresh operations.

Fifth layer of the :class:`~source_clone.refresh_worker.RefreshWorker` mixin
stack, and the only place that automatically *mutates* a repository before it
is refreshed. Force mode walks a fixed repair sequence — recover a detached
HEAD, return to the default branch, restore upstream tracking, stash a dirty
tree and (for ``--force-hard``) reset to upstream — aborting the refresh as
soon as a step leaves the repository in an unusable state.

Each step returns either the refreshed repository state or a signal that the
caller must stop; the :class:`~source_clone.models.RefreshResult` is fully
populated (including completion timestamps) before any abort signal is
returned.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from source_clone.logging import get_logger
from source_clone.models import RefreshStatus
from source_clone.refresh_execution import RefreshExecutionMixin
from source_clone.refresh_git_env import run_git
from source_clone.refresh_repo_state import StashOutcome
from source_clone.subprocess_tracking import ProcessAbandonedError

if TYPE_CHECKING:
    from datetime import datetime
    from pathlib import Path

    from source_clone.models import RefreshResult

logger = get_logger(__name__)


class ForceModeMixin(RefreshExecutionMixin):
    """Automatic repository repair performed by ``--force`` / ``--force-hard``."""

    # Supplied by RefreshWorker.__init__; declared here because this layer
    # reads it.
    force_hard: bool

    def _prepare_forced_repository(
        self,
        repo_path: Path,
        result: RefreshResult,
        state: dict[str, Any],
        started_at: datetime,
    ) -> bool:
        """Run the force-mode repair sequence against a repository.

        Args:
            repo_path: Repository path
            result: Result object to update
            state: Repository state as most recently observed
            started_at: Timestamp the refresh began, for completion metadata

        Returns:
            True if the repository is ready to refresh, False if the refresh
            must stop and return ``result`` as-is.
        """
        repaired = self._force_fix_detached_head(repo_path, result, state, started_at)
        if repaired is None:
            return False

        state, default_branch = self._force_switch_to_default_branch(
            repo_path, result, repaired
        )

        repaired = self._force_fix_upstream(repo_path, result, state, started_at)
        if repaired is None:
            return False

        if not self._force_stash(repo_path, result, started_at):
            return False

        self._force_hard_reset(repo_path, result, default_branch)
        return True

    def _force_fix_detached_head(
        self,
        repo_path: Path,
        result: RefreshResult,
        state: dict[str, Any],
        started_at: datetime,
    ) -> dict[str, Any] | None:
        """Recover a detached HEAD by checking out the default branch.

        Args:
            repo_path: Repository path
            result: Result object to update
            state: Repository state as most recently observed
            started_at: Timestamp the refresh began, for completion metadata

        Returns:
            The (possibly re-read) repository state, or None if the refresh
            must stop.
        """
        if not result.detached_head:
            return state

        # Check if we're on Gerrit's meta/config branch
        if state.get("on_meta_config", False):
            logger.debug(
                f"🔧 {result.project_name}: On Gerrit meta/config branch, switching to code branch"
            )
        else:
            logger.debug(f"🔧 {result.project_name}: Fixing detached HEAD state")

        if self._fix_detached_head(repo_path, result):
            # Re-check state after fix
            state = self._check_repository_state(repo_path)
            result.current_branch = state.get("branch")
            result.detached_head = state.get("detached_head", False)
            logger.debug(
                f"✓ {result.project_name}: Checked out branch '{result.current_branch}'"
            )
            return state

        # Check if this is a meta-only repo (parent project)
        if result.error_message and "meta-only" in result.error_message:
            result.status = RefreshStatus.SKIPPED
            self._stamp_completion(result, started_at)
            logger.debug(
                f"⊘ {result.project_name}: Skipping Gerrit parent project (no code branches)"
            )
            return None

        result.status = RefreshStatus.FAILED
        result.error_message = (
            result.error_message or "Failed to fix detached HEAD state"
        )
        self._stamp_completion(result, started_at)
        logger.error(f"❌ {result.project_name}: Failed to fix detached HEAD")
        return None

    def _force_switch_to_default_branch(
        self, repo_path: Path, result: RefreshResult, state: dict[str, Any]
    ) -> tuple[dict[str, Any], str | None]:
        """Return to the default branch when parked on a feature branch.

        Force mode refreshes the mainline rather than local feature work. Uses
        a local-only default-branch lookup first to avoid an extra networked
        ls-remote per repository.

        Args:
            repo_path: Repository path
            result: Result object to update
            state: Repository state as most recently observed

        Returns:
            Tuple of the (possibly re-read) repository state and the resolved
            default branch name (None if it could not be determined).
        """
        if not result.current_branch or result.detached_head:
            return state, None

        default_branch = self._get_default_branch_local(repo_path)
        if default_branch is None:
            default_branch = self._get_default_branch(repo_path)
        if not default_branch or result.current_branch == default_branch:
            return state, default_branch

        logger.debug(
            f"🔧 {result.project_name}: Switching from feature branch "
            f"'{result.current_branch}' to default branch '{default_branch}'"
        )
        # Stash first so an unclean tree cannot block checkout.
        if (
            result.had_uncommitted_changes
            and not result.stash_created
            and self._stash_changes(repo_path) is StashOutcome.CREATED
        ):
            result.stash_created = True
            # Record the branch the stash came from so it is not auto-popped
            # onto the default branch after the switch below (that would apply
            # feature-branch work to the wrong branch).
            result.stash_branch = result.current_branch
            # Tree is now clean; clear the dirty flag so the later force-mode
            # stash does not try to re-stash a clean tree if the checkout below
            # fails.
            result.had_uncommitted_changes = False

        if self._switch_to_default_branch(repo_path, default_branch):
            state = self._check_repository_state(repo_path)
            result.current_branch = state.get("branch")
            result.detached_head = state.get("detached_head", False)
            result.had_uncommitted_changes = state.get("has_uncommitted", False)
            logger.debug(
                f"✓ {result.project_name}: Switched to default branch "
                f"'{result.current_branch}'"
            )
        else:
            logger.warning(
                f"⚠️ {result.project_name}: Could not switch to default "
                f"branch '{default_branch}', refreshing current branch"
            )

        return state, default_branch

    def _force_fix_upstream(
        self,
        repo_path: Path,
        result: RefreshResult,
        state: dict[str, Any],
        started_at: datetime,
    ) -> dict[str, Any] | None:
        """Restore upstream tracking, falling back to the default branch.

        Args:
            repo_path: Repository path
            result: Result object to update
            state: Repository state as most recently observed
            started_at: Timestamp the refresh began, for completion metadata

        Returns:
            The (possibly re-read) repository state, or None if the refresh
            must stop.
        """
        if state.get("has_upstream", False) or not result.current_branch:
            return state

        logger.debug(
            f"🔧 {result.project_name}: Fixing upstream tracking for '{result.current_branch}'"
        )
        if self._fix_upstream_tracking(repo_path, result):
            # Re-check state after fix
            state = self._check_repository_state(repo_path)
            result.current_branch = state.get("branch")
            result.detached_head = state.get("detached_head", False)
            result.had_uncommitted_changes = state.get("has_uncommitted", False)
            logger.debug(f"✓ {result.project_name}: Set upstream tracking")
            return state

        logger.warning(
            f"⚠️ {result.project_name}: Could not set upstream, will try default branch"
        )
        # Try switching to default branch as fallback
        if self._fix_detached_head(repo_path, result):
            state = self._check_repository_state(repo_path)
            result.current_branch = state.get("branch")
            result.detached_head = state.get("detached_head", False)
            result.had_uncommitted_changes = state.get("has_uncommitted", False)
            logger.debug(
                f"✓ {result.project_name}: Switched to default branch '{result.current_branch}'"
            )
            return state

        # Both upstream fix and default branch checkout failed
        result.status = RefreshStatus.FAILED
        result.error_message = (
            "Failed to fix upstream tracking and could not switch to default branch"
        )
        self._stamp_completion(result, started_at)
        logger.error(f"❌ {result.project_name}: Could not fix repository state")
        return None

    def _force_stash(
        self, repo_path: Path, result: RefreshResult, started_at: datetime
    ) -> bool:
        """Always stash a dirty working tree in force mode.

        Args:
            repo_path: Repository path
            result: Result object to update
            started_at: Timestamp the refresh began, for completion metadata

        Returns:
            True if the tree is ready to refresh, False if the refresh must
            stop.
        """
        if not result.had_uncommitted_changes:
            return True

        logger.debug(f"💾 {result.project_name}: Force stashing uncommitted changes")
        stash_outcome = self._stash_changes(repo_path)
        if stash_outcome is StashOutcome.CREATED:
            result.stash_created = True
            result.stash_branch = result.current_branch
        elif stash_outcome is StashOutcome.NOTHING_TO_STASH:
            # Nothing git could stash (e.g. a modified submodule gitlink).
            # Not an error and nothing to restore later.
            result.had_uncommitted_changes = False
            logger.debug(
                f"💾 {result.project_name}: Nothing to stash "
                f"(e.g. submodule-only change)"
            )
        else:
            result.status = RefreshStatus.FAILED
            result.error_message = "Failed to stash uncommitted changes in force mode"
            self._stamp_completion(result, started_at)
            logger.error(f"❌ {result.project_name}: Failed to stash changes")
            return False

        return True

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
            upstream_check = run_git(
                [
                    "git",
                    "rev-parse",
                    "--abbrev-ref",
                    f"{result.current_branch}@{{upstream}}",
                ],
                repo_path,
                timeout=5,
            )
            if upstream_check.returncode != 0:
                logger.debug(
                    f"No upstream for '{result.current_branch}', cannot hard reset"
                )
                return False

            reset_result = run_git(
                ["git", "reset", "--hard", f"{result.current_branch}@{{upstream}}"],
                repo_path,
                timeout=30,
            )

            if reset_result.returncode == 0:
                return True

            logger.debug(f"Hard reset failed: {reset_result.stderr}")
            return False

        except ProcessAbandonedError:
            raise

        except Exception as e:
            logger.debug(f"Failed to hard reset to upstream: {e}")
            return False

    def _force_hard_reset(
        self, repo_path: Path, result: RefreshResult, default_branch: str | None
    ) -> None:
        """Discard local commits by hard-resetting to upstream (force-hard).

        Force-hard mode discards any local commits / divergence by
        hard-resetting the default branch to its upstream ref. This is the
        single, explicit way to make local content exactly match the remote;
        the normal pull that follows is then a no-op fast-forward.

        The reset is guarded so it only ever touches the default branch. If the
        default branch could not be determined or the switch to it failed
        earlier, we are still parked on a feature branch; hard-resetting it
        would silently discard local-only commits, which contradicts the
        documented "default branch only" contract. The reset is skipped in that
        case.

        Args:
            repo_path: Repository path
            result: Result object to update
            default_branch: Resolved default branch name, if known
        """
        if not (self.force_hard and result.current_branch):
            return

        on_default_branch = (
            default_branch is not None and result.current_branch == default_branch
        )
        if not on_default_branch:
            logger.warning(
                f"⚠️ {result.project_name}: Not on the default branch "
                f"(on '{result.current_branch}', default "
                f"'{default_branch}'); skipping hard reset to avoid "
                f"discarding local commits"
            )
        elif self._reset_to_upstream(repo_path, result):
            result.hard_reset = True
            logger.debug(
                f"🧨 {result.project_name}: Hard reset '{result.current_branch}' "
                f"to upstream"
            )
        else:
            logger.warning(
                f"⚠️ {result.project_name}: Hard reset to upstream failed "
                f"(no upstream?), continuing with pull"
            )
