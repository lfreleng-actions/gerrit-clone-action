# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""GraphQL transport for bulk repository listing.

Paginating an owner's repositories over the REST API costs one request
per hundred repositories; the GraphQL endpoint returns the same data
far more cheaply.  This module owns the query text and the per-page
request; the retry policy for the transient errors (502/503/429 and
cold-cache timeouts) that GitHub returns for large organizations is in
:mod:`source_clone.github_graphql_retry`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from source_clone.github_graphql_retry import (
    TRANSIENT_STATUS_CODES,
    retry_after_exception,
    retry_graphql_errors,
    retry_transient_status,
)
from source_clone.github_models import GitHubAPIError, GitHubAuthError
from source_clone.logging import get_logger

if TYPE_CHECKING:
    import httpx

    from source_clone.rate_limit import RateLimitBudget

logger = get_logger(__name__)

GRAPHQL_URL = "https://api.github.com/graphql"


@dataclass(frozen=True)
class GraphQLRepoPage:
    """A single page of an owner's repositories.

    Attributes:
        nodes: Raw repository nodes returned by the query
        has_next_page: Whether a further page is available
        end_cursor: Cursor to pass to the next request
        owner_missing: True when the query returned no owner object
            at all, in which case the caller must stop paginating
            without treating the page as a failure
    """

    nodes: list[dict[str, Any]]
    has_next_page: bool
    end_cursor: str | None
    owner_missing: bool = False


@dataclass(frozen=True)
class PageRequest:
    """One GraphQL request for a page of an owner's repositories.

    Attributes:
        login: Owner login, for log and console messages
        root: Field under ``data`` that holds the owner
        query: GraphQL query text
        variables: GraphQL variables sent with the query
        strict: Raise when the page cannot be fetched, rather than
            return None; a rejected token raises at once
    """

    login: str
    root: str
    query: str
    variables: dict[str, Any] = field(default_factory=dict)
    strict: bool = False

    def payload(self) -> dict[str, Any]:
        """Return the JSON request body."""
        if self.variables:
            return {"query": self.query, "variables": self.variables}
        return {"query": self.query}


# One page of a RepositoryConnection: the fields every listing reads.
REPO_PAGE_FRAGMENT = """
            fragment RepoPage on RepositoryConnection {
              nodes {
                name
                nameWithOwner
                url
                sshUrl
                isPrivate
                isArchived
                description
                defaultBranchRef {
                  name
                  target {
                    ... on Commit {
                      oid
                      committedDate
                    }
                  }
                }
              }
              pageInfo {
                hasNextPage
                endCursor
              }
            }
            """

# Repositories a user or organization owns.  A user's repositories
# default to those they own *or collaborate on*, so users ask for OWNER
# only; an organization keeps the default it has always been listed
# with.  The login and cursor travel as variables, never as query text.
OWNER_REPOS_QUERY = (
    """
            query($login: String!, $cursor: String) {
              repositoryOwner(login: $login) {
                __typename
                ... on Organization {
                  repositories(first: 100, after: $cursor) { ...RepoPage }
                }
                ... on User {
                  repositories(
                    first: 100, after: $cursor, ownerAffiliations: [OWNER]
                  ) { ...RepoPage }
                }
              }
            }
            """
    + REPO_PAGE_FRAGMENT
)


def build_org_repos_query(org: str, cursor: str | None) -> str:
    """Build the GraphQL query for one page of organization repositories.

    Args:
        org: Organization name
        cursor: Cursor returned by the previous page, if any

    Returns:
        GraphQL query text with the org name and cursor escaped
    """
    safe_org = org.replace('"', '\\"')
    safe_cursor = cursor.replace('"', '\\"') if cursor else None
    after_clause = f', after: "{safe_cursor}"' if safe_cursor else ""
    return (
        f"""
            query {{
              organization(login: "{safe_org}") {{
                repositories(first: 100{after_clause}) {{ ...RepoPage }}
              }}
            }}
            """
        + REPO_PAGE_FRAGMENT
    )


def _extract_repo_page(data: dict[str, Any], request: PageRequest) -> GraphQLRepoPage:
    """Turn a successful GraphQL response into a page of repositories.

    Args:
        data: Decoded GraphQL response body
        request: The request the response answers

    Returns:
        The page of repository nodes and its pagination state
    """
    data_payload = data.get("data")
    owner_data = data_payload.get(request.root) if data_payload else None
    if not owner_data:
        logger.warning(f"No {request.root} data for {request.login}")
        return GraphQLRepoPage(
            nodes=[],
            has_next_page=False,
            end_cursor=None,
            owner_missing=True,
        )

    repos_data = owner_data.get("repositories", {})
    page_info = repos_data.get("pageInfo", {})
    return GraphQLRepoPage(
        nodes=repos_data.get("nodes", []),
        has_next_page=page_info.get("hasNextPage", False),
        end_cursor=page_info.get("endCursor"),
    )


