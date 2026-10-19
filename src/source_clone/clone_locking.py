# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""Cross-platform advisory file locking for clone operations.

Locking is implemented with atomic file creation rather than fcntl/msvcrt
because the atomic-create approach behaves consistently across every platform
the action runs on.
"""

from __future__ import annotations

import os
import sys
import time
from contextlib import contextmanager, suppress
from typing import TYPE_CHECKING

from source_clone.logging import get_logger

# Cross-platform file locking imports
if sys.platform == "win32":
    pass
else:
    pass

if TYPE_CHECKING:
    from collections.abc import Generator
    from pathlib import Path

logger = get_logger(__name__)


def _try_create_lock(lock_file_path: Path) -> bool:
    """Attempt to claim the lock by creating its file exclusively.

    Args:
        lock_file_path: Path to the lock file

    Returns:
        True if this caller created the lock, False if it already existed
    """
    try:
        # Try to create lock file exclusively (atomic operation)
        with lock_file_path.open("x") as lock_file:
            lock_file.write(f"pid:{os.getpid()}\ntime:{time.time()}\n")
            lock_file.flush()
    except FileExistsError:
        return False
    return True


def _clear_stale_lock(lock_file_path: Path, timeout: float) -> bool:
    """Remove the lock file if it was abandoned by a dead process.

    A lock whose mtime is older than *timeout* is assumed to be stale, since a
    live holder would have released it well within that window.

    Args:
        lock_file_path: Path to the lock file
        timeout: Age beyond which a lock is considered stale

    Returns:
        True if a stale lock was removed and acquisition should be retried
    """
    try:
        # If lock file is older than timeout, it might be stale
        if lock_file_path.exists():
            stat = lock_file_path.stat()
            if time.time() - stat.st_mtime > timeout:
                # Try to remove stale lock
                lock_file_path.unlink()
                return True
    except OSError:
        logger.debug(
            "Could not inspect or remove stale lock %s",
            lock_file_path,
            exc_info=True,
        )
    return False


def _acquire_lock(lock_file_path: Path, timeout: float) -> None:
    """Block until the lock is held, clearing stale locks once timed out.

    Args:
        lock_file_path: Path to the lock file
        timeout: Maximum time to wait for lock acquisition

    Raises:
        OSError: If lock cannot be acquired within timeout
    """
    start_time = time.time()

    while True:
        if _try_create_lock(lock_file_path):
            return

        # Lock file already exists, check if it's stale
        if time.time() - start_time > timeout:
            if _clear_stale_lock(lock_file_path, timeout):
                continue  # Try again

            raise OSError(f"Could not acquire lock within {timeout}s: {lock_file_path}")

        # Wait briefly before retry
        time.sleep(0.05)  # 50ms wait


@contextmanager
def _file_lock(
    lock_file_path: Path, timeout: float = 30.0
) -> Generator[None, None, None]:
    """Cross-platform file locking using atomic file creation.

    This uses atomic file creation as the locking mechanism, which is more
    reliable across platforms than fcntl/msvcrt locking.

    Args:
        lock_file_path: Path to the lock file
        timeout: Maximum time to wait for lock acquisition

    Yields:
        None when lock is acquired

    Raises:
        OSError: If lock cannot be acquired within timeout
    """
    lock_file_path.parent.mkdir(parents=True, exist_ok=True)
    acquired = False

    try:
        _acquire_lock(lock_file_path, timeout)
        acquired = True

        yield

    finally:
        # Clean up lock file if we acquired it
        if acquired and lock_file_path.exists():
            with suppress(OSError):
                lock_file_path.unlink()
