# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""Repository state inspection and stash handling for refresh operations.

Second layer of the :class:`~gerrit_clone.refresh_worker.RefreshWorker` mixin
stack. It answers "what state is this repository in?" (bare or working tree;
for a working tree its branch, detached HEAD, uncommitted changes and Gerrit
meta refs) and owns the stash push/pop lifecycle used to move an unclean tree
out of the way, including the subtle git exit-status semantics documented on
:class:`StashOutcome` and ``_pop_stash``.

Probes let :class:`~gerrit_clone.subprocess_tracking.ProcessAbandonedError`
through rather than answering it: a guessed answer would misreport the
repository, or carry the refresh on towards a stash or a fetch.
"""

from __future__ import annotations

from enum import Enum
from typing import TYPE_CHECKING, Any

from gerrit_clone.git_utils import is_git_repository
from gerrit_clone.logging import get_logger
from gerrit_clone.refresh_git_env import GitEnvironmentMixin, run_git
from gerrit_clone.subprocess_tracking import ProcessAbandonedError

if TYPE_CHECKING:
    from pathlib import Path

    from gerrit_clone.models import RefreshResult

logger = get_logger(__name__)


def _updates_a_ref(refspec: str) -> bool:
    """Whether fetching *refspec* stores anything but ``FETCH_HEAD``.

    Only a positive refspec with a destination does: ``src:dst``, with
    ``+`` optional.  An empty one, a bare source, or a negative ``^``
    one leaves every ref where it was, and ``git fetch`` still exits 0.
    """
    spec = refspec.strip().removeprefix("+")
    if spec.startswith("^"):
        return False
    return bool(spec.partition(":")[2])


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


class RepositoryStateMixin(GitEnvironmentMixin):
    """Inspection of local repository state and stash management."""

    def _is_git_repository(self, path: Path) -> bool:
        """Check if path is a valid Git repository (regular or bare).

        Args:
            path: Path to check

        Returns:
            True if path is a Git repository (regular or bare)
        """
        # Use shared utility that detects both regular and bare repositories
        return is_git_repository(path)

    def _is_bare_repository(self, repo_path: Path) -> bool:
        """Whether *repo_path* is a bare repository, as a default clone is.

        A bare repository has no working tree, so none of the branch,
        upstream or stash handling applies to it.

        Args:
            repo_path: Repository path

        Returns:
            True if git reports the repository as bare
        """
        try:
            result = run_git(
                ["git", "rev-parse", "--is-bare-repository"], repo_path, timeout=5
            )
        except ProcessAbandonedError:
            raise
        except Exception as e:
            logger.debug(f"Failed to check whether repository is bare: {e}")
            return False
        return result.returncode == 0 and result.stdout.strip() == "true"

    def _bare_refresh_obstacle(self, repo_path: Path) -> str | None:
        """Why fetching would not update a bare repository, if it would not.

        A mirror clone fetches ``+refs/*:refs/*``, so a fetch brings
        every ref up to date.  A bare repository with no fetch refspec on
        any remote -- ``git clone --bare`` leaves none -- would fetch into
        ``FETCH_HEAD`` alone and update nothing, and reporting that as a
        refresh would be the very silent success this is here to avoid.
        So would a refspec that names no destination: an empty one,
        ``refs/heads/main`` alone, or a negative ``^`` one.  Every remote
        counts, since the fetch is ``--all``.

        Args:
            repo_path: Path to a bare repository

        Returns:
            The reason it cannot be refreshed, or ``None`` if it can.
        """
        try:
            result = run_git(
                ["git", "config", "--get-regexp", r"^remote\..*\.fetch$"],
                repo_path,
                timeout=5,
            )
        except ProcessAbandonedError:
            raise
        except Exception as e:
            return f"Could not read the fetch refspec: {e}"
        if result.returncode == 0 and any(
            _updates_a_ref(line.partition(" ")[2])
            for line in result.stdout.splitlines()
        ):
            return None
        return (
            "Bare repository has no fetch refspec that updates a ref; nothing to update"
        )

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
            branch_result = run_git(
                ["git", "rev-parse", "--abbrev-ref", "HEAD"], repo_path, timeout=5
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
                    upstream_result = run_git(
                        ["git", "rev-parse", "--abbrev-ref", f"{branch}@{{upstream}}"],
                        repo_path,
                        timeout=5,
                    )

                    if upstream_result.returncode == 0:
                        state["has_upstream"] = True

            status_result = run_git(
                ["git", "status", "--porcelain"], repo_path, timeout=5
            )

            if status_result.returncode == 0:
                state["has_uncommitted"] = bool(status_result.stdout.strip())

        except ProcessAbandonedError:
            raise

        except Exception as e:
            logger.debug(f"Failed to check repository state: {e}")

        return state

    def _is_on_meta_config(self, repo_path: Path) -> bool:
        """Check if repository is currently on Gerrit's meta/config branch.

        Args:
            repo_path: Repository path

        Returns:
            True if on meta/config branch
        """
        try:
            result = run_git(
                ["git", "symbolic-ref", "-q", "HEAD"], repo_path, timeout=5
            )

            if result.returncode == 0:
                ref = result.stdout.strip()
                return ref == "refs/meta/config"

            # If not a symbolic ref, check with rev-parse
            result = run_git(
                ["git", "rev-parse", "--symbolic-full-name", "HEAD"],
                repo_path,
                timeout=5,
            )

            if result.returncode == 0:
                ref = result.stdout.strip()
                return ref == "refs/meta/config" or ref.startswith("refs/meta/")

            return False

        except ProcessAbandonedError:
            raise

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
            result = run_git(
                ["git", "ls-remote", "--heads", "origin"], repo_path, timeout=10
            )

            if result.returncode != 0:
                return False

            # If there are no heads at all, this is likely a meta-only repo
            output = result.stdout.strip()
            if not output:
                # Double-check that meta/config exists
                meta_result = run_git(
                    ["git", "ls-remote", "origin", "refs/meta/config"],
                    repo_path,
                    timeout=10,
                )

                if meta_result.returncode == 0 and meta_result.stdout.strip():
                    logger.debug(
                        f"{repo_path.name}: Confirmed as Gerrit parent project (has meta/config, no heads)"
                    )
                    return True

            return False

        except ProcessAbandonedError:
            raise

        except Exception as e:
            logger.debug(f"Failed to check meta-only status: {e}")
            return False

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
            result = run_git(
                [
                    "git",
                    "stash",
                    "push",
                    "--include-untracked",
                    "-m",
                    "gerrit-clone refresh auto-stash",
                ],
                repo_path,
                timeout=30,
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

        except ProcessAbandonedError:
            raise

        except Exception as e:
            logger.debug(f"Failed to stash changes: {e}")
            return StashOutcome.FAILED

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
            result = run_git(["git", "stash", "pop"], repo_path, timeout=30)

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

        except ProcessAbandonedError:
            raise

        except Exception as e:
            logger.debug(f"Failed to pop stash: {e}")
            return False

    def _restore_stash(self, repo_path: Path, result: RefreshResult) -> None:
        """Pop a stash the refresh made, onto the branch it came from.

        In force mode the stash may have been taken on a feature branch
        before switching to the default branch; popping it there would
        apply that work to the wrong branch, and drop the stash entry.
        It is then left for ``git stash list``.
        """
        if not result.stash_created or result.stash_popped:
            return
        if (
            result.stash_branch is not None
            and result.current_branch != result.stash_branch
        ):
            logger.warning(
                f"⚠️ {result.project_name}: Stash was created on "
                f"'{result.stash_branch}' but the working tree is now "
                f"on '{result.current_branch}'; leaving the stash "
                f"intact for manual recovery (git stash list)"
            )
        elif self._pop_stash(repo_path):
            result.stash_popped = True
            logger.debug(f"💾 {result.project_name}: Restored stashed changes")
        else:
            logger.warning(
                f"⚠️ {result.project_name}: Failed to restore stash (may have conflicts)"
            )

    def _stash_count(self, repo_path: Path) -> int:
        """Return the number of entries in the repository's stash list.

        Args:
            repo_path: Repository path

        Returns:
            Number of stash entries, or -1 if the count could not be
            determined.
        """
        try:
            result = run_git(["git", "stash", "list"], repo_path, timeout=30)
            if result.returncode != 0:
                return -1
            return sum(1 for line in result.stdout.splitlines() if line.strip())
        except ProcessAbandonedError:
            raise
        except Exception as e:
            logger.debug(f"Failed to count stash entries: {e}")
            return -1