def fetch_org_repos_page(
    client: httpx.Client,
    budget: RateLimitBudget,
    org: str,
    cursor: str | None = None,
    max_retries: int = 3,
    graphql_url: str = GRAPHQL_URL,
) -> GraphQLRepoPage | None:
    """Fetch one page of an organization's repositories over GraphQL.

    Retries transient errors so that a single failed page does not
    cascade into an empty result for the whole organization.

    Args:
        client: Shared authenticated HTTP client
        budget: Shared rate-limit budget tracker
        org: Organization name
        cursor: Cursor returned by the previous page, if any
        max_retries: Retries per page on transient errors
        graphql_url: GraphQL endpoint of the GitHub host

    Returns:
        The page, or None if it failed after exhausting the retries
    """
    request = PageRequest(
        login=org, root="organization", query=build_org_repos_query(org, cursor)
    )
    logger.debug("GraphQL query for %s repos (cursor: %s)", org, cursor)
    return _fetch_page(client, budget, request, max_retries, graphql_url)


def fetch_owner_repos_page(
    client: httpx.Client,
    budget: RateLimitBudget,
    login: str,
    cursor: str | None = None,
    max_retries: int = 3,
    graphql_url: str = GRAPHQL_URL,
) -> GraphQLRepoPage | None:
    """Fetch one page of the repositories a user or organization owns.

    An unknown login yields a page with ``owner_missing`` set, not a
    GraphQL error, so it is answered at once rather than retried.

    Args:
        client: Shared authenticated HTTP client
        budget: Shared rate-limit budget tracker
        login: User or organization login
        cursor: Cursor returned by the previous page, if any
        max_retries: Retries per page on transient errors
        graphql_url: GraphQL endpoint of the GitHub host

    Returns:
        The page

    Raises:
        GitHubAuthError: GitHub rejected the token
        GitHubAPIError: The page failed after exhausting the retries
    """
    request = PageRequest(
        login=login,
        root="repositoryOwner",
        query=OWNER_REPOS_QUERY,
        variables={"login": login, "cursor": cursor},
        strict=True,
    )
    logger.debug("GraphQL query for %s repos (cursor: %s)", login, cursor)
    return _fetch_page(client, budget, request, max_retries, graphql_url)


def _fetch_page(
    client: httpx.Client,
    budget: RateLimitBudget,
    request: PageRequest,
    max_retries: int,
    graphql_url: str,
) -> GraphQLRepoPage | None:
    """Post one GraphQL page request, retrying transient failures.

    Args:
        client: Shared authenticated HTTP client
        budget: Shared rate-limit budget tracker
        request: The page request to send
        max_retries: Retries per page on transient errors
        graphql_url: GraphQL endpoint of the GitHub host

    Returns:
        The page, or None if it failed after exhausting the retries
        and the request is not strict

    Raises:
        GitHubAuthError: GitHub rejected the token (strict requests)
        GitHubAPIError: The page failed after exhausting the retries
            (strict requests)
    """
    login = request.login
    for retry in range(max_retries + 1):
        try:
            # Use a longer timeout for GraphQL than the default 30 s
            # client timeout.  GitHub's first query against a cold org
            # cache regularly exceeds 30 s, causing nginx to return 502
            # before the backend finishes.
            response = client.post(
                graphql_url,
                json=request.payload(),
                timeout=60.0,
            )

            # Record rate-limit headers from GraphQL too
            budget.update_from_headers_sync(response.headers)

            # A rejected token never succeeds on retry
            if response.status_code == 401 and request.strict:
                raise GitHubAuthError(
                    "GitHub rejected the token (HTTP 401); check it is "
                    "valid and unexpired"
                )

            # Transient HTTP errors → retry
            if response.status_code in TRANSIENT_STATUS_CODES:
                if retry_transient_status(
                    response.status_code, login, retry, max_retries, budget
                ):
                    continue
                _give_up(request, f"HTTP {response.status_code}")
                return None

            response.raise_for_status()
            data = response.json()

            if "errors" in data:
                if retry_graphql_errors(
                    data["errors"], login, retry, max_retries, budget
                ):
                    continue
                _give_up(request, f"GraphQL errors: {data['errors']}")
                return None

            return _extract_repo_page(data, request)

        except GitHubAPIError:
            raise
        except Exception as e:
            if retry_after_exception(e, login, retry, max_retries, budget):
                continue
            _give_up(request, str(e))
            return None

    _give_up(request, "no attempt succeeded")
    return None


def _give_up(request: PageRequest, cause: str) -> None:
    """Raise for a strict page request that failed after its retries.

    A strict request raises, so a failed listing cannot pass for an
    empty one; for any other this does nothing, and the caller
    returns None.

    Raises:
        GitHubAPIError: The request is strict
    """
    if request.strict:
        raise GitHubAPIError(
            f"Listing repositories for {request.login} failed: {cause}"
        )
