# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""Pacing and backoff shared by the async repository mutations.

Create and delete both consume tokens from a shared
:class:`TokenBucketLimiter` before every attempt and, on a 403,
report the hit to the limiter so that every concurrent task slows at
once.  Only the log wording differs between the two, so it is supplied
by the caller as a :class:`RateLimitMessages` value.
"""

from __future__ import annotations

import asyncio
import random
from dataclasses import dataclass
from typing import TYPE_CHECKING

from source_clone.logging import get_logger
from source_clone.rate_limit import parse_retry_after

if TYPE_CHECKING:
    import httpx

    from source_clone.rate_limit import TokenBucketLimiter

logger = get_logger(__name__)


@dataclass(frozen=True)
class RateLimitMessages:
    """Log formats and identifying arguments for a rate-limit backoff.

    Attributes:
        wait: Format used when the server supplied a ``Retry-After``
        backoff: Format used when computing our own backoff
        subject: Leading log arguments identifying the repository
    """

    wait: str
    backoff: str
    subject: tuple[object, ...]


def delete_rate_limit_messages(
    owner: str,
    repo_name: str,
) -> RateLimitMessages:
    """Build the rate-limit log formats for a delete attempt.

    Args:
        owner: Repository owner (user or org)
        repo_name: Repository name

    Returns:
        Log formats and identifying arguments
    """
    return RateLimitMessages(
        wait="⏳ Rate limited deleting %s/%s, server says wait %.0fs (attempt %d/%d)",
        backoff="⏳ Rate limited deleting %s/%s, backing off %ds (attempt %d/%d)",
        subject=(owner, repo_name),
    )


def create_rate_limit_messages(name: str) -> RateLimitMessages:
    """Build the rate-limit log formats for a create attempt.

    Args:
        name: Repository name

    Returns:
        Log formats and identifying arguments
    """
    return RateLimitMessages(
        wait="⏳ Rate limited creating %s, server says wait %.0fs (attempt %d/%d)",
        backoff="⏳ Rate limited creating %s, backing off %ds (attempt %d/%d)",
        subject=(name,),
    )


async def pace_attempt(
    attempt: int,
    rate_limiter: TokenBucketLimiter | None,
) -> None:
    """Consume rate-limiter tokens and jitter before a mutation attempt.

    Args:
        attempt: Zero-based attempt counter
        rate_limiter: Shared token-bucket rate limiter
    """
    # Consume 2 tokens for a mutation
    if rate_limiter:
        await rate_limiter.acquire(tokens=2.0)

    if attempt > 0:
        jitter = random.uniform(0.1, 0.5)
        await asyncio.sleep(jitter)


async def backoff_for_rate_limit(
    response: httpx.Response,
    attempt: int,
    max_retries: int,
    messages: RateLimitMessages,
    rate_limiter: TokenBucketLimiter | None = None,
) -> None:
    """Record a rate-limit hit and wait before the next attempt.

    Args:
        response: The 403 response that triggered the backoff
        attempt: Zero-based attempt counter
        max_retries: Maximum retry attempts
        messages: Log formats and identifying arguments
        rate_limiter: Shared token-bucket rate limiter
    """
    retry_after = parse_retry_after(response)

    if rate_limiter:
        await rate_limiter.record_rate_limit(
            retry_after=retry_after,
        )

    if retry_after:
        logger.warning(
            messages.wait,
            *messages.subject,
            retry_after,
            attempt + 1,
            max_retries + 1,
        )
        # The token bucket handles the global pause; no explicit sleep
        # needed here
        return

    backoff = min(90, 5 * (2**attempt))
    logger.warning(
        messages.backoff,
        *messages.subject,
        backoff,
        attempt + 1,
        max_retries + 1,
    )
    await asyncio.sleep(backoff)
