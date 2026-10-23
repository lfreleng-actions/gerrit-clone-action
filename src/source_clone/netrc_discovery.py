# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""
Discovery and permission checking for .netrc files.

Implements the standard netrc search order (explicit path, local
directory, home directory, Windows _netrc fallback) and warns about
world/group readable credential files on Unix.
"""

from __future__ import annotations

import logging
import os
import stat
from pathlib import Path

log = logging.getLogger(__name__)


def find_netrc_file(
    search_local: bool = True,
    explicit_path: Path | None = None,
) -> Path | None:
    """
    Find a .netrc file using standard search order.

    Search order:
    1. Explicit path (if provided)
    2. Local directory .netrc (if search_local=True)
    3. ~/.netrc
    4. ~/_netrc (Windows fallback)

    Args:
        search_local: Whether to search current directory first.
        explicit_path: Explicit path to a netrc file.

    Returns:
        Path to found netrc file, or None if not found.
    """
    if explicit_path is not None:
        if explicit_path.is_file():
            log.debug("Using explicit netrc file: %s", explicit_path)
            return explicit_path
        log.warning("Explicit netrc file not found: %s", explicit_path)
        return None

    candidates: list[Path] = []

    # Local directory
    if search_local:
        candidates.append(Path.cwd() / ".netrc")

    # Home directory
    home = Path.home()
    candidates.append(home / ".netrc")

    # Windows fallback
    if os.name == "nt":
        candidates.append(home / "_netrc")

    for candidate in candidates:
        if candidate.is_file():
            log.debug("Found netrc file: %s", candidate)
            return candidate

    log.debug("No netrc file found in search paths")
    return None


def check_netrc_permissions(path: Path) -> bool:
    """
    Check if netrc file has secure permissions.

    Warns if the file is readable by others (Unix only).

    Args:
        path: Path to the netrc file.

    Returns:
        True if permissions are secure, False otherwise.
    """
    if os.name == "nt":
        # Windows doesn't have the same permission model
        return True

    try:
        mode = path.stat().st_mode
    except OSError as e:
        log.warning("Could not check permissions for %s: %s", path, e)
        return True

    # Check if group or others have read permission
    if mode & (stat.S_IRGRP | stat.S_IROTH):
        log.warning(
            "Netrc file %s has insecure permissions. Consider running: chmod 600 %s",
            path,
            path,
        )
        return False
    return True


__all__ = [
    "check_netrc_permissions",
    "find_netrc_file",
]
