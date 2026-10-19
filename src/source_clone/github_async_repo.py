# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""Asynchronous per-repository create and delete operations.

These coroutines are the unit of work behind the batch operations.
Each one paces itself against a shared :class:`TokenBucketLimiter` so
that a 403 observed by any concurrent task immediately slows every
other task, and reports its outcome through a shared progress counter.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from source_clone.github_async_pacing import (
    backoff_for_rate_limit,
    create_rate_limit_messages,
    delete_rate_limit_messages,
    pace_attempt,
)
from source_clone.github_models import GitHubRepo, build_create_repo_payload
from source_clone.logging import get_logger
from source_clone.rate_limit import is_rate_limited

if TYPE_CHECKING:
    import httpx

    from source_clone.rate_limit import (
        AsyncProgressCounter,
        RateLimitBudget,
        TokenBucketLimiter,
    )

logger = get_logger(__name__)


async def _forbidden_delete_outcome(
    response: httpx.Response,
    owner: str,
    repo_name: str,
    attempt: int,
    max_retries: int,
    rate_limiter: TokenBucketLimiter | None = None,
) -> tuple[bool, str]:
    """Classify a 403 received while deleting a repository.

    Args:
        response: The 403 response
        owner: Repository owner (user or org)
        repo_name: Repository name
        attempt: Zero-based attempt counter
        max_retries: Maximum retry attempts
        rate_limiter: Shared token-bucket rate limiter

    Returns:
        Tuple of (should_retry, message).  When retrying, the message
        is the error to remember for the final failure report.
    """
    if not is_rate_limited(response):
        return False, f"Permission denied: {response.text}"

    if attempt >= max_retries:
        return False, f"Rate limited after {max_retries + 1} attempts: {response.text}"

    await backoff_for_rate_limit(
        response,
        attempt,
        max_retries,
        delete_rate_limit_messages(owner, repo_name),
        rate_limiter=rate_limiter,
    )
    return True, f"Rate limited: {response.text}"


async def _record_delete_failure(
    owner: str,
    repo_name: str,
    error: str,
    progress: AsyncProgressCounter | None,
) -> tuple[bool, str]:
    """Log and record a failed delete, returning the caller's result.

    Args:
        owner: Repository owner (user or org)
        repo_name: Repository name
        error: Error message to report
        progress: Optional shared progress counter

    Returns:
        Tuple of (False, error)
    """
    logger.error(f"✗ Failed to delete {owner}/{repo_name}: {error}")
    if progress:
        await progress.record(success=False, name=repo_name)
    return False, error


