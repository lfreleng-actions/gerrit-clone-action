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
        # Get the current HEAD commit SHA
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
        # Get the current branch name
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
        # Check for uncommitted changes
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
        # Get the remote URL
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
