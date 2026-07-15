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
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from gerrit_clone.logging import get_logger

if TYPE_CHECKING:
    import httpx

logger = get_logger(__name__)


# RateLimitBudget - primary rate-limit tracking


@dataclass
class RateLimitSnapshot:
    """Point-in-time snapshot of a GitHub rate-limit bucket.

    Attributes:
        limit: Total request budget for the window.
        remaining: Requests remaining in the window.
        reset_epoch: Unix timestamp when the window resets.
        used: Requests consumed so far.
        resource: Rate-limit resource category.
        observed_at: Monotonic timestamp when snapshot was taken.
    """

    limit: int = 5000
    remaining: int = 5000
    reset_epoch: float = 0.0
    used: int = 0
    resource: str = "core"
    observed_at: float = field(default_factory=time_mod.monotonic)

    @property
    def seconds_until_reset(self) -> float:
        """Wall-clock seconds until the rate-limit window resets."""
        now = time_mod.time()
        return max(0.0, self.reset_epoch - now)

    @property
    def budget_fraction(self) -> float:
        """Fraction of the budget still available (0.0-1.0)."""
        if self.limit <= 0:
            return 0.0
        return self.remaining / self.limit

    def safe_interval(self, safety_margin: float = 0.1) -> float:
        """Calculate a safe interval between requests.

        Distributes the remaining budget evenly over the time
        remaining in the window, reserving *safety_margin* of the
        budget as headroom.

        Args:
            safety_margin: Fraction of budget to keep in reserve.

        Returns:
            Recommended minimum seconds between requests.
        """
        usable = max(0, self.remaining - int(self.limit * safety_margin))
        secs = self.seconds_until_reset
        if usable <= 0 or secs <= 0:
            # Budget exhausted or window about to reset — be cautious
            return max(1.0, secs)
        return secs / usable


