# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2025 Matthew Watkins <mwatkins@linuxfoundation.org>

"""Git utility functions for repository operations."""

from __future__ import annotations

import subprocess
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path


def is_git_repository(repo_path: Path) -> bool:
    """Check if a path is a git repository (regular or bare).

    This function detects both:
    - Regular repositories (with a .git subdirectory)
    - Bare repositories (where the repo root IS the git directory)

    Args:
        repo_path: Path to check

    Returns:
        True if the path is a git repository (regular or bare), False otherwise
    """
    if not repo_path.exists():
        return False

    if not repo_path.is_dir():
        return False

    # Check for regular repository (.git subdirectory exists)
    git_dir = repo_path / ".git"
    if git_dir.exists():
        return True

    # Check for bare repository filesystem markers first (cheap operation)
    # A bare repo typically has these at the root level
    bare_markers = ["HEAD", "objects", "refs", "config"]
    has_markers = all((repo_path / marker).exists() for marker in bare_markers)

    if has_markers:
        return True

    # If filesystem markers aren't present, use git command as fallback
    # This handles edge cases where git knows it's a repo but markers are non-standard
    try:
        result = subprocess.run(
            ["git", "-C", str(repo_path), "rev-parse", "--is-bare-repository"],
            capture_output=True,
            text=True,
            check=False,
            timeout=5,
        )
        # If git command succeeds and returns "true", it's a bare repo
        if result.returncode == 0 and result.stdout.strip() == "true":
            return True
    except (subprocess.TimeoutExpired, FileNotFoundError, Exception):
        # If the git command fails, it's not a valid git repository
        pass

    return False


