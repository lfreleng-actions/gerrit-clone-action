# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2025 Matthew Watkins <mwatkins@linuxfoundation.org>

"""GitHub organization/user repository discovery.

This module discovers all repositories in a GitHub organization or user account
using the GitHub REST or GraphQL API.
"""

from __future__ import annotations

import os
from typing import Any

from gerrit_clone.github_api import GitHubAPI, GitHubAPIError, GitHubAuthError
from gerrit_clone.logging import get_logger
from gerrit_clone.models import Config, Project, ProjectState, SourceType

logger = get_logger(__name__)


def discover_github_repositories(
    config: Config,
) -> tuple[list[Project], dict[str, Any]]:
    """Discover all repositories in a GitHub organization or user account.

    Args:
        config: Configuration with GitHub settings

    Returns:
        Tuple of (list of Projects, statistics dict)

    Raises:
        GitHubAPIError: If GitHub API access fails
        ValueError: If configuration is invalid
    """
    if config.source_type != SourceType.GITHUB:
        raise ValueError(f"Expected source_type GITHUB, got {config.source_type}")

    # Determine organization/user from config
    org_or_user = config.github_org or _extract_org_from_host(config.host)

    if not org_or_user:
        raise ValueError(
            "Could not determine GitHub organization/user. "
            "Use --github-org or provide github.com/ORG URL"
        )

    logger.debug(f"Discovering repositories in GitHub org/user: {org_or_user}")

    # Get token from config or environment
    # Priority: explicit config > GERRIT_CLONE_TOKEN > GITHUB_TOKEN
    token: str | None
    if config.github_token:
        token = config.github_token
    else:
        token = os.getenv("GERRIT_CLONE_TOKEN") or os.getenv("GITHUB_TOKEN")

    try:
        with GitHubAPI(token=token) as api:
            # Try GraphQL organization API first - it's faster and more efficient
            # If it fails (empty results), fall back to REST user API
            logger.debug(
                f"Attempting to fetch repositories for {org_or_user} "
                "using GraphQL organization API"
            )
            repos_data = api.list_all_repos_graphql(org_or_user)

            if repos_data:
                # GraphQL succeeded - this is an organization
                logger.debug(
                    f"Successfully fetched {len(repos_data)} repositories "
                    f"from organization {org_or_user}"
                )
                repos_list = list(repos_data.values())
            else:
                # GraphQL returned empty - likely a user account, not an org
                logger.debug(
                    f"GraphQL organization API returned no results for {org_or_user}. "
                    "Falling back to REST user API."
                )
                github_repos = api.list_repos(org=None)
                repos_list = [
                    {
                        "name": r.name,
                        "full_name": r.full_name,
                        "html_url": r.html_url,
                        "ssh_url": r.ssh_url,
                        "clone_url": r.clone_url,
                        "private": r.private,
                        "description": r.description,
                        "default_branch": None,
                    }
                    for r in github_repos
                ]
                logger.debug(
                    f"Successfully fetched {len(repos_list)} repositories "
                    f"from user account {org_or_user}"
                )

            # Convert to Project models
            projects = _convert_to_projects(repos_list)

            # Apply filters
            filtered_projects = _apply_filters(projects, config)

            # Compute how many archived (READ_ONLY) projects were skipped by filtering
            archived_total = sum(
                1 for p in projects if p.state == ProjectState.READ_ONLY
            )
            archived_remaining = sum(
                1 for p in filtered_projects if p.state == ProjectState.READ_ONLY
            )
            skipped_archived = archived_total - archived_remaining

            stats = {
                "total": len(filtered_projects),
                "filtered": len(projects) - len(filtered_projects),
                "skipped": skipped_archived,
                "discovery_method": "github_api",
                "org_or_user": org_or_user,
            }

            logger.debug(
                f"Discovered {len(filtered_projects)} repositories " f"in {org_or_user}"
            )

            return filtered_projects, stats

    except GitHubAuthError as e:
        logger.error(f"GitHub authentication failed: {e}")
        raise
    except GitHubAPIError as e:
        logger.error(f"GitHub API error: {e}")
        raise
    except Exception as e:
        logger.error(f"Unexpected error during GitHub discovery: {e}")
        raise


