# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""Check that a freshly cloned repository holds no authentication token.

Clones authenticate through the process environment, so nothing this
tool does puts a token in ``.git/config``.  This module is the check on
that: it runs only when the remote URL turns out to hold the configured
token anyway, which takes an externally supplied ``project.clone_url``
that carried one in, and destroys the clone rather than leaving it.
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
    """Rewrite the remote without its token, or destroy the clone.

    Reached only when the remote URL holds the configured token, which
    this tool never puts there -- so the URL came in that way, through
    an externally supplied ``project.clone_url``.

    A credential in that URL no longer gets this far in the shapes
    :func:`gerrit_clone.github_url_safety.reject_credentialed_url`
    recognises, which is the canonical statement of the policy: this
    run's configured token wherever it sits, a password under any
    scheme, a username over HTTP(S), and non-empty HTTP(S) path
    parameters, query strings or fragments.  An SSH username is not a
    credential and is allowed, as is anything that cannot be
    classified -- scp-style URLs above all.

    What remains is narrow, and worth being exact about rather than
    calling this a general backstop: a token that is *not* this run's
    configured one, sitting somewhere structurally invisible such as
    the path. This function does not detect that either -- it acts only
    on the configured token -- so the case it really covers is that
    same token reaching the remote by a route the pre-clone check did
    not anticipate.

    In that case there is no clean replacement to write: the only
    candidate is that same value.  So this refuses, and the clone is
    destroyed rather than kept with a credential in ``.git/config``.

    Failure here is deliberately fatal to the clone: a repository left
    on disk holding a token is the outcome being prevented.

    Args:
        repo_path: Path to cloned repository
        project: Project that was cloned
        config: Configuration with github_token

    Raises:
        RuntimeError: If token removal fails (security-critical operation)
    """
    try:
        clean_url = project.clone_url or project.https_url(config.base_url)
        if config.github_token and config.github_token in clean_url:
            # The replacement comes from ``project.clone_url``, which is
            # the externally supplied value that carried the token in,
            # so writing it back would leave the credential exactly
            # where it is.  Refusing takes the destroy-the-clone path
            # below, rather than reporting a scrub that never happened.
            raise ValueError("the only available remote URL still contains the token")

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
