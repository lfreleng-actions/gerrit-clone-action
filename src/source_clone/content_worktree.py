# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""Worktree-based file removal fallback for bare git repositories.

Used when ``git filter-repo`` is unavailable.  Unlike filter-repo, this
approach only rewrites branch tips: a temporary worktree is created per
branch, matching files are removed with ``git rm`` and the result is
committed.  Historical commits still contain the removed files.
"""

from __future__ import annotations

import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING

from source_clone.content_git import run_content_git
from source_clone.content_removal import _list_tree_files
from source_clone.logging import get_logger

if TYPE_CHECKING:
    from collections.abc import Callable

logger = get_logger(__name__)

#: Commit message used for the removal commit created on each branch.
#: It and the commit author keep the old tool name deliberately, for
#: compatibility: :mod:`source_clone.content_legacy` recognises the
#: commits earlier releases wrote by this text.
_REMOVAL_COMMIT_MESSAGE = (
    "Remove filtered files for platform sync\n\n"
    "Files removed by gerrit-clone content filter "
    "to prevent platform-specific side effects."
)


def _list_branch_heads(repo_path: Path, timeout: int) -> list[str]:
    """List local branch names in *repo_path*.

    Args:
        repo_path: Path to the git repository.
        timeout: Timeout in seconds for the enumeration.

    Returns:
        Branch names.  An empty list means the repository genuinely has
        no branches.

    Raises:
        RuntimeError: If the branches could not be enumerated.  The
            caller would otherwise treat a failure as "no branches to
            filter" and report success with every requested file still
            present.
        ProcessAbandonedError: If the batch was abandoned.
    """
    try:
        result = run_content_git(
            [
                "git",
                "-C",
                str(repo_path),
                "for-each-ref",
                "--format=%(refname:short)",
                "refs/heads/",
            ],
            timeout=timeout,
        )
        if result.returncode != 0:
            msg = (
                f"Failed to list branches in {repo_path.name}: {result.stderr.strip()}"
            )
            logger.error(msg)
            raise RuntimeError(msg)
        return [b for b in result.stdout.strip().splitlines() if b]
    except subprocess.TimeoutExpired as exc:
        msg = f"Listing branches timed out in {repo_path.name} after {timeout}s"
        logger.error(msg)
        raise RuntimeError(msg) from exc
    except OSError as exc:
        msg = f"Failed to list branches in {repo_path.name}: {exc}"
        logger.error(msg)
        raise RuntimeError(msg) from exc


def _add_worktree(
    repo_path: Path,
    worktree_dir: str,
    branch: str,
    timeout: int,
) -> None:
    """Check *branch* out into a temporary worktree.

    Raises:
        subprocess.CalledProcessError: If ``git worktree add`` fails.
        ProcessAbandonedError: If the batch was abandoned.
    """
    run_content_git(
        [
            "git",
            "-C",
            str(repo_path),
            "worktree",
            "add",
            # ``--force`` is required for non-bare repos: git
            # otherwise refuses to add a worktree for a branch
            # that is already checked out in the repo's main
            # working tree (the common case for a normal
            # clone), which would make --remove-files a silent
            # no-op on that branch.  The branch ref is updated
            # by the commit below regardless of the now-stale
            # primary checkout.
            "--force",
            worktree_dir,
            branch,
        ],
        timeout=timeout,
        check=True,
    )


def _git_rm_files(
    worktree_dir: str,
    files_to_remove: list[str],
    branch: str,
    repo_name: str,
    timeout: int,
) -> None:
    """Remove *files_to_remove* from a checked-out worktree.

    Raises:
        RuntimeError: If ``git rm`` fails for any file.
    """
    for file_path in files_to_remove:
        full_path = Path(worktree_dir) / file_path
        if not full_path.exists():
            continue
        rm_result = run_content_git(
            ["git", "-C", worktree_dir, "rm", "-f", "--", file_path],
            timeout=timeout,
        )
        if rm_result.returncode != 0:
            raise RuntimeError(
                f"git rm failed for '{file_path}' on "
                f"branch '{branch}' in "
                f"{repo_name}: "
                f"{rm_result.stderr.strip()}"
            )


def _commit_removal(
    worktree_dir: str,
    branch: str,
    repo_name: str,
    timeout: int,
) -> None:
    """Commit the staged removals in *worktree_dir*.

    Raises:
        RuntimeError: If ``git commit`` fails.
    """
    result = run_content_git(
        [
            "git",
            "-C",
            worktree_dir,
            "-c",
            # Old tool name kept deliberately: see _REMOVAL_COMMIT_MESSAGE.
            "user.name=gerrit-clone",
            "-c",
            "user.email=gerrit-clone@noreply",
            "commit",
            "-m",
            _REMOVAL_COMMIT_MESSAGE,
            "--allow-empty",
        ],
        timeout=timeout,
    )

    if result.returncode != 0:
        raise RuntimeError(
            f"git commit failed on branch '{branch}' in "
            f"{repo_name}: {result.stderr.strip()}"
        )


def _cleanup_worktree(repo_path: Path, worktree_dir: str, timeout: int) -> None:
    """Detach and delete the temporary worktree, best effort.

    Raises:
        ProcessAbandonedError: If the batch was abandoned.  The
            temporary directory is deleted regardless.
    """
    try:
        run_content_git(
            [
                "git",
                "-C",
                str(repo_path),
                "worktree",
                "remove",
                "--force",
                worktree_dir,
            ],
            timeout=timeout,
        )
    finally:
        # The directory is ours whether or not git got to detach it; an
        # abandoned batch would otherwise leave a checkout in the
        # temporary directory for every branch it was filtering.
        if Path(worktree_dir).exists():
            shutil.rmtree(worktree_dir, ignore_errors=True)


def _remove_files_on_branch(
    repo_path: Path,
    branch: str,
    files_to_remove: list[str],
    timeout: int,
) -> None:
    """Remove and commit *files_to_remove* on a single *branch*.

    Raises:
        RuntimeError: If the worktree, removal or commit step fails.
    """
    # The prefix keeps the old tool name deliberately, so a worktree an
    # interrupted run leaves behind has one name whichever release made it.
    worktree_dir = tempfile.mkdtemp(prefix=f"gerrit-clone-filter-{repo_path.name}-")
    try:
        _add_worktree(repo_path, worktree_dir, branch, timeout)
        _git_rm_files(worktree_dir, files_to_remove, branch, repo_path.name, timeout)
        _commit_removal(worktree_dir, branch, repo_path.name, timeout)
        logger.debug(
            "Committed removal of %d files on branch '%s'",
            len(files_to_remove),
            branch,
        )
    except subprocess.CalledProcessError as exc:
        # Surface the failure instead of silently skipping the
        # branch: a swallowed worktree error would make
        # --remove-files a no-op for this branch while still
        # reporting overall success.  apply_content_filters
        # treats RuntimeError as a filtering failure.
        raise RuntimeError(
            f"Failed to create worktree for branch '{branch}' in "
            f"{repo_path.name}: {exc.stderr}"
        ) from exc
    finally:
        _cleanup_worktree(repo_path, worktree_dir, timeout)


def _remove_files_worktree(
    repo_path: Path,
    patterns: list[str],
    matcher: Callable[[str, str], bool],
    *,
    timeout: int = 300,
) -> list[str]:
    """Remove files from branch tips using a temporary worktree.

    This fallback method creates a temporary worktree for each branch,
    removes matching files, and commits the changes.  Unlike
    ``git filter-repo``, this only affects the branch tips — historical
    commits still contain the removed files.

    Args:
        repo_path: Path to the bare git repository.
        patterns: File path patterns to remove.
        matcher: Predicate deciding whether a path matches a pattern.
        timeout: Timeout for git operations.

    Returns:
        List of files that were removed (across all branches).

    Raises:
        RuntimeError: If the repository could not be enumerated, or if
            removal failed on a branch.  Reporting success while the
            files are still present is the wrong failure mode for a
            filter whose job is removing secrets.
    """
    branches = _list_branch_heads(repo_path, timeout)
    if not branches:
        logger.debug("No branches found in %s", repo_path.name)
        return []

    all_removed: list[str] = []
    for branch in branches:
        # List files on this branch
        files = _list_tree_files(repo_path, branch, timeout=timeout)
        if not files:
            continue

        # Find files matching any pattern.  The ``matcher`` supplied by
        # the caller adds directory-prefix matching for plain path
        # patterns so a pattern like ``.github/workflows`` removes
        # everything under that directory — matching ``git
        # filter-repo``'s ``--path`` prefix semantics used by the
        # preferred code path.
        files_to_remove = [f for f in files if any(matcher(f, pat) for pat in patterns)]
        if not files_to_remove:
            continue

        logger.debug(
            "Removing %d file(s) from branch '%s' in %s: %s",
            len(files_to_remove),
            branch,
            repo_path.name,
            files_to_remove[:5],
        )
        _remove_files_on_branch(repo_path, branch, files_to_remove, timeout)
        all_removed.extend(files_to_remove)

    unique_removed = sorted(set(all_removed))
    if unique_removed:
        logger.info(
            "Removed %d unique file(s) from %s across %d branch(es)",
            len(unique_removed),
            repo_path.name,
            len(branches),
        )
    return unique_removed
