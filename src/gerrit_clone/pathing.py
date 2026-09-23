# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2025 Matthew Watkins <mwatkins@linuxfoundation.org>

"""Path handling utilities for safe filesystem operations.

Detects and resolves clone destination conflicts, and performs the temporary
clone plus atomic move dance that avoids leaving partial repositories behind.
Project name validation and sanitization live in
:mod:`gerrit_clone.path_naming` and are re-exported here.
"""

from __future__ import annotations

import contextlib
import shutil
import time
import uuid
from pathlib import Path
from typing import TYPE_CHECKING, Any

from gerrit_clone.logging import get_logger
from gerrit_clone.path_errors import (
    PathConflictError,
    PathError,
    PathValidationError,
)
from gerrit_clone.path_naming import (
    get_project_path,
    sanitize_project_name,
    validate_project_name,
)

if TYPE_CHECKING:
    from collections.abc import Callable
    from contextlib import AbstractContextManager

logger = get_logger(__name__)

__all__ = [
    "AtomicClonePath",
    "PathConflictError",
    "PathError",
    "PathValidationError",
    "atomic_move",
    "check_path_conflicts",
    "cleanup_temp_path",
    "create_parent_directories",
    "ensure_directory_writable",
    "format_path_for_display",
    "get_project_path",
    "get_relative_path",
    "get_temp_clone_path",
    "move_conflicting_path",
    "sanitize_project_name",
    "validate_project_name",
]


def create_parent_directories(path: Path, mode: int = 0o755) -> None:
    """Create parent directories for a path if they don't exist.

    Args:
        path: Target path (parent directories will be created)
        mode: Directory permissions mode

    Raises:
        PathError: If directory creation fails
    """
    parent = path.parent
    if parent == path:
        # Root directory - nothing to create
        return

    try:
        parent.mkdir(parents=True, exist_ok=True, mode=mode)
        logger.debug(f"Created parent directories for [path]{path}[/path]")
    except OSError as e:
        raise PathError(f"Failed to create parent directories for {path}: {e}") from e


def check_path_conflicts(target_path: Path, is_nested_repo: bool = False) -> str | None:
    """Check for path conflicts that would prevent cloning.

    Detects both regular repositories (with .git subdirectory) and bare repositories
    (created with git clone --mirror).

    Args:
        target_path: Intended clone destination
        is_nested_repo: True if this is a nested repository under a parent

    Returns:
        Conflict description if found, None if path is available
    """
    try:
        if not target_path.exists():
            return None
    except (OSError, PermissionError) as e:
        return f"Permission denied accessing path: {e}"

    if target_path.is_file():
        if is_nested_repo:
            return "nested_file_conflict"  # Special case for nested repos with file conflicts
        return f"File exists at target path: {target_path}"

    if target_path.is_dir():
        # Check if it's already a git repository (regular or bare)
        from gerrit_clone.git_utils import is_git_repository  # noqa: PLC0415

        if is_git_repository(target_path):
            return "already_cloned"  # Special case - not an error
        else:
            # Directory exists but is not a git repo - could be incomplete clone
            contents = list(target_path.iterdir())
            if contents:
                # Check if this looks like an incomplete git clone (regular or bare)
                # Regular repo artifacts
                regular_git_files = [
                    ".git",
                    ".gitignore",
                    "README.md",
                    "pom.xml",
                    "Makefile",
                ]
                has_regular_artifacts = any(
                    item.name in regular_git_files for item in contents
                )

                # Bare repo artifacts (partial bare clone that failed)
                bare_git_files = [
                    "HEAD",
                    "objects",
                    "refs",
                    "config",
                    "hooks",
                    "info",
                    "description",
                ]
                has_bare_artifacts = any(
                    item.name in bare_git_files for item in contents
                )

                # If it has git artifacts but is_git_repository returned False,
                # it's likely an incomplete clone that should be cleaned up
                if has_regular_artifacts or has_bare_artifacts:
                    return "incomplete_clone"  # Special case - cleanup and retry
                else:
                    return f"Non-empty directory exists: {target_path} (contains {len(contents)} items)"
            else:
                return f"Empty directory exists: {target_path}"

    # Other filesystem object (symlink, device, etc.)
    return f"Filesystem object exists at target path: {target_path}"


