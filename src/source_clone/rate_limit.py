# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2025 Matthew Watkins <mwatkins@linuxfoundation.org>

"""Proactive rate-limit management for the GitHub API.

This module provides two complementary mechanisms:

1.  **RateLimitBudget** - tracks the primary (token-based) rate-limit
    budget by reading ``X-RateLimit-*`` headers from every GitHub API
    response and by querying ``GET /rate_limit`` before batch
    operations.  It can calculate a safe request interval and
    proactively pause when the remaining budget drops below a
    configurable threshold.

2.  **TokenBucketLimiter** - an async token-bucket rate limiter that
    replaces the previous ``_AsyncRateLimiter``.  Tokens are added at
    a steady rate derived from the current budget; each API call
    consumes one or more tokens (mutations cost more).  When the
    bucket is empty callers block until a token is available.  On a
    403 secondary-rate-limit response the bucket is drained and the
    refill rate is slashed, affecting *all* concurrent tasks
    immediately.  Recovery is time-based, not success-count-based.
"""

from __future__ import annotations

import asyncio
import time as time_mod

from source_clone.logging import get_logger
from source_clone.rate_limit_budget import RateLimitBudget, RateLimitSnapshot
from source_clone.rate_limit_helpers import (
    extract_rate_limit_info,
    is_rate_limited,
    parse_retry_after,
)
from source_clone.rate_limit_progress import AsyncProgressCounter

logger = get_logger(__name__)

# TokenBucketLimiter - async token-bucket for secondary rate limits


