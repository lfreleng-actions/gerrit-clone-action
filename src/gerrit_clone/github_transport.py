# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""Authenticated HTTP session and REST transport for the GitHub API.

Owns the token, the shared :class:`httpx.Client`, and the rate-limit
budget tracker, and turns HTTP responses into either decoded JSON or
the client's exception hierarchy.  Link-header pagination lives here
too because it is a property of the transport rather than of any
particular endpoint.
"""

from __future__ import annotations

import os
from typing import Any, Self

import httpx

from gerrit_clone.github_models import (
    GitHubAPIError,
    GitHubAuthError,
    GitHubNotFoundError,
    GitHubRateLimitError,
)
from gerrit_clone.logging import get_logger
from gerrit_clone.rate_limit import RateLimitBudget

logger = get_logger(__name__)

GITHUB_API_URL = "https://api.github.com"

# Where a GitHub token comes from, highest precedence first after an
# explicit --github-token.
GITHUB_TOKEN_VARIABLES = ("GERRIT_CLONE_TOKEN", "GITHUB_TOKEN")


def resolve_github_token(
    explicit: str | None, explicit_source: str = "--github-token"
) -> tuple[str | None, str | None]:
    """Return the GitHub token to use and where it came from.

    Precedence: an explicit token, then GERRIT_CLONE_TOKEN, then
    GITHUB_TOKEN.  The source is a name safe to log; the token never
    is.

    Args:
        explicit: Token given on the command line or in configuration
        explicit_source: Where *explicit* came from.  ``--github-token``
            also reads GERRIT_CLONE_TOKEN, so the CLI passes the
            source it observed.

    Returns:
        Tuple of (token, source name), or (None, None) when no source
        supplies one
    """
    if explicit:
        return explicit, explicit_source
    for variable in GITHUB_TOKEN_VARIABLES:
        if token := os.getenv(variable):
            return token, variable
    return None, None


def github_api_url(host: str) -> str:
    """Return the REST API base URL for a GitHub source host.

    ``host`` may carry a scheme (``https://github.com``), a port and
    an owner suffix (``github.com/ORG``); none changes the answer for
    github.com.  github.com and its subdomains use api.github.com,
    matched on the hostname alone (an explicit ``:443`` included) and
    exactly, so a lookalike such as ``github.com.evil.example`` doesn't.
    Any other host is GitHub Enterprise Server, whose API is under
    ``/api/v3`` on the same scheme, host and port.

    Args:
        host: Source host as configured

    Returns:
        REST API base URL, without a trailing slash
    """
    scheme, _, rest = host.rpartition("://")
    authority = rest.split("/", 1)[0]
    hostname = authority.rsplit(":", 1)[0] if ":" in authority else authority
    host_lower = hostname.lower()
    if host_lower == "github.com" or host_lower.endswith(".github.com"):
        return GITHUB_API_URL
    return f"{scheme or 'https'}://{authority}/api/v3"


def graphql_url_for(api_url: str) -> str:
    """Return the GraphQL endpoint that pairs with a REST API base URL.

    GitHub Enterprise Server serves REST under ``/api/v3`` and GraphQL
    under ``/api/graphql``; elsewhere GraphQL sits at ``/graphql``
    beside the REST API.

    Args:
        api_url: REST API base URL

    Returns:
        GraphQL endpoint URL
    """
    base = api_url.rstrip("/")
    if base.endswith("/api/v3"):
        return base.removesuffix("/v3") + "/graphql"
    return f"{base}/graphql"


def build_auth_headers(token: str | None) -> dict[str, str]:
    """Build the standard authenticated GitHub request headers.

    Args:
        token: GitHub personal access token

    Returns:
        Header dictionary shared by the sync and async clients
    """
    return {
        "Authorization": f"token {token}",
        "Accept": "application/vnd.github.v3+json",
        "User-Agent": "source-clone",
    }


class GitHubTransport:
    """HTTP session and request plumbing for the GitHub API."""

    def __init__(self, token: str | None = None, base_url: str | None = None) -> None:
        """Initialize GitHub API client.

        Args:
            token: GitHub personal access token. If None, will try
                   to read from GITHUB_TOKEN environment variable.
            base_url: REST API base URL; api.github.com when None.
                The token is sent only to this host.
        """
        self.token = token or os.getenv("GITHUB_TOKEN")
        if not self.token:
            raise GitHubAuthError(
                "GitHub token required. Set GITHUB_TOKEN environment "
                "variable or pass token parameter."
            )

        self.base_url = (base_url or GITHUB_API_URL).rstrip("/")
        self.graphql_url = graphql_url_for(self.base_url)
        self.client = httpx.Client(
            headers=build_auth_headers(self.token),
            timeout=30.0,
        )
        # Shared budget tracker for primary rate-limit awareness
        self._budget = RateLimitBudget()

    def __enter__(self) -> Self:
        """Context manager entry."""
        return self

    def __exit__(self, *args: Any) -> None:
        """Context manager exit."""
        self.close()

    def close(self) -> None:
        """Close the HTTP client."""
        self.client.close()

    @property
    def budget(self) -> RateLimitBudget:
        """Access the shared rate-limit budget tracker."""
        return self._budget

    def _request(
        self,
        method: str,
        endpoint: str,
        **kwargs: Any,
    ) -> dict[str, Any] | list[Any]:
        """Make API request with error handling.

        Args:
            method: HTTP method
            endpoint: API endpoint (without base URL)
            **kwargs: Additional arguments for httpx.request

        Returns:
            JSON response data

        Raises:
            GitHubAPIError: For API errors
        """
        url = f"{self.base_url}{endpoint}"
        logger.debug(f"GitHub API {method} {url}")

        try:
            response = self.client.request(method, url, **kwargs)

            # Record rate-limit headers from EVERY response
            self._budget.update_from_headers_sync(response.headers)

            self._handle_response_errors(response, endpoint)

            response.raise_for_status()

            # Handle empty responses (e.g., 204 No Content for DELETE)
            if response.status_code == 204 or not response.content:
                return {}

            try:
                result: dict[str, Any] | list[Any] = response.json()
                return result
            except ValueError as e:
                # Handle JSON decode errors (e.g., empty response bodies)
                logger.warning(
                    f"Failed to parse JSON response from {url}: {e}. "
                    "Returning empty dict."
                )
                return {}

        except httpx.HTTPError as e:
            raise GitHubAPIError(f"HTTP error: {e}") from e

    def _handle_response_errors(self, response: httpx.Response, endpoint: str) -> None:
        """Handle HTTP response errors and raise appropriate exceptions.

        Uses GitHub's official rate limit headers for reliable detection:
        - X-RateLimit-Remaining: Number of requests remaining
        - Retry-After: Seconds to wait before retrying
        Falls back to text matching only as a last resort.

        Args:
            response: HTTP response object
            endpoint: API endpoint for error messages

        Raises:
            GitHubAuthError: For 401 authentication errors
            GitHubNotFoundError: For 404 not found errors
            GitHubRateLimitError: For 403 rate limit errors
            GitHubAPIError: For other API errors
        """
        if response.status_code == 401:
            raise GitHubAuthError("Authentication failed. Check your GitHub token.")
        elif response.status_code == 404:
            raise GitHubNotFoundError(f"Resource not found: {endpoint}")
        elif response.status_code == 403:
            rate_limit_remaining = response.headers.get("X-RateLimit-Remaining")
            retry_after = response.headers.get("Retry-After")

            # Primary rate limit check: X-RateLimit-Remaining is "0"
            if rate_limit_remaining == "0":
                raise GitHubRateLimitError("GitHub API rate limit exceeded")

            # Secondary rate limit check: Retry-After header present
            if retry_after:
                raise GitHubRateLimitError(
                    f"GitHub API rate limit exceeded. Retry after {retry_after} seconds"
                )

            # Fallback: check response text (less reliable)
            if "rate limit" in response.text.lower():
                raise GitHubRateLimitError("GitHub API rate limit exceeded")

            raise GitHubAPIError(f"Forbidden: {response.text}")
        elif response.status_code >= 400:
            raise GitHubAPIError(
                f"GitHub API error {response.status_code}: {response.text}"
            )

    def _request_paginated(
        self,
        method: str,
        endpoint: str,
        per_page: int = 100,
        max_pages: int | None = None,
        **kwargs: Any,
    ) -> list[Any]:
        """Make paginated API requests and return all results.

        Handles GitHub's Link header pagination to fetch all pages.

        Args:
            method: HTTP method (usually GET)
            endpoint: API endpoint (without base URL)
            per_page: Number of items per page (max 100)
            max_pages: Optional maximum number of pages to fetch
            **kwargs: Additional arguments for httpx.request

        Returns:
            List of all items from all pages

        Raises:
            GitHubAPIError: For API errors
        """
        all_items: list[Any] = []
        page = 1

        while True:
            original_params = kwargs.get("params") or {}
            params = dict(original_params)
            params["per_page"] = per_page
            params["page"] = page
            kwargs["params"] = params

            url = f"{self.base_url}{endpoint}"
            logger.debug(f"GitHub API {method} {url} (page {page})")

            try:
                response = self.client.request(method, url, **kwargs)

                # Record rate-limit headers
                self._budget.update_from_headers_sync(response.headers)

                self._handle_response_errors(response, endpoint)
                response.raise_for_status()

                try:
                    data = response.json()
                except ValueError as e:
                    logger.warning(f"Failed to parse JSON response from {url}: {e}")
                    break

                if not data:
                    break
                if not isinstance(data, list):
                    logger.warning(
                        f"Expected list response from {url}, got {type(data)}"
                    )
                    break

                all_items.extend(data)

                if max_pages and page >= max_pages:
                    logger.debug(f"Reached max_pages limit: {max_pages}")
                    break

                link_header = response.headers.get("Link", "")
                if 'rel="next"' not in link_header:
                    logger.debug(f"No more pages (total pages: {page})")
                    break

                page += 1

            except httpx.HTTPError as e:
                raise GitHubAPIError(f"HTTP error: {e}") from e

        logger.debug(f"Fetched {len(all_items)} total items across {page} page(s)")
        return all_items
