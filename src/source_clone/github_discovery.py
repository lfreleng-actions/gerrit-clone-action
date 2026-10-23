# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2025 Matthew Watkins <mwatkins@linuxfoundation.org>

"""GitHub organization/user repository discovery.

This module discovers all repositories in a GitHub organization or user account
using the GitHub REST or GraphQL API.
"""

from __future__ import annotations

from typing import Any

from source_clone.github_api import GitHubAPI, GitHubAPIError, GitHubAuthError
from source_clone.github_transport import github_api_url, resolve_github_token
from source_clone.logging import get_logger
from source_clone.models import Config, Project, ProjectState, SourceType

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

    # The CLI logs where the token came from; here it is already resolved
    # into config.github_token, so its origin would be mislabelled.
    token, _source = resolve_github_token(config.github_token)

    try:
        # From the host, not config.base_url: --base-url and
        # GERRIT_BASE_URL name the Gerrit API, and the token goes to
        # whichever host this is.
        with GitHubAPI(token=token, base_url=github_api_url(config.host)) as api:
            logger.debug(f"Fetching repositories owned by {org_or_user} over GraphQL")
            repos_list = list(api.list_owner_repos_graphql(org_or_user).values())

            # Convert to Project models
            projects = _convert_to_projects(repos_list)

            # Apply filters
            filtered_projects, skipped_archived = _apply_filters(projects, config)

            stats = {
                "total": len(filtered_projects),
                "filtered": len(projects) - len(filtered_projects),
                "skipped": skipped_archived,
                "discovery_method": "github_api",
                "org_or_user": org_or_user,
            }

            logger.debug(
                f"Discovered {len(filtered_projects)} repositories in {org_or_user}"
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


def _apply_filters(
    projects: list[Project], config: Config
) -> tuple[list[Project], int]:
    """Apply configuration filters to project list.

    Supports shell-style wildcards (``*``, ``?``, ``[seq]``) and
    hierarchical matching for both include and exclude patterns.

    Args:
        projects: List of projects to filter
        config: Configuration with filter settings

    Returns:
        Tuple of (filtered projects, number skipped as archived). An
        archived repository the name filters already removed is not
        counted as skipped for being archived.
    """
    filtered = projects

    # Apply include/exclude project filtering (supports wildcards)
    include_pats = getattr(config, "include_projects", None)
    exclude_pats = getattr(config, "exclude_projects", None)
    if include_pats or exclude_pats:
        from source_clone.models import filter_projects  # noqa: PLC0415

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
    skipped_archived = 0
    if config.skip_archived:
        original_count = len(filtered)
        filtered = [p for p in filtered if p.state != ProjectState.READ_ONLY]
        skipped_archived = original_count - len(filtered)
        if skipped_archived > 0:
            logger.debug(f"Skipped {skipped_archived} archived repositories")

    return filtered, skipped_archived


def detect_github_source(host: str) -> bool:
    """Detect if a host string represents a GitHub source.

    Args:
        host: Host string (URL or hostname)

    Returns:
        True if this appears to be a GitHub source
    """
    host_lower = host.lower()

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
