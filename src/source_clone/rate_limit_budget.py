# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""Primary (token-based) GitHub rate-limit budget tracking.

Tracks the primary rate-limit budget by reading ``X-RateLimit-*``
headers from every GitHub API response and by querying
``GET /rate_limit`` before batch operations.  It can calculate a safe
request interval and proactively pause when the remaining budget drops
below a configurable threshold.
"""

from __future__ import annotations

import asyncio
import time as time_mod
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from source_clone.logging import get_logger

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
                "🚨 Rate-limit budget critical: %d/%d remaining (resets in %.0fs)",
                remaining,
                limit,
                max(0, reset_epoch - time_mod.time()),
            )
        elif remaining <= int(limit * self.low_threshold):
            logger.info(
                "⚠️  Rate-limit budget low: %d/%d remaining (resets in %.0fs)",
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
                "🚨 Rate-limit budget critical: %d/%d remaining (resets in %.0fs)",
                remaining,
                limit,
                max(0, reset_epoch - time_mod.time()),
            )
        elif remaining <= int(limit * self.low_threshold):
            logger.info(
                "⚠️  Rate-limit budget low: %d/%d remaining (resets in %.0fs)",
                remaining,
                limit,
                max(0, reset_epoch - time_mod.time()),
            )

    # -- pre-flight check ------------------------------------------------

    async def preflight_check(
        self,
        client: httpx.AsyncClient,
        base_url: str = "https://api.github.com",
    ) -> RateLimitSnapshot:
        """Query ``GET /rate_limit`` and return a fresh snapshot.

        This endpoint is free (does not count against the budget).

        Args:
            client: An authenticated ``httpx.AsyncClient``.
            base_url: REST API base URL of the GitHub host the
                client authenticates against.

        Returns:
            Updated :class:`RateLimitSnapshot`.
        """
        try:
            response = await client.get(f"{base_url}/rate_limit")
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
        self,
        client: httpx.Client,
        base_url: str = "https://api.github.com",
    ) -> RateLimitSnapshot:
        """Synchronous variant of :meth:`preflight_check`.

        Args:
            client: An authenticated ``httpx.Client``.
            base_url: REST API base URL of the GitHub host the
                client authenticates against.

        Returns:
            Updated :class:`RateLimitSnapshot`.
        """
        try:
            response = client.get(f"{base_url}/rate_limit")
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
            logger.warning("Pre-flight rate-limit check failed: %s", exc)

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
            "🛑 Rate-limit budget exhausted (%d/%d). Pausing %.0fs until reset...",
            snap.remaining,
            snap.limit,
            wait,
        )
        await asyncio.sleep(wait)
        return wait


__all__ = [
    "RateLimitBudget",
    "RateLimitSnapshot",
]
