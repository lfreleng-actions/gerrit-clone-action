# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""Removal of authentication tokens from a freshly cloned repository.

Tokens are embedded in the clone URL to authenticate HTTPS clones; this
module strips them back out of ``.git/config`` afterwards and destroys
the clone if that cannot be done.
"""

from __future__ import annotations

import shutil
import subprocess
from typing import TYPE_CHECKING

from gerrit_clone.logging import get_logger
from gerrit_clone.subprocess_tracking import run_tracked

if TYPE_CHECKING:
    from pathlib import Path

    from gerrit_clone.models import Config, Project

logger = get_logger(__name__)


def _delete_leaking_repo(repo_path: Path) -> None:
    """Delete *repo_path* after a failed token removal.

    The repository still holds the token in ``.git/config``, so it is
    destroyed rather than left on disk.
    """
    try:
        shutil.rmtree(repo_path)
        logger.warning(f"Deleted repository {repo_path} due to token removal failure")
    except Exception as cleanup_error:
        logger.error(
            f"Failed to cleanup repository after token removal failure: {cleanup_error}"
        )


def remove_token_from_remote_url(
    repo_path: Path,
    project: Project,
    config: Config,
) -> None:
    """Remove authentication token from git remote URL after cloning.

    Security measure to prevent token leakage:
    - Tokens are embedded in URLs for clone authentication
    - After successful clone, the remote URL is updated to remove the token
    - This prevents the token from being stored in .git/config
    - Subsequent git operations will use credential helper or SSH

    This operation is CRITICAL for security - if token removal fails, the clone
    operation will fail to prevent credential leakage in .git/config.

    Args:
        repo_path: Path to cloned repository
        project: Project that was cloned
        config: Configuration with github_token

    Raises:
        RuntimeError: If token removal fails (security-critical operation)
    """
    try:
        clean_url = project.clone_url or project.https_url(config.base_url)

        # Tracked and bounded: this runs after the clone, so a batch
        # that has given up must be able to stop it, and an unbounded
        # call here could hang the worker indefinitely.
        result = run_tracked(
            ["git", "remote", "set-url", "origin", clean_url],
            cwd=repo_path,
            timeout=config.clone_timeout,
        )
        if result.returncode != 0:
            raise subprocess.CalledProcessError(
                result.returncode,
                result.args,
                output=result.stdout,
                stderr=result.stderr,
            )
        logger.debug(f"Removed token from remote URL for {project.name}")
    except subprocess.CalledProcessError as e:
        # CRITICAL: Token removal failed - this is a security issue
        # Delete the repository to prevent credential leakage
        _delete_leaking_repo(repo_path)

        error_msg = (
            f"SECURITY: Failed to remove token from remote URL for {project.name}. "
            f"Repository deleted to prevent credential leakage. Error: {e.stderr.strip()}"
        )
        logger.error(error_msg)
        raise RuntimeError(error_msg) from e
    except Exception as e:
        # CRITICAL: Unexpected error during token removal
        # Delete the repository to prevent credential leakage
        _delete_leaking_repo(repo_path)

        error_msg = (
            f"SECURITY: Failed to update remote URL for {project.name}. "
            f"Repository deleted to prevent credential leakage. Error: {e}"
        )
        logger.error(error_msg)
        raise RuntimeError(error_msg) from e
