# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""Helpers for pushing a local mirror clone to a GitHub repository.

Covers push URL selection, credential handling via ``GIT_CONFIG_*``
environment variables, token redaction, and summarising git's push
output.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING
from urllib.parse import urlparse

from gerrit_clone.git_credential_env import build_token_auth_env
from gerrit_clone.logging import get_logger

if TYPE_CHECKING:
    from gerrit_clone.github_api import GitHubRepo

logger = get_logger(__name__)


@dataclass(frozen=True)
class PushSettings:
    """Authentication and transport settings for a mirror push."""

    github_token: str | None
    clone_timeout: int
    git_ssh_command: str | None


def sanitize_token(github_token: str | None, text: str) -> str:
    """Remove *github_token* from *text* if present.

    This must be applied to **all** git output (stdout *and* stderr)
    before logging or returning it, because git can include the
    credentialed URL in either stream.
    """
    if github_token and github_token in text:
        return text.replace(github_token, "***")
    return text


def build_push_url(settings: PushSettings, github_repo: GitHubRepo) -> str:
    """Build the push URL for a GitHub repository.

    When a github_token is available, returns the plain HTTPS clone
    URL (no credentials embedded).  Authentication is handled
    separately via environment variables in :func:`build_push_env`
    so that secrets never appear on the command line or in process
    listings.

    Falls back to the SSH URL when no token is available.

    Args:
        settings: Push authentication settings
        github_repo: Target GitHub repository

    Returns:
        Push URL string (plain HTTPS or SSH)

    Raises:
        ValueError: If ``clone_url`` is not HTTPS when a token is set.
    """
    if settings.github_token:
        parsed = urlparse(github_repo.clone_url)
        if parsed.scheme != "https":
            raise ValueError(
                f"Expected HTTPS clone URL for token auth, "
                f"got scheme '{parsed.scheme}' in: "
                f"{github_repo.clone_url}"
            )
        # Return the plain HTTPS URL — credentials are passed via
        # GIT_CONFIG_* environment variables in _push_to_github().
        logger.debug(f"Using HTTPS token auth for push to {github_repo.full_name}")
        return github_repo.clone_url
    else:
        # Fall back to SSH URL
        logger.debug(f"Using SSH for push to {github_repo.full_name}")
        return github_repo.ssh_url


def build_push_env(settings: PushSettings, push_url: str) -> dict[str, str]:
    """Build the git environment overrides used for a mirror push.

    Credentials are passed via ``GIT_CONFIG_COUNT`` /
    ``GIT_CONFIG_KEY_*`` / ``GIT_CONFIG_VALUE_*`` environment
    variables so the token never appears on the command line or
    in ``/proc`` process listings, and scoped to the push URL's origin
    so it is not offered to any other host.  The clone path
    authenticates the same way, through the same helper; see
    :mod:`gerrit_clone.git_credential_env`.
    """
    if settings.github_token:
        return build_token_auth_env(settings.github_token, push_url)
    if settings.git_ssh_command:
        # Only set GIT_SSH_COMMAND when using SSH push
        return {"GIT_SSH_COMMAND": settings.git_ssh_command}
    return {}


def log_push_success(github_repo: GitHubRepo, stdout: str, stderr: str) -> None:
    """Summarise a successful push.

    ``stdout`` and ``stderr`` must already be token-sanitized.
    """
    # Summarise push output; stderr can list every ref pushed
    # which is extremely verbose for repos with many branches.
    stderr_lines = stderr.strip().splitlines()
    ref_count = sum(
        1 for line in stderr_lines if line.strip().startswith("*") or "->" in line
    )
    if ref_count:
        logger.debug(
            "Push successful to %s (%d refs)",
            github_repo.full_name,
            ref_count,
        )
    else:
        logger.debug(
            "Push successful to %s (up to date)",
            github_repo.full_name,
        )
    if stdout.strip():
        logger.debug(
            "Push stdout for %s: %s",
            github_repo.full_name,
            stdout.strip(),
        )


def format_push_failure(stdout: str, stderr: str) -> str:
    """Build the error message for a failed ``git push``.

    Both streams must already be token-sanitized.
    """
    if stdout.strip():
        return f"Git push failed: {stderr} | stdout: {stdout}"
    return f"Git push failed: {stderr}"
