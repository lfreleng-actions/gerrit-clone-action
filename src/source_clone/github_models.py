# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""Data types, errors and value helpers for the GitHub API client.

This module holds the pieces of the GitHub client that carry no
transport state: the repository record returned by the API, the
exception hierarchy raised by the client, and the small pure helpers
used to build request payloads.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any


@dataclass
class GitHubRepo:
    """Represents a GitHub repository."""

    name: str
    full_name: str
    html_url: str
    clone_url: str
    ssh_url: str
    private: bool
    description: str | None = None
    default_branch: str | None = None
    archived: bool = False

    @classmethod
    def from_api_response(cls, data: dict[str, Any]) -> GitHubRepo:
        """Create GitHubRepo from API response data.

        Args:
            data: GitHub API response dictionary

        Returns:
            GitHubRepo instance
        """
        return cls(
            name=data["name"],
            full_name=data["full_name"],
            html_url=data["html_url"],
            clone_url=data["clone_url"],
            ssh_url=data["ssh_url"],
            private=data["private"],
            description=data.get("description"),
            default_branch=data.get("default_branch"),
            archived=data.get("archived", False),
        )


class GitHubAPIError(Exception):
    """GitHub API error."""

    pass


class GitHubAuthError(GitHubAPIError):
    """GitHub API authentication error."""

    pass


class GitHubNotFoundError(GitHubAPIError):
    """GitHub API resource not found."""

    pass


class GitHubRateLimitError(GitHubAPIError):
    """GitHub API rate limit exceeded."""

    pass


def sanitize_description(
    description: str | None,
) -> str | None:
    """Sanitize repository description for GitHub API.

    GitHub does not allow control characters in descriptions.

    Args:
        description: Raw description text

    Returns:
        Sanitized description, or None if empty
    """
    if not description:
        return None

    sanitized = re.sub(r"[\x00-\x1F\x7F-\x9F]", " ", description)
    sanitized = re.sub(r"\s+", " ", sanitized)
    sanitized = sanitized.strip()

    if len(sanitized) > 350:
        sanitized = sanitized[:347] + "..."

    return sanitized if sanitized else None


def transform_gerrit_name_to_github(
    gerrit_name: str,
) -> str:
    """Transform Gerrit project name to valid GitHub repository name.

    Replaces forward slashes with hyphens.

    Args:
        gerrit_name: Gerrit project name

    Returns:
        GitHub-compatible repository name
    """
    return gerrit_name.replace("/", "-")


def build_create_repo_payload(
    name: str,
    description: str | None,
    private: bool,
) -> dict[str, Any]:
    """Build the JSON payload for a repository creation request.

    Args:
        name: Repository name
        description: Raw (unsanitized) repository description
        private: Whether repository should be private

    Returns:
        Payload dictionary for the GitHub create-repository endpoint
    """
    sanitized_desc = sanitize_description(description)
    if not sanitized_desc:
        sanitized_desc = f"Mirror of {name}"

    return {
        "name": name,
        "description": sanitized_desc,
        "private": private,
        "auto_init": False,
    }