class RateLimitBudget:
    """Track GitHub's primary rate-limit budget across requests.

    Every response from the GitHub API carries ``X-RateLimit-*``
    headers.  By recording these we can calculate a safe pacing rate
    *before* we hit the limit, and proactively pause when the budget
    is running low.

    Thread-safety: asynchronous mutations are serialized via an internal
    ``asyncio.Lock`` so the budget can be safely shared across concurrent
    async tasks. Synchronous helpers do not take this lock and must be
    externally synchronized if used from multiple threads.
    """

    def __init__(
        self,
        low_threshold: float = 0.10,
        critical_threshold: float = 0.03,
    ) -> None:
        """Initialise the budget tracker.

        Args:
            low_threshold: Fraction below which pacing is slowed.
            critical_threshold: Fraction below which operations pause
                until the window resets.
        """
        self._snapshot = RateLimitSnapshot()
        self._lock = asyncio.Lock()
        self.low_threshold = low_threshold
        self.critical_threshold = critical_threshold

    @property
    def snapshot(self) -> RateLimitSnapshot:
        """Most recent snapshot (read without locking)."""
        return self._snapshot

    # -- update from response headers ------------------------------------

    async def update_from_headers(self, headers: httpx.Headers) -> None:
        """Extract rate-limit metadata from a GitHub response.

        Args:
            headers: Response headers from any GitHub API call.
        """
        remaining_str = headers.get("X-RateLimit-Remaining")
        if remaining_str is None:
            return  # Not a rate-limited endpoint

        try:
            remaining = int(remaining_str)
            limit = int(headers.get("X-RateLimit-Limit", "5000"))
            reset_epoch = float(headers.get("X-RateLimit-Reset", "0"))
            used = int(headers.get("X-RateLimit-Used", "0"))
            resource = headers.get("X-RateLimit-Resource", "core")
        except (ValueError, TypeError):
            return

        async with self._lock:
            self._snapshot = RateLimitSnapshot(
                limit=limit,
                remaining=remaining,
                reset_epoch=reset_epoch,
                used=used,
                resource=resource,
            )

        if remaining <= int(limit * self.critical_threshold):
            logger.warning(
                "🚨 Rate-limit budget critical: %d/%d remaining "
                "(resets in %.0fs)",
                remaining,
                limit,
                max(0, reset_epoch - time_mod.time()),
            )
        elif remaining <= int(limit * self.low_threshold):
            logger.info(
                "⚠️  Rate-limit budget low: %d/%d remaining "
                "(resets in %.0fs)",
                remaining,
                limit,
                max(0, reset_epoch - time_mod.time()),
            )

    def update_from_headers_sync(self, headers: httpx.Headers) -> None:
        """Synchronous variant for use from non-async code paths.

        Args:
            headers: Response headers from any GitHub API call.
        """
        remaining_str = headers.get("X-RateLimit-Remaining")
        if remaining_str is None:
            return

        try:
            remaining = int(remaining_str)
            limit = int(headers.get("X-RateLimit-Limit", "5000"))
            reset_epoch = float(headers.get("X-RateLimit-Reset", "0"))
            used = int(headers.get("X-RateLimit-Used", "0"))
            resource = headers.get("X-RateLimit-Resource", "core")
        except (ValueError, TypeError):
            return

        self._snapshot = RateLimitSnapshot(
            limit=limit,
            remaining=remaining,
            reset_epoch=reset_epoch,
            used=used,
            resource=resource,
        )

        if remaining <= int(limit * self.critical_threshold):
            logger.warning(
                "🚨 Rate-limit budget critical: %d/%d remaining "
                "(resets in %.0fs)",
                remaining,
                limit,
                max(0, reset_epoch - time_mod.time()),
            )
        elif remaining <= int(limit * self.low_threshold):
            logger.info(
                "⚠️  Rate-limit budget low: %d/%d remaining "
                "(resets in %.0fs)",
                remaining,
                limit,
                max(0, reset_epoch - time_mod.time()),
            )

    # -- pre-flight check ------------------------------------------------

    async def preflight_check(
        self, client: httpx.AsyncClient
    ) -> RateLimitSnapshot:
        """Query ``GET /rate_limit`` and return a fresh snapshot.

        This endpoint is free (does not count against the budget).

        Args:
            client: An authenticated ``httpx.AsyncClient``.

        Returns:
            Updated :class:`RateLimitSnapshot`.
        """
        try:
            response = await client.get("https://api.github.com/rate_limit")
            if response.status_code == 200:
                data = response.json()
                resources = data.get("resources", {})
                core = resources.get("core", {})
                graphql = resources.get("graphql", {})

                snap = RateLimitSnapshot(
                    limit=core.get("limit", 5000),
                    remaining=core.get("remaining", 5000),
                    reset_epoch=float(core.get("reset", 0)),
                    used=core.get("used", 0),
                    resource="core",
                )
                async with self._lock:
                    self._snapshot = snap

                logger.info(
                    "📊 Rate-limit budget: %d/%d remaining "
                    "(resets in %.0fs) | GraphQL: %d/%d",
                    snap.remaining,
                    snap.limit,
                    snap.seconds_until_reset,
                    graphql.get("remaining", 0),
                    graphql.get("limit", 0),
                )
                return snap
            else:
                logger.warning(
                    "Pre-flight rate-limit check returned %d",
                    response.status_code,
                )
        except Exception as exc:
            logger.warning("Pre-flight rate-limit check failed: %s", exc)

        return self._snapshot

    def preflight_check_sync(
        self, client: httpx.Client
    ) -> RateLimitSnapshot:
        """Synchronous variant of :meth:`preflight_check`.

        Args:
            client: An authenticated ``httpx.Client``.

        Returns:
            Updated :class:`RateLimitSnapshot`.
        """
        try:
            response = client.get("https://api.github.com/rate_limit")
            if response.status_code == 200:
                data = response.json()
                resources = data.get("resources", {})
                core = resources.get("core", {})
                graphql = resources.get("graphql", {})

                self._snapshot = RateLimitSnapshot(
                    limit=core.get("limit", 5000),
                    remaining=core.get("remaining", 5000),
                    reset_epoch=float(core.get("reset", 0)),
                    used=core.get("used", 0),
                    resource="core",
                )

                logger.info(
                    "📊 Rate-limit budget: %d/%d remaining "
                    "(resets in %.0fs) | GraphQL: %d/%d",
                    self._snapshot.remaining,
                    self._snapshot.limit,
                    self._snapshot.seconds_until_reset,
                    graphql.get("remaining", 0),
                    graphql.get("limit", 0),
                )
                return self._snapshot
            else:
                logger.warning(
                    "Pre-flight rate-limit check returned %d",
                    response.status_code,
                )
        except Exception as exc:
            logger.warning(
                "Pre-flight rate-limit check failed: %s", exc
            )

        return self._snapshot

    # -- proactive pause -------------------------------------------------

    async def wait_if_exhausted(self) -> float:
        """If the budget is critically low, sleep until the reset.

        Returns:
            Number of seconds actually slept (0.0 if no pause).
        """
        async with self._lock:
            snap = self._snapshot

        if snap.budget_fraction > self.critical_threshold:
            return 0.0

        wait = snap.seconds_until_reset
        if wait <= 0:
            return 0.0

        # Add a small buffer so we don't race the reset boundary
        wait = max(wait + 2.0, wait * 1.05)
        logger.warning(
            "🛑 Rate-limit budget exhausted (%d/%d). "
            "Pausing %.0fs until reset...",
            snap.remaining,
            snap.limit,
            wait,
        )
        await asyncio.sleep(wait)
        return wait


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
            raise ValueError(
                f"rate must be positive, got {rate}"
            )
        if burst <= 0:
            raise ValueError(
                f"burst must be positive, got {burst}"
            )
        if min_rate <= 0:
            raise ValueError(
                f"min_rate must be positive, got {min_rate}"
            )
        if recovery_seconds <= 0:
            raise ValueError(
                f"recovery_seconds must be positive, "
                f"got {recovery_seconds}"
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
        self._tokens = min(
            self._burst, self._tokens + elapsed * self._rate
        )
        self._last_refill = now

        # Time-based recovery: gradually restore rate toward baseline
        if (
            self._rate < self._base_rate
            and self._last_rate_limit_time > 0
        ):
            since_limit = now - self._last_rate_limit_time
            if since_limit >= self._recovery_seconds:
                # Full recovery
                old = self._rate
                self._rate = self._base_rate
                self._rate_limit_count = 0
                if old != self._rate:
                    logger.info(
                        "⚙️  Token bucket fully recovered: "
                        "rate %.3f → %.3f tokens/s",
                        old,
                        self._rate,
                    )
            elif since_limit > self._recovery_seconds * 0.5:
                # Partial recovery (linear ramp toward baseline)
                progress = since_limit / self._recovery_seconds
                target = (
                    self._min_rate
                    + (self._base_rate - self._min_rate) * progress
                )
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
                f"tokens must be in the range (0, {self._burst}], "
                f"got {tokens!r}"
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
                    wait = (
                        deficit / self._rate if self._rate > 0
                        else 1.0
                    )

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
            self._rate = max(
                self._min_rate, self._rate * 0.5
            )
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
                        "🛑 Global retry-after set: %.0fs "
                        "(all tasks will pause)",
                        retry_after,
                    )

    async def set_global_retry_after(
        self, seconds: float
    ) -> None:
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
                    "🛑 Global retry-after: all tasks pausing "
                    "%.0fs",
                    seconds,
                )

    async def adjust_rate_from_budget(
        self, budget: RateLimitBudget
    ) -> None:
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
        suggested_rate = (
            1.0 / safe if safe > 0 else self._base_rate
        )

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