def move_conflicting_path(
    target_path: Path,
    _is_nested_repo: bool = False,
    reserve: Callable[[Path], bool] | None = None,
    guard: Callable[[], AbstractContextManager[None]] | None = None,
) -> bool:
    """Move conflicting file or directory to .parent suffix to allow nested cloning.

    Args:
        target_path: Path that needs to be cleared for cloning
        _is_nested_repo: True if this is for a nested repository (currently unused)
        reserve: Claims a candidate backup name, returning whether it
            was granted; a refused name is passed over like an occupied
            one.  Absence on disk does not make a name safe, another
            clone being able to hold one it has not yet written to.
            Omitted outside a batch, where there is nothing to arbitrate.
        guard: Held around the rename alone, so the caller can make it
            atomic with a check of its own without the backup name being
            reserved inside it.

    Returns:
        True if conflict was moved, False if no conflict or move failed

    Raises:
        PathError: If move operation fails critically
        Exception: Whatever *guard* raises to refuse the move, unchanged
    """
    if not target_path.exists():
        return False

    # Each candidate is claimed as it is chosen, so that the choice and
    # the claim cannot be separated by another clone taking the name.
    parent_backup_path = target_path.with_name(target_path.name + ".parent")

    counter = 1
    while parent_backup_path.exists() or (
        reserve is not None and not reserve(parent_backup_path)
    ):
        parent_backup_path = target_path.with_name(
            f"{target_path.name}.parent.{counter}"
        )
        counter += 1
        if counter > 100:  # Prevent infinite loop
            raise PathError(f"Cannot generate unique backup name for {target_path}")

    try:
        # Check if it's a file or directory before moving
        is_file = target_path.is_file()

        # Perform the move
        with guard() if guard is not None else contextlib.nullcontext():
            target_path.rename(parent_backup_path)
        logger.info(
            f"Moved conflicting {'file' if is_file else 'directory'} "
            f"[path]{target_path.name}[/path] to [path]{parent_backup_path.name}[/path]"
        )
        return True
    except OSError as e:
        # Check if the path still exists after the attempted move
        if target_path.exists():
            raise PathError(
                f"Failed to move conflicting path {target_path} to {parent_backup_path}: {e}"
            ) from e
        else:
            # Move succeeded even though we got an exception (race condition?)
            logger.debug(
                f"Move succeeded despite exception: {target_path} -> {parent_backup_path}"
            )
            return True


def get_temp_clone_path(target_path: Path) -> Path:
    """Get a temporary path for atomic clone operations.

    Args:
        target_path: Final destination path

    Returns:
        Temporary path for cloning
    """
    # Generate unique temporary name
    temp_suffix = f".partial.{uuid.uuid4().hex[:8]}"
    temp_path = target_path.with_name(target_path.name + temp_suffix)

    # Ensure temp path doesn't exist (very unlikely with UUID)
    counter = 1
    while temp_path.exists():
        temp_suffix = f".partial.{uuid.uuid4().hex[:8]}.{counter}"
        temp_path = target_path.with_name(target_path.name + temp_suffix)
        counter += 1
        if counter > 100:  # Prevent infinite loop
            raise PathError(f"Cannot generate unique temporary path for {target_path}")

    return temp_path


def atomic_move(source_path: Path, target_path: Path) -> None:
    """Atomically move source to target path.

    Args:
        source_path: Source path (typically temporary clone directory)
        target_path: Final destination path

    Raises:
        PathError: If move operation fails
    """
    try:
        # Ensure target parent directories exist
        create_parent_directories(target_path)

        # If target exists, remove it first to ensure replacement
        if target_path.exists():
            if target_path.is_dir():
                shutil.rmtree(target_path)
            else:
                target_path.unlink()

        # Perform atomic move
        shutil.move(str(source_path), str(target_path))
        logger.debug(f"Moved [path]{source_path}[/path] -> [path]{target_path}[/path]")

    except OSError as e:
        raise PathError(f"Failed to move {source_path} to {target_path}: {e}") from e


