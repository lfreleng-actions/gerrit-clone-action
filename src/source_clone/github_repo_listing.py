# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""Bulk repository listing over the GitHub GraphQL API.

Pages through an owner's repositories with
:mod:`source_clone.github_graphql` and folds each page into one
mapping of repository name to details, the shape mirror, reset and
discovery all consume.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from source_clone.github_graphql import fetch_org_repos_page, fetch_owner_repos_page
from source_clone.github_models import GitHubNotFoundError
from source_clone.github_repo_ops import GitHubRepoOperations
from source_clone.logging import get_logger

if TYPE_CHECKING:
    from collections.abc import Callable

    from source_clone.github_graphql import GraphQLRepoPage

logger = get_logger(__name__)


def _absorb_repo_nodes(
    nodes: list[dict[str, Any]],
    repos_map: dict[str, dict[str, Any]],
    repos_without_default_branch: list[str],
) -> None:
    """Fold one page of GraphQL repository nodes into the result map.

    Args:
        nodes: Raw repository nodes from a GraphQL page
        repos_map: Accumulated mapping of repo name to repo details
        repos_without_default_branch: Accumulated names of repos with
            no default branch configured
    """
    for node in nodes:
        name = node["name"]
        default_branch_ref = node.get("defaultBranchRef")
        default_branch = None
        latest_commit_sha = None
        last_commit_date = None

        if default_branch_ref:
            default_branch = default_branch_ref.get("name")
            target = default_branch_ref.get("target")
            if target:
                latest_commit_sha = target.get("oid")
                last_commit_date = target.get("committedDate")
        else:
            repos_without_default_branch.append(name)
            logger.debug(
                "Repository %s has no default "
                "branch configured (may be a "
                "Gerrit parent project or an "
                "empty repo from a failed push)"
                "; latest_commit_sha will be "
                "unavailable",
                name,
            )

        repos_map[name] = {
            "name": name,
            "full_name": node["nameWithOwner"],
            "html_url": node["url"],
            "ssh_url": node["sshUrl"],
            "clone_url": node["url"],
            "private": node["isPrivate"],
            "archived": node.get("isArchived", False),
            "description": node.get("description"),
            "default_branch": default_branch,
            "latest_commit_sha": (latest_commit_sha),
            "last_commit_date": (last_commit_date),
        }


class GitHubRepoListing(GitHubRepoOperations):
    """Repository listing over GraphQL, on top of the REST client."""

    # GraphQL - list all repos with retry

    def list_all_repos_graphql(
        self,
        org: str,
        max_retries: int = 3,
    ) -> dict[str, dict[str, Any]]:
        """List all repositories in an org using GraphQL.

        Much faster than paginating through the REST API.  Now
        includes retry logic for transient errors (502, 503, etc.)
        that previously caused cascade failures when the result was
        empty.

        Args:
            org: Organization name
            max_retries: Retries per page on transient errors

        Returns:
            Dictionary mapping repo name to repo details; empty when
            ``org`` is not an organization
        """
        repos_map, _found = self._list_repos_graphql(
            org, fetch_org_repos_page, max_retries
        )
        return repos_map

    def list_owner_repos_graphql(
        self,
        login: str,
        max_retries: int = 3,
    ) -> dict[str, dict[str, Any]]:
        """List the repositories a user or organization owns, using GraphQL.

        Unlike :meth:`list_all_repos_graphql`, ``login`` may name a
        user, whose own repositories are listed (not those they only
        collaborate on).

        Args:
            login: User or organization login
            max_retries: Retries per page on transient errors

        Returns:
            Dictionary mapping repo name to repo details

        Raises:
            GitHubNotFoundError: GitHub knows no user or organization
                by that login, or the token cannot see it
            GitHubAuthError: GitHub rejected the token
            GitHubAPIError: A page failed after exhausting its retries,
                so the listing would be incomplete
        """
        repos_map, found = self._list_repos_graphql(
            login, fetch_owner_repos_page, max_retries
        )
        if not found:
            raise GitHubNotFoundError(
                f"GitHub has no user or organization '{login}' visible to this token"
            )
        return repos_map

    def _list_repos_graphql(
        self,
        login: str,
        fetch_page: Callable[..., GraphQLRepoPage | None],
        max_retries: int,
    ) -> tuple[dict[str, dict[str, Any]], bool]:
        """Page through an owner's repositories with *fetch_page*.

        Args:
            login: Owner login
            fetch_page: Fetches one page of the owner's repositories
            max_retries: Retries per page on transient errors

        Returns:
            Tuple of (repo name to repo details, whether the owner
            exists); a page that fails after its retries stops the
            listing but keeps what was collected
        """
        repos_map: dict[str, dict[str, Any]] = {}
        repos_without_default_branch: list[str] = []
        cursor: str | None = None
        has_next_page = True
        found = True

        while has_next_page:
            page = fetch_page(
                self.client,
                self._budget,
                login,
                cursor=cursor,
                max_retries=max_retries,
                graphql_url=self.graphql_url,
            )

            if page is None:
                # If a page failed after all retries, stop
                # paginating but keep what we have so far.
                logger.warning(
                    "Stopping GraphQL pagination after "
                    "page failure (collected %d repos so far)",
                    len(repos_map),
                )
                break

            if page.owner_missing:
                found = False
                break

            _absorb_repo_nodes(page.nodes, repos_map, repos_without_default_branch)

            has_next_page = page.has_next_page
            cursor = page.end_cursor

            logger.debug(
                "Fetched %d repos, total so far: %d, has_next: %s",
                len(page.nodes),
                len(repos_map),
                has_next_page,
            )

        if repos_without_default_branch:
            logger.info(
                "%d/%d repositories have no default branch "
                "configured (typically Gerrit parent projects "
                "with no code branches, or repos where a "
                "previous push failed): %s",
                len(repos_without_default_branch),
                len(repos_map),
                ", ".join(sorted(repos_without_default_branch)),
            )

        logger.debug(
            "Fetched %d repositories from %s using GraphQL",
            len(repos_map),
            login,
        )
        return repos_map, found
