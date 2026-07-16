# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2025 Matthew Watkins <mwatkins@linuxfoundation.org>

"""GitHub API integration for repository mirroring."""

from __future__ import annotations

import asyncio
import os
import random
import re
import time as time_mod
from dataclasses import dataclass
from typing import Any

import httpx
from rich.console import Console

from gerrit_clone.logging import get_logger
from gerrit_clone.rate_limit import (
    AsyncProgressCounter,
    RateLimitBudget,
    TokenBucketLimiter,
    is_rate_limited,
    parse_retry_after,
)

logger = get_logger(__name__)


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


# Keep the old name available for backward compatibility in tests
_AsyncProgressCounter = AsyncProgressCounter

# Keep the old class name importable for existing test references
_AsyncRateLimiter = TokenBucketLimiter


class GitHubAPI:
    """GitHub API client for repository operations."""

    def __init__(self, token: str | None = None) -> None:
        """Initialize GitHub API client.

        Args:
            token: GitHub personal access token. If None, will try
                   to read from GITHUB_TOKEN environment variable.
        """
        self.token = token or os.getenv("GITHUB_TOKEN")
        if not self.token:
            raise GitHubAuthError(
                "GitHub token required. Set GITHUB_TOKEN environment "
                "variable or pass token parameter."
            )

        self.base_url = "https://api.github.com"
        self.client = httpx.Client(
            headers={
                "Authorization": f"token {self.token}",
                "Accept": "application/vnd.github.v3+json",
                "User-Agent": "gerrit-clone-mirror",
            },
            timeout=30.0,
        )
        # Shared budget tracker for primary rate-limit awareness
        self._budget = RateLimitBudget()

    def __enter__(self) -> GitHubAPI:
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

    def _handle_response_errors(
        self, response: httpx.Response, endpoint: str
    ) -> None:
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
            raise GitHubAuthError(
                "Authentication failed. Check your GitHub token."
            )
        elif response.status_code == 404:
            raise GitHubNotFoundError(f"Resource not found: {endpoint}")
        elif response.status_code == 403:
            rate_limit_remaining = response.headers.get(
                "X-RateLimit-Remaining"
            )
            retry_after = response.headers.get("Retry-After")

            # Primary rate limit check: X-RateLimit-Remaining is "0"
            if rate_limit_remaining == "0":
                raise GitHubRateLimitError(
                    "GitHub API rate limit exceeded"
                )

            # Secondary rate limit check: Retry-After header present
            if retry_after:
                raise GitHubRateLimitError(
                    f"GitHub API rate limit exceeded. "
                    f"Retry after {retry_after} seconds"
                )

            # Fallback: check response text (less reliable)
            if "rate limit" in response.text.lower():
                raise GitHubRateLimitError(
                    "GitHub API rate limit exceeded"
                )

            raise GitHubAPIError(f"Forbidden: {response.text}")
        elif response.status_code >= 400:
            raise GitHubAPIError(
                f"GitHub API error {response.status_code}: "
                f"{response.text}"
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
                    logger.warning(
                        f"Failed to parse JSON response from "
                        f"{url}: {e}"
                    )
                    break

                if not data:
                    break
                if not isinstance(data, list):
                    logger.warning(
                        f"Expected list response from {url}, "
                        f"got {type(data)}"
                    )
                    break

                all_items.extend(data)

                if max_pages and page >= max_pages:
                    logger.debug(
                        f"Reached max_pages limit: {max_pages}"
                    )
                    break

                link_header = response.headers.get("Link", "")
                if 'rel="next"' not in link_header:
                    logger.debug(
                        f"No more pages (total pages: {page})"
                    )
                    break

                page += 1

            except httpx.HTTPError as e:
                raise GitHubAPIError(f"HTTP error: {e}") from e

        logger.debug(
            f"Fetched {len(all_items)} total items "
            f"across {page} page(s)"
        )
        return all_items

    def get_authenticated_user(self) -> dict[str, Any]:
        """Get the authenticated user information.

        Returns:
            User information dictionary

        Raises:
            GitHubAPIError: For API errors
        """
        data = self._request("GET", "/user")
        if not isinstance(data, dict):
            raise GitHubAPIError(
                "Unexpected response type for user info"
            )
        return data

    def get_user_orgs(self) -> list[dict[str, Any]]:
        """Get organizations for the authenticated user.

        Returns:
            List of organization dictionaries

        Raises:
            GitHubAPIError: For API errors
        """
        data = self._request("GET", "/user/orgs")
        if not isinstance(data, list):
            raise GitHubAPIError(
                "Unexpected response type for user orgs"
            )
        return data

    def repo_exists(self, owner: str, repo_name: str) -> bool:
        """Check if a repository exists.

        Args:
            owner: Repository owner (user or org)
            repo_name: Repository name

        Returns:
            True if repository exists, False otherwise
        """
        try:
            self._request("GET", f"/repos/{owner}/{repo_name}")
            return True
        except GitHubNotFoundError:
            return False
        except GitHubAPIError as e:
            logger.warning(
                f"Error checking repository existence: {e}"
            )
            return False

    def get_repo(
        self, owner: str, repo_name: str
    ) -> GitHubRepo:
        """Get repository information.

        Args:
            owner: Repository owner (user or org)
            repo_name: Repository name

        Returns:
            GitHubRepo instance

        Raises:
            GitHubNotFoundError: If repository not found
            GitHubAPIError: For other API errors
        """
        data = self._request(
            "GET", f"/repos/{owner}/{repo_name}"
        )
        if not isinstance(data, dict):
            raise GitHubAPIError(
                "Unexpected response type for repo info"
            )
        return GitHubRepo.from_api_response(data)

    def create_repo(
        self,
        name: str,
        org: str | None = None,
        description: str | None = None,
        private: bool = False,
    ) -> GitHubRepo:
        """Create a new repository.

        Args:
            name: Repository name
            org: Organization name (if None, creates in user account)
            description: Repository description
            private: Whether repository should be private

        Returns:
            Created GitHubRepo instance

        Raises:
            GitHubAPIError: For API errors
        """
        sanitized_desc = sanitize_description(description)
        if not sanitized_desc:
            sanitized_desc = f"Mirror of {name}"

        payload = {
            "name": name,
            "description": sanitized_desc,
            "private": private,
            "auto_init": False,
        }

        endpoint = f"/orgs/{org}/repos" if org else "/user/repos"

        logger.info(
            f"Creating GitHub repository: {org}/{name}"
            if org
            else name
        )
        data = self._request("POST", endpoint, json=payload)
        if not isinstance(data, dict):
            raise GitHubAPIError(
                "Unexpected response type for repo creation"
            )
        return GitHubRepo.from_api_response(data)

    def list_repos(
        self,
        org: str | None = None,
        per_page: int = 100,
    ) -> list[GitHubRepo]:
        """List repositories for user or organization.

        Args:
            org: Organization name (if None, lists user repos)
            per_page: Number of results per page

        Returns:
            List of GitHubRepo instances

        Raises:
            GitHubAPIError: For API errors
        """
        repos: list[GitHubRepo] = []
        page = 1

        while True:
            endpoint = f"/orgs/{org}/repos" if org else "/user/repos"
            endpoint += f"?per_page={per_page}&page={page}"

            data = self._request("GET", endpoint)
            if not isinstance(data, list):
                raise GitHubAPIError(
                    "Unexpected response type for repo list"
                )

            if not data:
                break

            repos.extend(
                GitHubRepo.from_api_response(r) for r in data
            )
            page += 1

            if len(data) < per_page:
                break

        return repos

    def set_default_branch(
        self, owner: str, repo_name: str, branch: str
    ) -> bool:
        """Set the default branch for a repository.

        Args:
            owner: Repository owner (user or org)
            repo_name: Repository name
            branch: Branch name to set as default

        Returns:
            True if successful, False otherwise
        """
        logger.debug(
            "Setting default branch for %s/%s to '%s'",
            owner,
            repo_name,
            branch,
        )
        try:
            self._request(
                "PATCH",
                f"/repos/{owner}/{repo_name}",
                json={"default_branch": branch},
            )
            logger.info(
                "Set default branch for %s/%s to '%s'",
                owner,
                repo_name,
                branch,
            )
            return True
        except GitHubAPIError as exc:
            logger.warning(
                "Failed to set default branch for "
                "%s/%s to '%s': %s",
                owner,
                repo_name,
                branch,
                exc,
            )
            return False

    def delete_repo(self, owner: str, repo_name: str) -> None:
        """Delete a repository.

        Args:
            owner: Repository owner (user or org)
            repo_name: Repository name

        Raises:
            GitHubAPIError: For API errors
        """
        self._request("DELETE", f"/repos/{owner}/{repo_name}")

    async def _delete_repo_async_with_client(
        self,
        client: httpx.AsyncClient,
        owner: str,
        repo_name: str,
        max_retries: int = 5,
        rate_limiter: TokenBucketLimiter | None = None,
        progress: AsyncProgressCounter | None = None,
        budget: RateLimitBudget | None = None,
    ) -> tuple[bool, str | None]:
        """Delete a repository asynchronously with rate limiting.

        Uses a shared :class:`TokenBucketLimiter` to pace requests
        and avoid triggering GitHub's secondary rate limits.

        Args:
            client: Async HTTP client to use
            owner: Repository owner (user or org)
            repo_name: Repository name
            max_retries: Maximum retry attempts on rate-limit
            rate_limiter: Shared token-bucket rate limiter
            progress: Optional shared progress counter
            budget: Optional shared rate-limit budget tracker

        Returns:
            Tuple of (success, error_message)
        """
        url = f"{self.base_url}/repos/{owner}/{repo_name}"
        logger.debug(f"Async DELETE {url}")

        last_error: str = "unknown error"

        for attempt in range(max_retries + 1):
            # Consume 2 tokens for a mutation (DELETE)
            if rate_limiter:
                await rate_limiter.acquire(tokens=2.0)

            if attempt > 0:
                jitter = random.uniform(0.1, 0.5)
                await asyncio.sleep(jitter)

            try:
                response = await client.delete(url)

                if budget:
                    await budget.update_from_headers(
                        response.headers
                    )

                if response.status_code in (204, 404):
                    if attempt > 0:
                        logger.info(
                            f"✓ Deleted {owner}/{repo_name} "
                            f"(after {attempt} "
                            f"{'retry' if attempt == 1 else 'retries'})"
                        )
                    else:
                        logger.info(
                            f"✓ Deleted {owner}/{repo_name}"
                        )
                    if rate_limiter:
                        await rate_limiter.record_success()
                    if progress:
                        await progress.record(
                            success=True, name=repo_name
                        )
                    return True, None

                elif response.status_code == 403:
                    if is_rate_limited(response):
                        if attempt < max_retries:
                            retry_after = parse_retry_after(
                                response
                            )

                            if rate_limiter:
                                await rate_limiter.record_rate_limit(
                                    retry_after=retry_after,
                                )

                            if retry_after:
                                logger.warning(
                                    "⏳ Rate limited deleting "
                                    "%s/%s, server says wait "
                                    "%.0fs (attempt %d/%d)",
                                    owner,
                                    repo_name,
                                    retry_after,
                                    attempt + 1,
                                    max_retries + 1,
                                )
                                # The token bucket handles the
                                # global pause; no explicit sleep
                                # needed here
                            else:
                                backoff = min(
                                    90, 5 * (2**attempt)
                                )
                                logger.warning(
                                    "⏳ Rate limited deleting "
                                    "%s/%s, backing off %ds "
                                    "(attempt %d/%d)",
                                    owner,
                                    repo_name,
                                    backoff,
                                    attempt + 1,
                                    max_retries + 1,
                                )
                                await asyncio.sleep(backoff)

                            last_error = (
                                f"Rate limited: {response.text}"
                            )
                            continue

                        error = (
                            f"Rate limited after "
                            f"{max_retries + 1} attempts: "
                            f"{response.text}"
                        )
                    else:
                        error = (
                            f"Permission denied: {response.text}"
                        )
                    logger.error(
                        f"✗ Failed to delete "
                        f"{owner}/{repo_name}: {error}"
                    )
                    if progress:
                        await progress.record(
                            success=False, name=repo_name
                        )
                    return False, error
                else:
                    error = (
                        f"Status {response.status_code}: "
                        f"{response.text}"
                    )
                    logger.error(
                        f"✗ Failed to delete "
                        f"{owner}/{repo_name}: {error}"
                    )
                    if progress:
                        await progress.record(
                            success=False, name=repo_name
                        )
                    return False, error

            except Exception as e:
                if attempt < max_retries:
                    logger.warning(
                        f"⏳ Error deleting "
                        f"{owner}/{repo_name}: {e} "
                        f"(attempt {attempt + 1}/"
                        f"{max_retries + 1})"
                    )
                    last_error = f"Delete failed: {e}"
                    continue
                error = f"Delete failed: {e}"
                logger.error(
                    f"✗ Failed to delete "
                    f"{owner}/{repo_name}: {error}"
                )
                if progress:
                    await progress.record(
                        success=False, name=repo_name
                    )
                return False, error

        error = (
            f"Failed after {max_retries + 1} attempts: "
            f"{last_error}"
        )
        logger.error(
            f"✗ Failed to delete "
            f"{owner}/{repo_name}: {error}"
        )
        if progress:
            await progress.record(
                success=False, name=repo_name
            )
        return False, error

    async def _create_repo_async_with_client(  # noqa: PLR0911
        self,
        client: httpx.AsyncClient,
        name: str,
        org: str | None = None,
        description: str | None = None,
        private: bool = False,
        max_retries: int = 5,
        rate_limiter: TokenBucketLimiter | None = None,
        progress: AsyncProgressCounter | None = None,
        budget: RateLimitBudget | None = None,
    ) -> tuple[GitHubRepo | None, str | None]:
        """Create a repository asynchronously with rate limiting.

        Uses a shared :class:`TokenBucketLimiter` to pace requests
        and avoid triggering GitHub's secondary rate limits.  When a
        403 is returned the limiter's rate is slashed, immediately
        affecting all concurrent tasks.

        Args:
            client: Async HTTP client to use
            name: Repository name
            org: Organization name
            description: Repository description
            private: Whether repository should be private
            max_retries: Maximum retry attempts for rate limits
            rate_limiter: Shared token-bucket rate limiter
            progress: Optional shared progress counter
            budget: Optional shared rate-limit budget tracker

        Returns:
            Tuple of (GitHubRepo or None, error_message or None)
        """
        sanitized_desc = sanitize_description(description)
        if not sanitized_desc:
            sanitized_desc = f"Mirror of {name}"

        payload = {
            "name": name,
            "description": sanitized_desc,
            "private": private,
            "auto_init": False,
        }

        if org:
            url = f"{self.base_url}/orgs/{org}/repos"
        else:
            url = f"{self.base_url}/user/repos"

        logger.debug(f"Async POST {url}")

        last_error: str = "unknown error"

        for attempt in range(max_retries + 1):
            # Consume 2 tokens for a mutation (POST create)
            if rate_limiter:
                await rate_limiter.acquire(tokens=2.0)

            if attempt > 0:
                jitter = random.uniform(0.1, 0.5)
                await asyncio.sleep(jitter)

            try:
                response = await client.post(url, json=payload)

                if budget:
                    await budget.update_from_headers(
                        response.headers
                    )

                if response.status_code in (200, 201):
                    data = response.json()
                    if attempt > 0:
                        logger.info(
                            f"✓ Created {name} (after {attempt} "
                            f"{'retry' if attempt == 1 else 'retries'})"
                        )
                    else:
                        logger.info(f"✓ Created {name}")
                    if rate_limiter:
                        await rate_limiter.record_success()
                    if progress:
                        await progress.record(
                            success=True, name=name
                        )
                    return (
                        GitHubRepo.from_api_response(data),
                        None,
                    )

                elif response.status_code == 422:
                    error = "Repository already exists"
                    logger.warning(
                        f"⚠ {name} already exists "
                        "(delete may have failed)"
                    )
                    # A 422 is still an API call that counts against
                    # the secondary rate limit — record success so
                    # the limiter can pace correctly.
                    if rate_limiter:
                        await rate_limiter.record_success()
                    if org:
                        try:
                            get_url = (
                                f"{self.base_url}/repos/"
                                f"{org}/{name}"
                            )
                            if rate_limiter:
                                await rate_limiter.acquire(
                                    tokens=1.0
                                )
                            get_response = await client.get(
                                get_url
                            )
                            if budget:
                                await budget.update_from_headers(
                                    get_response.headers
                                )
                            if get_response.status_code == 200:
                                data = get_response.json()
                                logger.info(
                                    "  Retrieved existing "
                                    f"repo: {name}"
                                )
                                return (
                                    GitHubRepo.from_api_response(
                                        data
                                    ),
                                    None,
                                )
                        except Exception as ex:
                            logger.warning(
                                "Failed to retrieve existing "
                                f"repo details for {name}: {ex}"
                            )
                    if progress:
                        await progress.record(
                            success=False, name=name
                        )
                    return None, error

                elif response.status_code == 403:
                    if (
                        is_rate_limited(response)
                        and attempt < max_retries
                    ):
                        retry_after = parse_retry_after(response)

                        if rate_limiter:
                            await rate_limiter.record_rate_limit(
                                retry_after=retry_after,
                            )

                        if retry_after:
                            logger.warning(
                                "⏳ Rate limited creating %s, "
                                "server says wait %.0fs "
                                "(attempt %d/%d)",
                                name,
                                retry_after,
                                attempt + 1,
                                max_retries + 1,
                            )
                            # Token bucket handles global pause
                        else:
                            backoff = min(90, 5 * (2**attempt))
                            logger.warning(
                                "⏳ Rate limited creating %s, "
                                "backing off %ds "
                                "(attempt %d/%d)",
                                name,
                                backoff,
                                attempt + 1,
                                max_retries + 1,
                            )
                            await asyncio.sleep(backoff)

                        last_error = (
                            f"Status {response.status_code}: "
                            f"{response.text}"
                        )
                        continue
                    else:
                        error = (
                            f"Status {response.status_code}: "
                            f"{response.text}"
                        )
                        logger.error(
                            f"✗ Failed to create {name}: {error}"
                        )
                        if progress:
                            await progress.record(
                                success=False, name=name
                            )
                        return None, error
                else:
                    error = (
                        f"Status {response.status_code}: "
                        f"{response.text}"
                    )
                    logger.error(
                        f"✗ Failed to create {name}: {error}"
                    )
                    if progress:
                        await progress.record(
                            success=False, name=name
                        )
                    return None, error

            except Exception as e:
                if attempt < max_retries:
                    logger.warning(
                        f"⏳ Error creating {name}: {e} "
                        f"(attempt {attempt + 1}/"
                        f"{max_retries + 1})"
                    )
                    last_error = f"Create failed: {e}"
                    continue
                error = f"Create failed: {e}"
                logger.error(
                    f"✗ Failed to create {name}: {error}"
                )
                if progress:
                    await progress.record(
                        success=False, name=name
                    )
                return None, error

        error = (
            f"Failed after {max_retries + 1} attempts: "
            f"{last_error}"
        )
        logger.error(f"✗ Failed to create {name}: {error}")
        if progress:
            await progress.record(success=False, name=name)
        return None, error

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
            Dictionary mapping repo name to repo details
        """
        repos_map: dict[str, dict[str, Any]] = {}
        repos_without_default_branch: list[str] = []
        cursor = None
        has_next_page = True

        while has_next_page:
            safe_org = org.replace('"', '\\"')
            safe_cursor = (
                cursor.replace('"', '\\"') if cursor else None
            )
            after_clause = (
                f', after: "{safe_cursor}"'
                if safe_cursor
                else ""
            )
            query = f"""
            query {{
              organization(login: "{safe_org}") {{
                repositories(first: 100{after_clause}) {{
                  nodes {{
                    name
                    nameWithOwner
                    url
                    sshUrl
                    isPrivate
                    description
                    defaultBranchRef {{
                      name
                      target {{
                        ... on Commit {{
                          oid
                          committedDate
                        }}
                      }}
                    }}
                  }}
                  pageInfo {{
                    hasNextPage
                    endCursor
                  }}
                }}
              }}
            }}
            """

            url = "https://api.github.com/graphql"
            logger.debug(
                "GraphQL query for %s repos (cursor: %s)",
                org,
                cursor,
            )

            page_succeeded = False
            for retry in range(max_retries + 1):
                try:
                    # Use a longer timeout for GraphQL than the
                    # default 30 s client timeout.  GitHub's first
                    # query against a cold org cache regularly
                    # exceeds 30 s, causing nginx to return 502
                    # before the backend finishes.
                    response = self.client.post(
                        url,
                        json={"query": query},
                        timeout=60.0,
                    )

                    # Record rate-limit headers from GraphQL too
                    self._budget.update_from_headers_sync(
                        response.headers
                    )

                    # Transient HTTP errors → retry
                    if response.status_code in (
                        502,
                        503,
                        429,
                    ):
                        if retry < max_retries:
                            # Start at 5 s so a cold-cache 502
                            # has time to warm before we retry.
                            backoff = min(
                                30, 5 * (2**retry)
                            )
                            # Detailed message for file log only
                            logger.info(
                                "GraphQL transient error "
                                "%d for %s, retrying in "
                                "%ds (%d/%d)",
                                response.status_code,
                                org,
                                backoff,
                                retry + 1,
                                max_retries,
                            )
                            # Compact one-liner for the console
                            snap = self._budget.snapshot
                            budget_pct = (
                                f"{snap.budget_fraction:.1%}"
                                if snap.limit > 0
                                else "unknown"
                            )
                            Console(stderr=True).print(
                                f"[yellow]⚠️ GraphQL transient "
                                f"error {response.status_code} "
                                f"for GitHub Organisation: "
                                f"{org}, retrying in {backoff}s "
                                f"({retry + 1}/{max_retries}) "
                                f"[Budget remaining: "
                                f"{budget_pct}][/yellow]"
                            )
                            time_mod.sleep(backoff)
                            continue
                        logger.error(
                            "GraphQL failed after %d retries "
                            "(HTTP %d) for %s",
                            max_retries,
                            response.status_code,
                            org,
                        )
                        break

                    response.raise_for_status()
                    data = response.json()

                    if "errors" in data:
                        errors = data["errors"]
                        # Full error detail for file log only
                        logger.info(
                            "GraphQL errors: %s", errors
                        )
                        if retry < max_retries:
                            backoff = min(
                                30, 5 * (2**retry)
                            )
                            snap = self._budget.snapshot
                            budget_pct = (
                                f"{snap.budget_fraction:.1%}"
                                if snap.limit > 0
                                else "unknown"
                            )
                            Console(stderr=True).print(
                                f"[yellow]⚠️ GraphQL error "
                                f"for GitHub Organisation: "
                                f"{org}, retrying in {backoff}s "
                                f"({retry + 1}/{max_retries}) "
                                f"[Budget remaining: "
                                f"{budget_pct}][/yellow]"
                            )
                            time_mod.sleep(backoff)
                            continue
                        break

                    data_payload = data.get("data")
                    org_data = (
                        data_payload.get("organization")
                        if data_payload
                        else None
                    )
                    if not org_data:
                        logger.warning(
                            f"No organization data for {org}"
                        )
                        has_next_page = False
                        page_succeeded = True
                        break

                    repos_data = org_data.get(
                        "repositories", {}
                    )
                    nodes = repos_data.get("nodes", [])
                    page_info = repos_data.get("pageInfo", {})

                    for node in nodes:
                        name = node["name"]
                        default_branch_ref = node.get(
                            "defaultBranchRef"
                        )
                        default_branch = None
                        latest_commit_sha = None
                        last_commit_date = None

                        if default_branch_ref:
                            default_branch = (
                                default_branch_ref.get("name")
                            )
                            target = default_branch_ref.get(
                                "target"
                            )
                            if target:
                                latest_commit_sha = target.get(
                                    "oid"
                                )
                                last_commit_date = target.get(
                                    "committedDate"
                                )
                        else:
                            repos_without_default_branch.append(
                                name
                            )
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
                            "full_name": node[
                                "nameWithOwner"
                            ],
                            "html_url": node["url"],
                            "ssh_url": node["sshUrl"],
                            "clone_url": node["url"],
                            "private": node["isPrivate"],
                            "description": node.get(
                                "description"
                            ),
                            "default_branch": default_branch,
                            "latest_commit_sha": (
                                latest_commit_sha
                            ),
                            "last_commit_date": (
                                last_commit_date
                            ),
                        }

                    has_next_page = page_info.get(
                        "hasNextPage", False
                    )
                    cursor = page_info.get("endCursor")

                    logger.debug(
                        "Fetched %d repos, total so far: %d, "
                        "has_next: %s",
                        len(nodes),
                        len(repos_map),
                        has_next_page,
                    )
                    page_succeeded = True
                    break

                except Exception as e:
                    if retry < max_retries:
                        backoff = min(30, 5 * (2**retry))
                        # Full traceback for file log only
                        logger.info(
                            "GraphQL query failed: %s "
                            "(retrying in %ds, %d/%d)",
                            e,
                            backoff,
                            retry + 1,
                            max_retries,
                        )
                        snap = self._budget.snapshot
                        budget_pct = (
                            f"{snap.budget_fraction:.1%}"
                            if snap.limit > 0
                            else "unknown"
                        )
                        Console(stderr=True).print(
                            f"[yellow]⚠️ GraphQL query "
                            f"failed for GitHub "
                            f"Organisation: {org}, "
                            f"retrying in {backoff}s "
                            f"({retry + 1}/{max_retries}) "
                            f"[Budget remaining: "
                            f"{budget_pct}][/yellow]"
                        )
                        time_mod.sleep(backoff)
                        continue
                    logger.error(
                        "GraphQL query failed after %d "
                        "retries: %s",
                        max_retries,
                        e,
                    )
                    break

            if not page_succeeded:
                # If a page failed after all retries, stop
                # paginating but keep what we have so far.
                logger.warning(
                    "Stopping GraphQL pagination after "
                    "page failure (collected %d repos so far)",
                    len(repos_map),
                )
                break

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
            org,
        )
        return repos_map

    # Batch operations with token-bucket rate limiting

    async def batch_delete_repos(
        self,
        owner: str,
        repo_names: list[str],
        max_concurrent: int = 10,
        rate_limit_interval: float = 0.5,
        shared_limiter: TokenBucketLimiter | None = None,
    ) -> dict[str, tuple[bool, str | None]]:
        """Delete multiple repositories with rate-limit-aware scheduling.

        Uses a :class:`TokenBucketLimiter` to pace requests.  When
        any task receives a 403 the limiter's rate is slashed,
        immediately slowing all concurrent tasks.

        Args:
            owner: Repository owner (user or org)
            repo_names: List of repository names to delete
            max_concurrent: Maximum tasks in flight at once
            rate_limit_interval: Baseline seconds between requests
            shared_limiter: Optional pre-existing limiter to share
                state across phases (e.g. delete → create).

        Returns:
            Dict mapping repo name to (success, error_message)
        """
        if not repo_names:
            return {}

        batch_retries = max(5, min(15, len(repo_names) // 10))

        # Derive token-bucket rate from interval (tokens/s).
        # Mutations cost 2 tokens each, so effective mutation
        # rate is rate / 2.
        rate = 1.0 / max(rate_limit_interval, 0.1)
        rate_limiter = shared_limiter or TokenBucketLimiter(
            rate=rate,
            burst=max(3, min(10, len(repo_names) // 20)),
            min_rate=0.02,
            recovery_seconds=120.0,
        )

        effective_delete_rate = rate_limiter.rate / 2.0
        logger.info(
            "Batch deleting %d repositories "
            "(max %d concurrent, ~%.2f tokens/s (~%.2f delete req/s)%s, "
            "max %d retries per repo)",
            len(repo_names),
            max_concurrent,
            rate_limiter.rate,
            effective_delete_rate,
            " [shared limiter]" if shared_limiter else "",
            batch_retries,
        )

        progress = AsyncProgressCounter(
            total=len(repo_names),
            label="Delete",
            report_every=max(1, len(repo_names) // 10),
        )

        budget = self._budget

        async with httpx.AsyncClient(
            headers={
                "Authorization": f"token {self.token}",
                "Accept": "application/vnd.github.v3+json",
                "User-Agent": "gerrit-clone-mirror",
            },
            timeout=30.0,
        ) as client:
            # Pre-flight budget check
            await budget.preflight_check(client)
            await budget.wait_if_exhausted()

            semaphore = asyncio.Semaphore(max_concurrent)

            async def delete_with_semaphore(
                repo_name: str,
            ) -> tuple[str, tuple[bool, str | None]]:
                async with semaphore:
                    result = (
                        await self._delete_repo_async_with_client(
                            client,
                            owner,
                            repo_name,
                            max_retries=batch_retries,
                            rate_limiter=rate_limiter,
                            progress=progress,
                            budget=budget,
                        )
                    )
                    return repo_name, result

            tasks = [
                delete_with_semaphore(name)
                for name in repo_names
            ]
            results: list[
                tuple[str, tuple[bool, str | None]]
                | BaseException
            ] = await asyncio.gather(
                *tasks, return_exceptions=True
            )

            results_map: dict[str, tuple[bool, str | None]] = {}
            for result in results:
                if isinstance(result, BaseException):
                    logger.error(
                        "Delete task failed with "
                        f"exception: {result}"
                    )
                    continue
                repo_name, (success, error) = result
                results_map[repo_name] = (success, error)

            success_count = sum(
                1 for s, _ in results_map.values() if s
            )
            failed_count = len(repo_names) - success_count

            if failed_count > 0:
                failed_repos = [
                    name
                    for name, (success, error) in (
                        results_map.items()
                    )
                    if not success
                ]
                logger.error(
                    "Batch delete: %d/%d successful, "
                    "%d FAILED",
                    success_count,
                    len(repo_names),
                    failed_count,
                )
                logger.error(f"Failed repos: {failed_repos}")
                for name in failed_repos[:5]:
                    _, error = results_map[name]
                    logger.error(f"  - {name}: {error}")
            else:
                logger.info(
                    "Batch delete completed: "
                    "%d/%d successful",
                    success_count,
                    len(repo_names),
                )

            return results_map

    async def batch_create_repos(
        self,
        org: str,
        repo_configs: list[dict[str, Any]],
        max_concurrent: int = 10,
        rate_limit_interval: float = 2.0,
        shared_limiter: TokenBucketLimiter | None = None,
    ) -> dict[str, tuple[GitHubRepo | None, str | None]]:
        """Create multiple repositories with rate-limit-aware scheduling.

        Uses a :class:`TokenBucketLimiter` to pace requests.  When
        any task receives a 403 the limiter's rate is slashed,
        immediately slowing all concurrent tasks.

        Args:
            org: Organization name
            repo_configs: List of repo config dicts with keys:
                name, description, private
            max_concurrent: Maximum tasks in flight at once
            rate_limit_interval: Baseline seconds between requests
            shared_limiter: Optional pre-existing limiter to share
                state across phases.

        Returns:
            Dict mapping repo name to (GitHubRepo or None, error)
        """
        if not repo_configs:
            return {}

        batch_retries = max(5, min(15, len(repo_configs) // 10))

        # Derive token-bucket rate from interval; scale for large
        # batches to be more conservative from the start.
        effective_interval = rate_limit_interval
        if len(repo_configs) > 100:
            effective_interval = max(effective_interval, 3.0)
        if len(repo_configs) > 200:
            effective_interval = max(effective_interval, 4.0)

        # Mutations cost 2 tokens each, so effective mutation
        # rate is rate / 2.
        rate = 1.0 / max(effective_interval, 0.1)
        rate_limiter = shared_limiter or TokenBucketLimiter(
            rate=rate,
            burst=max(2, min(5, len(repo_configs) // 30)),
            min_rate=0.02,
            recovery_seconds=120.0,
        )

        effective_create_rate = rate_limiter.rate / 2.0
        logger.info(
            "Batch creating %d repositories "
            "(max %d concurrent, ~%.2f tokens/s (~%.2f create req/s)%s, "
            "max %d retries per repo)",
            len(repo_configs),
            max_concurrent,
            rate_limiter.rate,
            effective_create_rate,
            " [shared limiter]" if shared_limiter else "",
            batch_retries,
        )

        progress = AsyncProgressCounter(
            total=len(repo_configs),
            label="Create",
            report_every=max(1, len(repo_configs) // 10),
        )

        budget = self._budget

        async with httpx.AsyncClient(
            headers={
                "Authorization": f"token {self.token}",
                "Accept": "application/vnd.github.v3+json",
                "User-Agent": "gerrit-clone-mirror",
            },
            timeout=30.0,
        ) as client:
            # Pre-flight budget check
            await budget.preflight_check(client)
            await budget.wait_if_exhausted()

            semaphore = asyncio.Semaphore(max_concurrent)

            async def create_with_semaphore(
                config: dict[str, Any],
            ) -> tuple[
                str,
                tuple[GitHubRepo | None, str | None],
            ]:
                async with semaphore:
                    name = config["name"]
                    result = await self._create_repo_async_with_client(
                        client,
                        name=name,
                        org=org,
                        description=config.get("description"),
                        private=config.get("private", False),
                        max_retries=batch_retries,
                        rate_limiter=rate_limiter,
                        progress=progress,
                        budget=budget,
                    )
                    return name, result

            tasks = [
                create_with_semaphore(cfg)
                for cfg in repo_configs
            ]
            results: list[
                tuple[
                    str,
                    tuple[GitHubRepo | None, str | None],
                ]
                | BaseException
            ] = await asyncio.gather(
                *tasks, return_exceptions=True
            )

            results_map: dict[
                str,
                tuple[GitHubRepo | None, str | None],
            ] = {}
            for result in results:
                if isinstance(result, BaseException):
                    logger.error(
                        "Create task failed with "
                        f"exception: {result}"
                    )
                    continue
                repo_name, (repo, error) = result
                results_map[repo_name] = (repo, error)

            success_count = sum(
                1
                for repo, _ in results_map.values()
                if repo is not None
            )
            failed_count = len(repo_configs) - success_count

            if failed_count > 0:
                failed_repos = [
                    cfg["name"]
                    for cfg in repo_configs
                    if results_map.get(
                        cfg["name"], (None, None)
                    )[0]
                    is None
                ]
                logger.warning(
                    "Batch create: %d/%d successful, "
                    "%d failed",
                    success_count,
                    len(repo_configs),
                    failed_count,
                )
                logger.warning(
                    f"Failed repos: {failed_repos[:10]}"
                )
            else:
                logger.info(
                    "Batch create completed: "
                    "%d/%d successful",
                    success_count,
                    len(repo_configs),
                )

            return results_map


# Module-level helpers


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


def get_default_org_or_user(
    api: GitHubAPI,
) -> tuple[str, bool]:
    """Get default organization or user for the authenticated token.

    Returns the first organization if available, otherwise returns
    the authenticated user's login.

    Args:
        api: GitHubAPI instance

    Returns:
        Tuple of (owner_name, is_org)

    Raises:
        GitHubAPIError: For API errors
    """
    orgs = api.get_user_orgs()
    if orgs:
        org_login = orgs[0]["login"]
        logger.info(f"Using default organization: {org_login}")
        return org_login, True

    user = api.get_authenticated_user()
    user_login = user["login"]
    logger.info(
        f"Using authenticated user account: {user_login}"
    )
    return user_login, False