async def delete_repo_async(
    base_url: str,
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
        base_url: GitHub API base URL
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
    url = f"{base_url}/repos/{owner}/{repo_name}"
    logger.debug(f"Async DELETE {url}")

    last_error: str = "unknown error"

    for attempt in range(max_retries + 1):
        await pace_attempt(attempt, rate_limiter)

        try:
            response = await client.delete(url)

            if budget:
                await budget.update_from_headers(response.headers)

            if response.status_code in (204, 404):
                if attempt > 0:
                    logger.info(
                        f"✓ Deleted {owner}/{repo_name} "
                        f"(after {attempt} "
                        f"{'retry' if attempt == 1 else 'retries'})"
                    )
                else:
                    logger.info(f"✓ Deleted {owner}/{repo_name}")
                if rate_limiter:
                    await rate_limiter.record_success()
                if progress:
                    await progress.record(success=True, name=repo_name)
                return True, None

            if response.status_code == 403:
                should_retry, message = await _forbidden_delete_outcome(
                    response,
                    owner,
                    repo_name,
                    attempt,
                    max_retries,
                    rate_limiter=rate_limiter,
                )
                if should_retry:
                    last_error = message
                    continue
                error = message
            else:
                error = f"Status {response.status_code}: {response.text}"

            return await _record_delete_failure(owner, repo_name, error, progress)

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
            return await _record_delete_failure(owner, repo_name, error, progress)

    error = f"Failed after {max_retries + 1} attempts: {last_error}"
    return await _record_delete_failure(owner, repo_name, error, progress)


async def _retry_forbidden_create(
    response: httpx.Response,
    name: str,
    attempt: int,
    max_retries: int,
    rate_limiter: TokenBucketLimiter | None = None,
) -> bool:
    """Decide whether a 403 received while creating is retryable.

    Args:
        response: The 403 response
        name: Repository name
        attempt: Zero-based attempt counter
        max_retries: Maximum retry attempts
        rate_limiter: Shared token-bucket rate limiter

    Returns:
        True if the caller should retry the create
    """
    if not (is_rate_limited(response) and attempt < max_retries):
        return False

    await backoff_for_rate_limit(
        response,
        attempt,
        max_retries,
        create_rate_limit_messages(name),
        rate_limiter=rate_limiter,
    )
    return True


async def _record_create_failure(
    name: str,
    error: str,
    progress: AsyncProgressCounter | None,
) -> tuple[GitHubRepo | None, str | None]:
    """Log and record a failed create, returning the caller's result.

    Args:
        name: Repository name
        error: Error message to report
        progress: Optional shared progress counter

    Returns:
        Tuple of (None, error)
    """
    logger.error(f"✗ Failed to create {name}: {error}")
    if progress:
        await progress.record(success=False, name=name)
    return None, error


async def _resolve_existing_repo(
    base_url: str,
    client: httpx.AsyncClient,
    name: str,
    org: str | None,
    rate_limiter: TokenBucketLimiter | None = None,
    progress: AsyncProgressCounter | None = None,
    budget: RateLimitBudget | None = None,
) -> tuple[GitHubRepo | None, str | None]:
    """Handle a 422 by fetching the repository that already exists.

    Args:
        base_url: GitHub API base URL
        client: Async HTTP client to use
        name: Repository name
        org: Organization name
        rate_limiter: Shared token-bucket rate limiter
        progress: Optional shared progress counter
        budget: Optional shared rate-limit budget tracker

    Returns:
        Tuple of (GitHubRepo or None, error_message or None)
    """
    error = "Repository already exists"
    logger.warning(f"⚠ {name} already exists (delete may have failed)")
    # A 422 is still an API call that counts against
    # the secondary rate limit — record success so
    # the limiter can pace correctly.
    if rate_limiter:
        await rate_limiter.record_success()
    if org:
        try:
            get_url = f"{base_url}/repos/{org}/{name}"
            if rate_limiter:
                await rate_limiter.acquire(tokens=1.0)
            get_response = await client.get(get_url)
            if budget:
                await budget.update_from_headers(get_response.headers)
            if get_response.status_code == 200:
                data = get_response.json()
                logger.info(f"  Retrieved existing repo: {name}")
                return (
                    GitHubRepo.from_api_response(data),
                    None,
                )
        except Exception as ex:
            logger.warning(f"Failed to retrieve existing repo details for {name}: {ex}")
    if progress:
        await progress.record(success=False, name=name)
    return None, error


async def create_repo_async(
    base_url: str,
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
        base_url: GitHub API base URL
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
    payload = build_create_repo_payload(name, description, private)

    url = f"{base_url}/orgs/{org}/repos" if org else f"{base_url}/user/repos"

    logger.debug(f"Async POST {url}")

    last_error: str = "unknown error"

    for attempt in range(max_retries + 1):
        await pace_attempt(attempt, rate_limiter)

        try:
            response = await client.post(url, json=payload)

            if budget:
                await budget.update_from_headers(response.headers)

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
                    await progress.record(success=True, name=name)
                return (
                    GitHubRepo.from_api_response(data),
                    None,
                )

            if response.status_code == 422:
                return await _resolve_existing_repo(
                    base_url,
                    client,
                    name,
                    org,
                    rate_limiter=rate_limiter,
                    progress=progress,
                    budget=budget,
                )

            error = f"Status {response.status_code}: {response.text}"
            if response.status_code == 403 and await _retry_forbidden_create(
                response,
                name,
                attempt,
                max_retries,
                rate_limiter=rate_limiter,
            ):
                last_error = error
                continue

            return await _record_create_failure(name, error, progress)

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
            return await _record_create_failure(name, error, progress)

    error = f"Failed after {max_retries + 1} attempts: {last_error}"
    return await _record_create_failure(name, error, progress)
