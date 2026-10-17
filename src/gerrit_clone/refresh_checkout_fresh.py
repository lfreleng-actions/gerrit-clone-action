# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""Filtering a working copy ``clone`` has just made, through a copy.

Filtered where it stands, a working copy loses the remote-tracking refs
a later refresh follows: ``git filter-repo`` folds them into local
branches.  So a new one is filtered through a staged copy, the steps of
:mod:`gerrit_clone.refresh_checkout_stage` with three differences:

1. The copy fetches nothing -- the clone has just fetched upstream --
   but takes the checkout's tags, so the filters rewrite them too.
2. Its refs are published without the follow check: the checkout's
   branch holds the unfiltered history the filtered one replaces.
3. If the filters rewrote anything, the checkout's current branch, with
   no work of its own, is reset to its filtered upstream -- or a
   detached ``HEAD`` moved to the filtered form of the tag it was on --
   and every unfiltered object purged.  Its tags are then the copy's
   exactly, so a tag the filters removed entirely goes too.  A filter
   matching nothing records nothing and leaves the checkout as it was.

Anything that fails leaves a checkout that may hold unfiltered content:
the caller deletes it.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from gerrit_clone.content_journal import refs_digest
from gerrit_clone.content_policy import PolicyRecordError, stored_policy
from gerrit_clone.refresh_checkout_refs import PUBLISHED
from gerrit_clone.refresh_checkout_stage import StagedCheckoutMixin
from gerrit_clone.refresh_git_env import run_git

if TYPE_CHECKING:
    from gerrit_clone.models import RefreshResult

#: Why a fresh working copy whose branch tracks nothing cannot be filtered.
NO_UPSTREAM_REFUSAL = (
    "Its branch tracks no upstream branch, so it could not be reset to the "
    "filtered history"
)

#: Why a fresh working copy detached on rewritten history cannot be filtered.
DETACHED_REWRITTEN_REFUSAL = (
    "Its detached HEAD is on a commit the filters rewrote, and on no tag "
    "they kept, so it could not be moved to the filtered history"
)