# AsyncProgressCounter - batch operation progress reporting


class AsyncProgressCounter:
    """Thread-safe counter for tracking batch operation progress.

    Emits a log message every *report_every* completions so the
    operator can see that work is proceeding.
    """

    def __init__(
        self, total: int, label: str, report_every: int = 10
    ) -> None:
        """Initialise the counter.

        Args:
            total: Expected total number of operations.
            label: Human-readable label (e.g. ``"Create"``).
            report_every: Log a progress line every N completions.
        """
        self._total = total
        self._label = label
        self._report_every = report_every
        self._count = 0
        self._success = 0
        self._failed = 0
        self._lock = asyncio.Lock()

    async def record(
        self, *, success: bool, name: str
    ) -> None:
        """Record one completed operation.

        Args:
            success: Whether the operation succeeded.
            name: Repository name (included in debug log).
        """
        async with self._lock:
            self._count += 1
            if success:
                self._success += 1
            else:
                self._failed += 1
            count = self._count
            success_count = self._success
            failed_count = self._failed

        logger.debug(
            "%s [%d/%d] %s: %s",
            self._label,
            count,
            self._total,
            "ok" if success else "FAILED",
            name,
        )

        if count % self._report_every == 0 or count == self._total:
            logger.info(
                "📊 %s progress: %d/%d completed "
                "(%d succeeded, %d failed)",
                self._label,
                count,
                self._total,
                success_count,
                failed_count,
            )


# Helper: parse Retry-After from a response


def parse_retry_after(response: Any) -> float | None:
    """Extract a ``Retry-After`` value from a GitHub response.

    Returns the number of seconds to wait, or ``None`` if the
    header is absent or unparsable.

    Args:
        response: An HTTP response.

    Returns:
        Seconds to wait, or ``None``.
    """
    raw = response.headers.get("Retry-After")
    if raw is None:
        return None
    try:
        return float(raw)
    except (ValueError, TypeError):
        return None


def is_rate_limited(response: Any) -> bool:
    """Determine whether a 403 response indicates rate limiting.

    GitHub returns HTTP 403 for both primary rate-limit exhaustion
    and secondary (abuse) rate limits.  This function checks for
    all known indicators of either type.

    Args:
        response: An HTTP response with status 403.

    Returns:
        ``True`` if this response appears to be rate-limited
        (either primary exhaustion or a secondary/abuse limit).
    """
    if response.status_code != 403:
        return False

    # Retry-After header is a strong signal
    if response.headers.get("Retry-After") is not None:
        return True

    # Primary rate limit exhaustion (X-RateLimit-Remaining: 0)
    if response.headers.get("X-RateLimit-Remaining") == "0":
        return True

    # Text-based detection (least reliable, but necessary)
    text = response.text.lower()
    return "rate limit" in text


def extract_rate_limit_info(
    response: Any,
) -> dict[str, Any]:
    """Extract all rate-limit-related information from a response.

    Useful for logging / debugging.

    Args:
        response: Any GitHub API response.

    Returns:
        Dictionary with rate-limit metadata.
    """
    info: dict[str, Any] = {}
    for header in (
        "X-RateLimit-Limit",
        "X-RateLimit-Remaining",
        "X-RateLimit-Reset",
        "X-RateLimit-Used",
        "X-RateLimit-Resource",
        "Retry-After",
    ):
        val = response.headers.get(header)
        if val is not None:
            info[header] = val
    return info
