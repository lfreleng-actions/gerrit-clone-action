# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""File removal from bare git repositories via ``git filter-repo``.

Provides the availability probe for ``git filter-repo``, the preferred
history-rewriting removal path, and the ``git ls-tree`` helper used by
the worktree fallback in :mod:`source_clone.content_worktree`.
"""

from __future__ import annotations

import subprocess
from typing import TYPE_CHECKING

from source_clone.content_git import run_content_git
from source_clone.logging import get_logger

if TYPE_CHECKING:
    from pathlib import Path

logger = get_logger(__name__)


def _check_git_filter_repo() -> bool:
    """Check if git-filter-repo is available.

    Returns:
        ``True`` if ``git filter-repo`` is available on PATH.

    Raises:
        ProcessAbandonedError: If the batch was abandoned.  Answering
            "unavailable" would send removal down the worktree fallback.
    """
    try:
        result = run_content_git(["git", "filter-repo", "--version"], timeout=5)
        return result.returncode == 0
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False


def _build_filter_repo_command(
    repo_path: Path, patterns: list[str]
) -> tuple[
    list[str],
    list[str],
]:
    """Build the ``git filter-repo`` command line for *patterns*.

    Returns:
        Tuple of ``(command, applied_patterns)``.  Patterns that would
        be unsafe or meaningless are skipped and excluded from
        ``applied_patterns``.
    """
    cmd: list[str] = [
        "git",
        "-C",
        str(repo_path),
        "filter-repo",
        "--force",
    ]

    applied: list[str] = []
    for pattern in patterns:
        if pattern.startswith("regex:"):
            # Use --path-regex with --invert-paths
            regex = pattern[len("regex:") :]
            if not regex:
                # A bare ``regex:`` is an empty regex, which matches
                # every path. Combined with ``--invert-paths`` that
                # would wipe the entire repository history. Reject it
                # explicitly, mirroring ``match_file_pattern``.
                logger.warning("Empty regex pattern (bare 'regex:') ignored")
                continue
            cmd.extend(["--path-regex", regex, "--invert-paths"])
            applied.append(pattern)
        elif any(c in pattern for c in ("*", "?", "[", "]")):
            cmd.extend(["--path-glob", pattern, "--invert-paths"])
            applied.append(pattern)
        elif not pattern:
            # An empty exact path is meaningless; skip it rather than
            # passing an empty ``--path`` to git filter-repo.
            logger.warning("Empty file pattern ignored")
            continue
        else:
            # Exact path — use --path with --invert-paths
            cmd.extend(["--path", pattern, "--invert-paths"])
            applied.append(pattern)

    return cmd, applied


def _remove_files_filter_repo(
    repo_path: Path,
    patterns: list[str],
    *,
    timeout: int = 300,
) -> list[str]:
    """Remove files using git filter-repo (all history).

    Args:
        repo_path: Path to the bare git repository.
        patterns: File path patterns to remove.
        timeout: Timeout for the operation.

    Returns:
        List of pattern arguments that were applied.
    """
    cmd, applied = _build_filter_repo_command(repo_path, patterns)

    if not applied:
        return []

    logger.info(
        "Removing files from %s using git filter-repo: %s",
        repo_path.name,
        applied,
    )

    try:
        result = run_content_git(cmd, timeout=timeout)
        if result.returncode != 0:
            msg = (
                f"git filter-repo failed for {repo_path.name}: {result.stderr.strip()}"
            )
            logger.error(msg)
            raise RuntimeError(msg)

        logger.info(
            "Successfully filtered files from %s",
            repo_path.name,
        )
        return applied
    except subprocess.TimeoutExpired:
        msg = f"git filter-repo timed out for {repo_path.name} after {timeout}s"
        logger.error(msg)
        raise RuntimeError(msg) from None
    except RuntimeError:
        # Includes ProcessAbandonedError, which must reach the batch
        # unwrapped rather than as a filtering failure.
        raise
    except Exception as exc:
        msg = f"git filter-repo error for {repo_path.name}: {exc}"
        logger.error(msg)
        raise RuntimeError(msg) from exc


def _list_tree_files(
    repo_path: Path,
    ref: str,
    *,
    timeout: int = 300,
) -> list[str]:
    """List all files in a bare repo at a given ref.

    Args:
        repo_path: Path to the bare git repository.
        ref: Git ref to list files from.
        timeout: Timeout in seconds for the ls-tree operation.

    Returns:
        List of file paths relative to the repo root.  An empty list
        means the ref genuinely holds no files.

    Raises:
        RuntimeError: If the listing could not be produced.  A filter
            whose job is removing secrets must not report success with
            the files still in place, so a failure to enumerate is a
            filtering failure rather than "nothing to do".
        ProcessAbandonedError: If the batch was abandoned.
    """
    try:
        result = run_content_git(
            ["git", "-C", str(repo_path), "ls-tree", "-r", "--name-only", ref],
            timeout=timeout,
        )
        if result.returncode != 0:
            msg = (
                f"git ls-tree failed for {repo_path.name} (ref {ref}): "
                f"{result.stderr.strip()}"
            )
            logger.error(msg)
            raise RuntimeError(msg)
        return [line for line in result.stdout.strip().splitlines() if line]
    except subprocess.TimeoutExpired as exc:
        msg = f"git ls-tree timed out for {repo_path.name} (ref {ref}) after {timeout}s"
        logger.error(msg)
        raise RuntimeError(msg) from exc
    except OSError as exc:
        msg = f"git ls-tree failed for {repo_path.name} (ref {ref}): {exc}"
        logger.error(msg)
        raise RuntimeError(msg) from exc