def cleanup_temp_path(temp_path: Path) -> None:
    """Clean up temporary path after failed operation.

    Args:
        temp_path: Temporary path to remove
    """
    if not temp_path.exists():
        return

    try:
        if temp_path.is_dir():
            shutil.rmtree(temp_path)
            logger.debug(f"Cleaned up temporary directory [path]{temp_path}[/path]")
        else:
            temp_path.unlink()
            logger.debug(f"Cleaned up temporary file [path]{temp_path}[/path]")
    except OSError as e:
        logger.warning(f"Failed to cleanup temporary path {temp_path}: {e}")


def ensure_directory_writable(path: Path) -> None:
    """Ensure directory is writable for current user.

    Args:
        path: Directory path to check

    Raises:
        PathError: If directory is not writable
    """
    if not path.exists():
        raise PathError(f"Directory does not exist: {path}")

    if not path.is_dir():
        raise PathError(f"Path is not a directory: {path}")

    # Test writability by attempting to create a temporary file
    test_file = path / f".write_test_{uuid.uuid4().hex[:8]}"
    try:
        test_file.touch()
        test_file.unlink()
    except OSError as e:
        raise PathError(f"Directory is not writable: {path} ({e})") from e


def get_relative_path(path: Path, base: Path) -> Path:
    """Get relative path from base directory.

    Args:
        path: Target path
        base: Base directory

    Returns:
        Relative path, or original path if not under base
    """
    try:
        return path.relative_to(base)
    except ValueError:
        # Path is not under base directory
        return path


def format_path_for_display(path: Path, base: Path | None = None) -> str:
    """Format path for user-friendly display.

    Args:
        path: Path to format
        base: Optional base path to make relative

    Returns:
        Formatted path string
    """
    if base is not None:
        try:
            display_path = path.relative_to(base)
        except ValueError:
            display_path = path
    else:
        display_path = path

    return str(display_path)


class AtomicClonePath:
    """Context manager for atomic clone operations."""

    def __init__(self, target_path: Path) -> None:
        """Initialize atomic clone path manager.

        Args:
            target_path: Final destination for clone
        """
        self.target_path = target_path
        self.temp_path = get_temp_clone_path(target_path)
        self._finalized = False

    def __enter__(self) -> AtomicClonePath:
        """Enter context and return self for accessing temp_path and other methods.

        Returns:
            Self to allow access to temp_path and finalize method
        """
        # Ensure parent directories exist
        create_parent_directories(self.temp_path)
        return self

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        """Exit context and handle cleanup or finalization."""
        # Only cleanup if we explicitly haven't finalized AND there was an exception
        # This prevents premature cleanup while Git might still be accessing the directory
        if exc_type is not None and not self._finalized:
            # Exception occurred - cleanup temp, but with a delay to ensure Git has finished
            time.sleep(0.2)  # Brief delay to let Git processes finish
            cleanup_temp_path(self.temp_path)
        # If success case but not finalized, leave temp directory - caller should finalize

    def finalize(self) -> None:
        """Finalize the clone by moving temp to target path.

        Raises:
            PathError: If finalization fails
        """
        if self._finalized:
            return  # Already finalized, nothing to do

        # Ensure the temporary path still exists before attempting move
        if not self.temp_path.exists():
            raise PathError(
                f"Temporary path {self.temp_path} no longer exists for finalization"
            )

        atomic_move(self.temp_path, self.target_path)
        self._finalized = True

    def cleanup_temp(self) -> None:
        """Explicitly cleanup temporary directory if not finalized."""
        if not self._finalized and self.temp_path.exists():
            cleanup_temp_path(self.temp_path)