class FreshCheckoutMixin(StagedCheckoutMixin):
    """Staged filtering of working copies ``clone`` has just made."""

    def _filter_fresh_checkout(self, repo_path: Path, result: RefreshResult) -> bool:
        """Filter *repo_path*, a working copy just cloned, as the module says.

        Returns:
            True if the checkout holds filtered content alone and the copy
            has gone.  Otherwise False, with ``result.error_message``
            saying why: the checkout may then hold unfiltered content.
        """
        git_dir = self._checkout_git_dir(repo_path, result)
        if git_dir is None:
            return False
        # What publishing replaces, as it stood when the copy was made.
        base = refs_digest(repo_path, *PUBLISHED)
        filtered, removed = self._in_checkout_stage(
            git_dir,
            result,
            lambda stage: self._fresh_checkout_stage(
                repo_path, Path(git_dir), stage, result, base
            ),
        )
        return filtered and removed

    def _fresh_checkout_stage(
        self,
        repo_path: Path,
        git_dir: Path,
        stage: Path,
        result: RefreshResult,
        base: str | None,
    ) -> bool:
        """:meth:`_filter_fresh_checkout`, with *stage* as the copy."""
        if self._stage_and_filter(repo_path, stage, result, fetch=False) is not None:
            return False
        # Before publishing replaces them: a detached HEAD follows its tag.
        on_tags = run_git(
            ["git", "tag", "--points-at", "HEAD"], repo_path, timeout=10
        ).stdout.split()
        try:
            rewrote = not stored_policy(stage).empty
        except PolicyRecordError as exc:
            result.error_message = f"Could not read what the filters rewrote: {exc}"
            return False
        if (
            self._record_and_publish(repo_path, stage, result, base, follow=False)
            is not None
        ):
            return False
        return not rewrote or (
            self._reset_fresh_branch(repo_path, result, on_tags)
            and self._purge_unfiltered(repo_path, git_dir, result)
        )

    def _reset_fresh_branch(
        self, repo_path: Path, result: RefreshResult, on_tags: list[str]
    ) -> bool:
        """Put a fresh checkout's current branch on its filtered upstream.

        It has no work of its own to lose.  An unborn branch has nothing
        to reset.  A detached ``HEAD`` stays if the filtered refs still
        hold its commit; otherwise it moves to the filtered form of a tag
        it was on, *on_tags* (a clone of a single tag), or is refused.
        """
        head = run_git(
            ["git", "rev-parse", "-q", "--verify", "HEAD"], repo_path, timeout=10
        )
        if head.returncode != 0:
            return True  # Unborn: an empty upstream.
        branch = run_git(["git", "symbolic-ref", "-q", "HEAD"], repo_path, timeout=10)
        if branch.returncode != 0:
            holding = run_git(
                [
                    "git",
                    "for-each-ref",
                    "--count=1",
                    "--contains",
                    head.stdout.strip(),
                    *PUBLISHED,
                ],
                repo_path,
                timeout=self.timeout,
            )
            if holding.returncode == 0 and holding.stdout.strip():
                return True
            return self._follow_tag(repo_path, result, on_tags)
        upstream = run_git(
            ["git", "rev-parse", "-q", "--verify", "@{upstream}"],
            repo_path,
            timeout=10,
        )
        if upstream.returncode != 0:
            result.error_message = NO_UPSTREAM_REFUSAL
            return False
        reset = run_git(
            ["git", "reset", "--hard", "--quiet", upstream.stdout.strip()],
            repo_path,
            timeout=self.timeout,
        )
        if reset.returncode != 0:
            result.error_message = (
                f"Could not reset the branch to its filtered upstream: "
                f"{reset.stderr.strip()}"
            )
        return reset.returncode == 0

    def _follow_tag(
        self, repo_path: Path, result: RefreshResult, on_tags: list[str]
    ) -> bool:
        """Move a detached ``HEAD`` to the filtered form of a tag it was on."""
        for tag in on_tags:
            target = run_git(
                ["git", "rev-parse", "-q", "--verify", f"refs/tags/{tag}^{{commit}}"],
                repo_path,
                timeout=10,
            )
            if target.returncode != 0:
                continue  # The filters removed this tag entirely.
            moved = run_git(
                ["git", "checkout", "--quiet", "--detach", target.stdout.strip()],
                repo_path,
                timeout=self.timeout,
            )
            if moved.returncode != 0:
                result.error_message = (
                    f"Could not move HEAD to the filtered tag {tag}: "
                    f"{moved.stderr.strip()}"
                )
            return moved.returncode == 0
        result.error_message = DETACHED_REWRITTEN_REFUSAL
        return False

    def _purge_unfiltered(
        self, repo_path: Path, git_dir: Path, result: RefreshResult
    ) -> bool:
        """Delete every object the filters left unreachable in *repo_path*.

        A plain ``gc`` would keep them: the reflogs and ``ORIG_HEAD`` still
        reach the unfiltered commits.
        """
        expired = run_git(
            [
                "git",
                "reflog",
                "expire",
                "--expire=now",
                "--expire-unreachable=now",
                "--all",
            ],
            repo_path,
            timeout=self.timeout,
        )
        failure = expired.stderr.strip() if expired.returncode != 0 else None
        if failure is None:
            try:
                (git_dir / "ORIG_HEAD").unlink(missing_ok=True)
            except OSError as exc:
                failure = str(exc)
        if failure is None:
            pruned = run_git(
                ["git", "gc", "--prune=now", "--quiet"],
                repo_path,
                timeout=self.timeout,
            )
            failure = pruned.stderr.strip() if pruned.returncode != 0 else None
        if failure is not None:
            result.error_message = f"Could not purge the unfiltered objects: {failure}"
        return failure is None