class TokenBucketLimiter:
    """Async token-bucket rate limiter for GitHub API calls.

    Unlike a fixed-interval limiter, a token bucket allows short
    bursts when the bucket is full while enforcing an average rate
    over time.  This maps well to GitHub's secondary rate limit,
    which uses a rolling window of "content-creation points".

    Key properties:

    *   **Mutations cost more** - ``acquire(tokens=2)`` for write
        operations (POST/DELETE) vs ``acquire(tokens=1)`` for reads.
    *   **Adaptive** - on a 403, :meth:`record_rate_limit` drains
        the bucket and slashes the refill rate, immediately affecting
        *all* concurrent callers.
    *   **Time-based recovery** - after a configurable cooldown
        period the refill rate ramps back up, regardless of
        success/failure count.
    *   **Global Retry-After** - when any task receives a
        ``Retry-After`` header, :meth:`set_global_retry_after` blocks
        *all* tasks for that duration.
    """

    def __init__(
        self,
        rate: float = 1.0,
        burst: int = 5,
        min_rate: float = 0.05,
        recovery_seconds: float = 120.0,
    ) -> None:
        """Initialise the token bucket.

        Args:
            rate: Tokens added per second (steady-state throughput).
            burst: Maximum tokens the bucket can hold.
            min_rate: Minimum refill rate even when severely limited.
            recovery_seconds: Seconds after a rate-limit hit before
                the refill rate is fully restored.
        """
        # Validate inputs before any clamping so callers get clear
        # feedback on obviously wrong values.
        if rate <= 0:
            raise ValueError(f"rate must be positive, got {rate}")
        if burst <= 0:
            raise ValueError(f"burst must be positive, got {burst}")
        if min_rate <= 0:
            raise ValueError(f"min_rate must be positive, got {min_rate}")
        if recovery_seconds <= 0:
            raise ValueError(
                f"recovery_seconds must be positive, got {recovery_seconds}"
            )
        # Ensure min_rate never exceeds rate so that a rate-limit
        # event cannot *increase* throughput.
        min_rate = min(min_rate, rate)
        self._rate = rate
        self._base_rate = rate
        self._min_rate = min_rate
        self._burst = burst
        self._tokens = float(burst)
        self._last_refill = time_mod.monotonic()
        self._lock = asyncio.Lock()

        # Recovery tracking
        self._recovery_seconds = recovery_seconds
        self._last_rate_limit_time: float = 0.0
        self._rate_limit_count: int = 0

        # Global retry-after
        self._global_retry_until: float = 0.0

    @property
    def rate(self) -> float:
        """Current refill rate (tokens per second)."""
        return self._rate

    @property
    def tokens(self) -> float:
        """Current token count (approximate, no locking)."""
        return self._tokens

    def _refill(self) -> None:
        """Add tokens based on elapsed time (call under lock)."""
        now = time_mod.monotonic()
        elapsed = now - self._last_refill
        self._tokens = min(self._burst, self._tokens + elapsed * self._rate)
        self._last_refill = now

        # Time-based recovery: gradually restore rate toward baseline
        if self._rate < self._base_rate and self._last_rate_limit_time > 0:
            since_limit = now - self._last_rate_limit_time
            if since_limit >= self._recovery_seconds:
                # Full recovery
                old = self._rate
                self._rate = self._base_rate
                self._rate_limit_count = 0
                if old != self._rate:
                    logger.info(
                        "⚙️  Token bucket fully recovered: rate %.3f → %.3f tokens/s",
                        old,
                        self._rate,
                    )
            elif since_limit > self._recovery_seconds * 0.5:
                # Partial recovery (linear ramp toward baseline)
                progress = since_limit / self._recovery_seconds
                target = self._min_rate + (self._base_rate - self._min_rate) * progress
                if target > self._rate:
                    old = self._rate
                    self._rate = min(target, self._base_rate)
                    if abs(old - self._rate) > 0.01:
                        logger.info(
                            "⚙️  Token bucket recovering: "
                            "rate %.3f → %.3f tokens/s "
                            "(%.0f%% recovered)",
                            old,
                            self._rate,
                            progress * 100,
                        )

    async def acquire(self, tokens: float = 1.0) -> float:
        """Wait until *tokens* are available, then consume them.

        Args:
            tokens: Number of tokens to consume.  Use ``1.0`` for
                read operations and ``2.0`` for mutations.  Must be
                greater than 0 and less than or equal to the burst
                size.

        Returns:
            Number of seconds spent waiting.
        """
        if tokens <= 0 or tokens > self._burst:
            raise ValueError(
                f"tokens must be in the range (0, {self._burst}], got {tokens!r}"
            )

        total_wait = 0.0

        while True:
            async with self._lock:
                now = time_mod.monotonic()

                # Honour global retry-after on every iteration so
                # that newly-set global pauses affect tasks that
                # are already waiting for tokens.
                if self._global_retry_until > now:
                    wait = self._global_retry_until - now
                    is_global_wait = True
                else:
                    self._refill()

                    if self._tokens >= tokens:
                        self._tokens -= tokens
                        return total_wait

                    # Calculate how long to wait for enough tokens
                    deficit = tokens - self._tokens
                    wait = deficit / self._rate if self._rate > 0 else 1.0

                    # Cap individual waits at 60s to allow periodic
                    # re-evaluation (rate may recover mid-wait).
                    wait = min(wait, 60.0)
                    is_global_wait = False

            if wait > 0:
                if is_global_wait:
                    logger.debug(
                        "Global retry-after: sleeping %.1fs",
                        wait,
                    )
                await asyncio.sleep(wait)
                total_wait += wait
            else:
                # Yield control briefly even if the computed
                # wait is 0.
                await asyncio.sleep(0)

    async def record_success(self) -> None:
        """Record a successful API call.

        In the token-bucket model, success doesn't directly affect
        the refill rate — recovery is time-based.  This method exists
        for symmetry and for future use (e.g. metrics).
        """
        # No-op; recovery is handled by _refill() based on elapsed time

    async def record_rate_limit(
        self,
        retry_after: float | None = None,
    ) -> None:
        """Record a secondary rate-limit (403) response.

        Drains the bucket and reduces the refill rate.  If
        *retry_after* is provided, sets a global pause.

        Args:
            retry_after: Optional seconds from the ``Retry-After``
                response header.
        """
        async with self._lock:
            self._rate_limit_count += 1
            self._last_rate_limit_time = time_mod.monotonic()

            # Drain the bucket so no queued task can fire immediately
            self._tokens = 0.0

            # Slash the rate — each consecutive hit halves it further,
            # down to the minimum.
            old_rate = self._rate
            self._rate = max(self._min_rate, self._rate * 0.5)
            logger.warning(
                "⚙️  Token bucket rate-limited (#%d): "
                "rate %.3f → %.3f tokens/s, bucket drained",
                self._rate_limit_count,
                old_rate,
                self._rate,
            )

            # Set global retry-after if provided
            if retry_after is not None and retry_after > 0:
                deadline = time_mod.monotonic() + retry_after
                if deadline > self._global_retry_until:
                    self._global_retry_until = deadline
                    logger.warning(
                        "🛑 Global retry-after set: %.0fs (all tasks will pause)",
                        retry_after,
                    )

    async def set_global_retry_after(self, seconds: float) -> None:
        """Force all tasks to pause for *seconds*.

        This is called when any task receives a ``Retry-After``
        header.  Every task that subsequently calls :meth:`acquire`
        will sleep until the deadline.

        A non-positive *seconds* value is treated as a no-op so
        that callers do not need to guard against zero/negative
        durations parsed from malformed headers.

        Args:
            seconds: Duration to pause (from now).  Values <= 0
                are ignored.
        """
        if seconds <= 0:
            return
        async with self._lock:
            self._tokens = 0.0
            deadline = time_mod.monotonic() + seconds
            if deadline > self._global_retry_until:
                self._global_retry_until = deadline
                logger.warning(
                    "🛑 Global retry-after: all tasks pausing %.0fs",
                    seconds,
                )

    async def adjust_rate_from_budget(self, budget: RateLimitBudget) -> None:
        """Adjust the refill rate based on the current budget.

        Call this after a :meth:`RateLimitBudget.preflight_check` or
        periodically during long-running batch operations.

        Args:
            budget: The shared budget tracker.
        """
        snap = budget.snapshot
        if snap.remaining <= 0 or snap.seconds_until_reset <= 0:
            return

        safe = snap.safe_interval(safety_margin=0.15)
        suggested_rate = 1.0 / safe if safe > 0 else self._base_rate

        async with self._lock:
            # Only slow down, never speed up beyond the base rate
            capped = min(suggested_rate, self._base_rate)
            if capped < self._rate:
                old = self._rate
                self._rate = max(self._min_rate, capped)
                logger.info(
                    "⚙️  Token bucket adjusted from budget: "
                    "rate %.3f → %.3f tokens/s "
                    "(%d/%d remaining, %.0fs to reset)",
                    old,
                    self._rate,
                    snap.remaining,
                    snap.limit,
                    snap.seconds_until_reset,
                )


__all__ = [
    "AsyncProgressCounter",
    "RateLimitBudget",
    "RateLimitSnapshot",
    "TokenBucketLimiter",
    "extract_rate_limit_info",
    "is_rate_limited",
    "parse_retry_after",
]