def _extract_org_from_host(host: str) -> str | None:
    """Extract organization/user from GitHub URL or host.

    Args:
        host: GitHub host (e.g., 'github.com/lfreleng-actions' or 'github.com')

    Returns:
        Organization/user name or None if not found
    """
    # Remove protocol if present
    host = host.replace("https://", "").replace("http://", "")

    # Split by / and look for org after domain
    parts = host.split("/")

    if len(parts) >= 2:
        # github.com/ORG or enterprise.github.com/ORG
        return parts[1]

    return None


def _convert_to_projects(repos: list[dict[str, Any]]) -> list[Project]:
    """Convert GitHub repository data to Project models.

    Args:
        repos: List of repository data from GitHub API

    Returns:
        List of Project instances
    """
    projects: list[Project] = []

    for repo in repos:
        # GitHub repos are always "active" - use archived flag if available
        state = ProjectState.ACTIVE
        if repo.get("archived", False):
            state = ProjectState.READ_ONLY

        # Store GitHub metadata including latest commit SHA for smart refresh
        metadata = {
            "latest_commit_sha": repo.get("latest_commit_sha"),
            "full_name": repo.get("full_name"),
            "html_url": repo.get("html_url"),
            "private": repo.get("private"),
        }

        project = Project(
            name=repo["name"],
            state=state,
            description=repo.get("description"),
            source_type=SourceType.GITHUB,
            clone_url=repo.get("clone_url") or repo.get("html_url"),
            ssh_url_override=repo.get("ssh_url"),
            default_branch=repo.get("default_branch"),
            metadata=metadata,
            web_links=[
                {
                    "name": "GitHub",
                    "url": repo.get("html_url", ""),
                }
            ]
            if repo.get("html_url")
            else None,
        )

        projects.append(project)

    return projects


def _apply_filters(projects: list[Project], config: Config) -> list[Project]:
    """Apply configuration filters to project list.

    Supports shell-style wildcards (``*``, ``?``, ``[seq]``) and
    hierarchical matching for both include and exclude patterns.

    Args:
        projects: List of projects to filter
        config: Configuration with filter settings

    Returns:
        Filtered list of projects
    """
    filtered = projects

    # Apply include/exclude project filtering (supports wildcards)
    include_pats = getattr(config, "include_projects", None)
    exclude_pats = getattr(config, "exclude_projects", None)
    if include_pats or exclude_pats:
        from gerrit_clone.models import filter_projects

        before_count = len(filtered)
        filtered = filter_projects(
            filtered,
            include_patterns=include_pats or None,
            exclude_patterns=exclude_pats or None,
        )
        parts: list[str] = []
        if include_pats:
            parts.append(f"include={sorted(include_pats)}")
        if exclude_pats:
            parts.append(f"exclude={sorted(exclude_pats)}")
        logger.debug(
            f"Project filter: kept {len(filtered)}/{before_count} repositories "
            f"({', '.join(parts)})"
        )

    # Filter archived repositories if skip_archived is True
    if config.skip_archived:
        original_count = len(filtered)
        filtered = [p for p in filtered if p.state != ProjectState.READ_ONLY]
        skipped = original_count - len(filtered)
        if skipped > 0:
            logger.debug(f"Skipped {skipped} archived repositories")

    return filtered


def detect_github_source(host: str) -> bool:
    """Detect if a host string represents a GitHub source.

    Args:
        host: Host string (URL or hostname)

    Returns:
        True if this appears to be a GitHub source
    """
    host_lower = host.lower()

    # Check for github.com or common GitHub Enterprise patterns
    github_indicators = [
        "github.com",
        "github.io",
        "ghe.",
        "github.",
    ]

    return any(indicator in host_lower for indicator in github_indicators)


def parse_github_url(url: str) -> tuple[str | None, str | None]:
    """Parse a GitHub URL to extract host and org/user.

    Args:
        url: GitHub URL (e.g., 'https://github.com/lfreleng-actions')

    Returns:
        Tuple of (host, org_or_user) or (None, None) if not parseable
    """
    # Remove protocol
    url = url.replace("https://", "").replace("http://", "")

    # Split by /
    parts = url.split("/")

    if len(parts) >= 2:
        host = parts[0]
        org = parts[1]
        return host, org
    elif len(parts) == 1:
        # Just a hostname
        return parts[0], None

    return None, None
