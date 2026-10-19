# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2025 Matthew Watkins <mwatkins@linuxfoundation.org>

"""GitHub API integration for repository mirroring.

This module is the public entry point for the GitHub client.  It
composes the transport, single-repository and GraphQL listing layers
into :class:`GitHubAPI` and adds the rate-limit-aware batch mutations
that coordinate them.
"""

from __future__ import annotations

import asyncio
from typing import Any

import httpx

from source_clone.github_async_repo import create_repo_async, delete_repo_async
from source_clone.github_batch_support import (
    batch_retry_budget,
    build_batch_limiter,
    collect_batch_results,
    log_create_batch_outcome,
    log_delete_batch_outcome,
    scale_create_interval,
)
from source_clone.github_models import (
    GitHubAPIError,
    GitHubAuthError,
    GitHubNotFoundError,
    GitHubRateLimitError,
    GitHubRepo,
    sanitize_description,
    transform_gerrit_name_to_github,
)
from source_clone.github_repo_listing import GitHubRepoListing
from source_clone.github_repo_ops import get_default_org_or_user
from source_clone.github_transport import build_auth_headers
from source_clone.logging import get_logger
from source_clone.rate_limit import AsyncProgressCounter, TokenBucketLimiter

__all__ = [
    "GitHubAPI",
    "GitHubAPIError",
    "GitHubAuthError",
    "GitHubNotFoundError",
    "GitHubRateLimitError",
    "GitHubRepo",
    "get_default_org_or_user",
    "sanitize_description",
    "transform_gerrit_name_to_github",
]

logger = get_logger(__name__)

# Keep the old name available for backward compatibility in tests
_AsyncProgressCounter = AsyncProgressCounter

# Keep the old class name importable for existing test references
_AsyncRateLimiter = TokenBucketLimiter


class GitHubAPI(GitHubRepoListing):
    """GitHub API client for repository operations."""

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

        batch_retries = batch_retry_budget(len(repo_names))

        rate_limiter = build_batch_limiter(
            rate_limit_interval,
            max(3, min(10, len(repo_names) // 20)),
            shared_limiter,
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
            headers=build_auth_headers(self.token),
            timeout=30.0,
        ) as client:
            # Pre-flight budget check
            await budget.preflight_check(client, self.base_url)
            await budget.wait_if_exhausted()

            semaphore = asyncio.Semaphore(max_concurrent)

            async def delete_with_semaphore(
                repo_name: str,
            ) -> tuple[str, tuple[bool, str | None]]:
                async with semaphore:
                    result = await delete_repo_async(
                        self.base_url,
                        client,
                        owner,
                        repo_name,
                        max_retries=batch_retries,
                        rate_limiter=rate_limiter,
                        progress=progress,
                        budget=budget,
                    )
                    return repo_name, result

            tasks = [delete_with_semaphore(name) for name in repo_names]
            results: list[
                tuple[str, tuple[bool, str | None]] | BaseException
            ] = await asyncio.gather(*tasks, return_exceptions=True)

            results_map = collect_batch_results(
                results,
                "Delete task failed with exception",
            )
            log_delete_batch_outcome(results_map, repo_names)
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

        batch_retries = batch_retry_budget(len(repo_configs))

        effective_interval = scale_create_interval(
            len(repo_configs), rate_limit_interval
        )

        rate_limiter = build_batch_limiter(
            effective_interval,
            max(2, min(5, len(repo_configs) // 30)),
            shared_limiter,
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
            headers=build_auth_headers(self.token),
            timeout=30.0,
        ) as client:
            # Pre-flight budget check
            await budget.preflight_check(client, self.base_url)
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
                    result = await create_repo_async(
                        self.base_url,
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

            tasks = [create_with_semaphore(cfg) for cfg in repo_configs]
            results: list[
                tuple[
                    str,
                    tuple[GitHubRepo | None, str | None],
                ]
                | BaseException
            ] = await asyncio.gather(*tasks, return_exceptions=True)

            results_map = collect_batch_results(
                results,
                "Create task failed with exception",
            )
            log_create_batch_outcome(results_map, repo_configs)
            return results_map