def get_current_commit_sha(repo_path: Path) -> str | None:
    """Get the current commit SHA (HEAD) for a local repository.

    Works with both regular and bare repositories.

    Args:
        repo_path: Path to the git repository

    Returns:
        The commit SHA as a string, or None if it cannot be determined

    Raises:
        FileNotFoundError: If the repository path doesn't exist
        ValueError: If the path is not a git repository
    """
    if not repo_path.exists():
        raise FileNotFoundError(f"Repository path does not exist: {repo_path}")

    if not is_git_repository(repo_path):
        raise ValueError(f"Not a git repository: {repo_path}")

    try:
        result = subprocess.run(
            ["git", "-C", str(repo_path), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            check=True,
            timeout=5,
        )
        sha = result.stdout.strip()
        return sha if sha else None

    except subprocess.CalledProcessError:
        # Could be detached HEAD, new repo with no commits, etc.
        return None
    except subprocess.TimeoutExpired:
        return None
    except Exception:
        return None


def get_current_branch(repo_path: Path) -> str | None:
    """Get the current branch name for a local repository.

    Works with both regular and bare repositories.

    Args:
        repo_path: Path to the git repository

    Returns:
        The branch name as a string, or None if detached HEAD or error

    Raises:
        FileNotFoundError: If the repository path doesn't exist
        ValueError: If the path is not a git repository
    """
    if not repo_path.exists():
        raise FileNotFoundError(f"Repository path does not exist: {repo_path}")

    if not is_git_repository(repo_path):
        raise ValueError(f"Not a git repository: {repo_path}")

    try:
        result = subprocess.run(
            ["git", "-C", str(repo_path), "symbolic-ref", "--short", "HEAD"],
            capture_output=True,
            text=True,
            check=True,
            timeout=5,
        )
        branch = result.stdout.strip()
        return branch if branch else None

    except subprocess.CalledProcessError:
        # Detached HEAD state
        return None
    except subprocess.TimeoutExpired:
        return None
    except Exception:
        return None


def is_repo_dirty(repo_path: Path) -> bool:
    """Check if a repository has uncommitted changes.

    Note: Bare repositories cannot have uncommitted changes by definition,
    so this will always return False for bare repos.

    Args:
        repo_path: Path to the git repository

    Returns:
        True if there are uncommitted changes, False otherwise

    Raises:
        FileNotFoundError: If the repository path doesn't exist
        ValueError: If the path is not a git repository
    """
    if not repo_path.exists():
        raise FileNotFoundError(f"Repository path does not exist: {repo_path}")

    if not is_git_repository(repo_path):
        raise ValueError(f"Not a git repository: {repo_path}")

    try:
        result = subprocess.run(
            ["git", "-C", str(repo_path), "status", "--porcelain"],
            capture_output=True,
            text=True,
            check=True,
            timeout=5,
        )
        # If output is non-empty, there are changes
        return bool(result.stdout.strip())

    except subprocess.CalledProcessError:
        return False
    except subprocess.TimeoutExpired:
        return False
    except Exception:
        return False


def get_remote_url(repo_path: Path, remote: str = "origin") -> str | None:
    """Get the remote URL for a repository.

    Works with both regular and bare repositories.

    Args:
        repo_path: Path to the git repository
        remote: Name of the remote (default: "origin")

    Returns:
        The remote URL as a string, or None if not found

    Raises:
        FileNotFoundError: If the repository path doesn't exist
        ValueError: If the path is not a git repository
    """
    if not repo_path.exists():
        raise FileNotFoundError(f"Repository path does not exist: {repo_path}")

    if not is_git_repository(repo_path):
        raise ValueError(f"Not a git repository: {repo_path}")

    try:
        result = subprocess.run(
            ["git", "-C", str(repo_path), "remote", "get-url", remote],
            capture_output=True,
            text=True,
            check=True,
            timeout=5,
        )
        url = result.stdout.strip()
        return url if url else None

    except subprocess.CalledProcessError:
        # Remote doesn't exist
        return None
    except subprocess.TimeoutExpired:
        return None
    except Exception:
        return None


def get_head_ref(repo_path: Path) -> str | None:
    """Read the raw HEAD symbolic reference from a repository.

    Unlike :func:`get_current_branch`, this returns the full ref string
    (e.g. ``refs/heads/master`` or ``refs/meta/config``), which is needed
    to distinguish Gerrit parent projects from normal repositories.

    Works with both regular and bare repositories by reading the HEAD
    file directly.

    Args:
        repo_path: Path to the git repository

    Returns:
        The full ref string (e.g. ``refs/heads/main``,
        ``refs/meta/config``), or ``None`` if HEAD is detached,
        the file is missing, or the format is unrecognised.
    """
    try:
        head_file = repo_path / "HEAD"
        if not head_file.exists():
            # Try .git/HEAD for non-bare repos
            head_file = repo_path / ".git" / "HEAD"
        if not head_file.exists():
            return None

        content = head_file.read_text().strip()
        if content.startswith("ref: "):
            return content[len("ref: "):]
        # Detached HEAD (raw SHA) — no symbolic ref
        return None
    except Exception:
        return None


def list_local_branches(repo_path: Path) -> list[str]:
    """List all local branch names in a repository.

    For bare clones this lists everything under ``refs/heads/``.
    Returns an empty list if the repo has no branches (e.g. a Gerrit
    parent project whose only ref is ``refs/meta/config``).

    Args:
        repo_path: Path to the git repository (regular or bare)

    Returns:
        Sorted list of branch names (without the ``refs/heads/`` prefix),
        or an empty list on error.
    """
    try:
        result = subprocess.run(
            [
                "git", "-C", str(repo_path),
                "for-each-ref",
                "--format=%(refname:short)",
                "refs/heads/",
            ],
            capture_output=True,
            text=True,
            check=True,
            timeout=10,
        )
        branches = [
            line.strip()
            for line in result.stdout.splitlines()
            if line.strip()
        ]
        return sorted(branches)
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, Exception):
        return []


def is_gerrit_parent_project(repo_path: Path) -> bool:
    """Detect whether a local clone is a Gerrit organisational parent project.

    Gerrit uses "parent projects" to structure its project hierarchy
    (e.g. ``aai`` is the parent of ``aai/aai-common``, ``aai/babel``,
    etc.).  These projects typically:

    * Have their HEAD pointing to ``refs/meta/config``
    * Contain **no** ``refs/heads/*`` branches
    * Hold only Gerrit metadata (``refs/meta/config``, ``refs/changes/*``)

    Such repositories will always appear empty on GitHub because there
    are no code branches to display.

    Args:
        repo_path: Path to the local (usually bare) clone

    Returns:
        ``True`` if the repository looks like a Gerrit parent project
        (HEAD → ``refs/meta/config`` and no branches under
        ``refs/heads/``), ``False`` otherwise.
    """
    head_ref = get_head_ref(repo_path)
    if head_ref != "refs/meta/config":
        return False

    branches = list_local_branches(repo_path)
    return len(branches) == 0
