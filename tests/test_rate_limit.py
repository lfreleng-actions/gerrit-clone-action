# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2025 Matthew Watkins <mwatkins@linuxfoundation.org>

"""Unit tests for the rate_limit module."""

from __future__ import annotations

import asyncio
import time
from unittest.mock import AsyncMock, Mock, patch

import httpx
import pytest

from gerrit_clone.rate_limit import (
    AsyncProgressCounter,
    RateLimitBudget,
    RateLimitSnapshot,
    TokenBucketLimiter,
    extract_rate_limit_info,
    is_rate_limited,
    parse_retry_after,
)

# -------------------------------------------------------------------
# RateLimitSnapshot
# -------------------------------------------------------------------


class TestRateLimitSnapshot:
    """Tests for RateLimitSnapshot dataclass."""

    def test_default_values(self) -> None:
        """Snapshot should have sensible defaults."""
        snap = RateLimitSnapshot()
        assert snap.limit == 5000
        assert snap.remaining == 5000
        assert snap.used == 0
        assert snap.resource == "core"

    def test_budget_fraction_full(self) -> None:
        """Full budget should return 1.0."""
        snap = RateLimitSnapshot(limit=5000, remaining=5000)
        assert snap.budget_fraction == 1.0

    def test_budget_fraction_half(self) -> None:
        """Half budget should return 0.5."""
        snap = RateLimitSnapshot(limit=5000, remaining=2500)
        assert snap.budget_fraction == 0.5

    def test_budget_fraction_empty(self) -> None:
        """Empty budget should return 0.0."""
        snap = RateLimitSnapshot(limit=5000, remaining=0)
        assert snap.budget_fraction == 0.0

    def test_budget_fraction_zero_limit(self) -> None:
        """Zero limit should return 0.0 without division error."""
        snap = RateLimitSnapshot(limit=0, remaining=0)
        assert snap.budget_fraction == 0.0

    def test_seconds_until_reset_future(self) -> None:
        """Should return positive seconds for future reset."""
        future = time.time() + 60.0
        snap = RateLimitSnapshot(reset_epoch=future)
        assert snap.seconds_until_reset > 0
        assert snap.seconds_until_reset <= 61.0

    def test_seconds_until_reset_past(self) -> None:
        """Should return 0.0 for past reset."""
        past = time.time() - 60.0
        snap = RateLimitSnapshot(reset_epoch=past)
        assert snap.seconds_until_reset == 0.0

    def test_safe_interval_with_budget(self) -> None:
        """Should calculate a positive safe interval."""
        future = time.time() + 3600.0  # 1 hour
        snap = RateLimitSnapshot(limit=5000, remaining=1000, reset_epoch=future)
        interval = snap.safe_interval(safety_margin=0.1)
        # With 1000 remaining, 500 reserved (10% of 5000),
        # usable = 500, time ~3600s => ~7.2s interval
        assert interval > 0
        assert interval < 60  # Should be reasonable

    def test_safe_interval_budget_exhausted(self) -> None:
        """Should return cautious interval when budget is gone."""
        future = time.time() + 60.0
        snap = RateLimitSnapshot(limit=5000, remaining=0, reset_epoch=future)
        interval = snap.safe_interval()
        # Budget exhausted, should wait ~60s
        assert interval >= 1.0

    def test_safe_interval_reset_imminent(self) -> None:
        """Should return at least 1.0 when reset is imminent."""
        past = time.time() - 1.0
        snap = RateLimitSnapshot(limit=5000, remaining=100, reset_epoch=past)
        interval = snap.safe_interval()
        assert interval >= 1.0


# -------------------------------------------------------------------
# RateLimitBudget
# -------------------------------------------------------------------


class TestRateLimitBudget:
    """Tests for RateLimitBudget tracker."""

    @pytest.mark.asyncio
    async def test_update_from_headers(self) -> None:
        """Should update snapshot from response headers."""
        budget = RateLimitBudget()
        headers = httpx.Headers(
            {
                "X-RateLimit-Limit": "5000",
                "X-RateLimit-Remaining": "4500",
                "X-RateLimit-Reset": str(int(time.time()) + 3600),
                "X-RateLimit-Used": "500",
                "X-RateLimit-Resource": "core",
            }
        )
        await budget.update_from_headers(headers)
        snap = budget.snapshot
        assert snap.limit == 5000
        assert snap.remaining == 4500
        assert snap.used == 500
        assert snap.resource == "core"

    @pytest.mark.asyncio
    async def test_update_from_headers_missing(self) -> None:
        """Should be a no-op when headers are absent."""
        budget = RateLimitBudget()
        original = budget.snapshot.remaining
        await budget.update_from_headers(httpx.Headers({}))
        assert budget.snapshot.remaining == original

    @pytest.mark.asyncio
    async def test_update_from_headers_invalid(self) -> None:
        """Should handle non-numeric header values gracefully."""
        budget = RateLimitBudget()
        headers = httpx.Headers(
            {
                "X-RateLimit-Remaining": "not-a-number",
                "X-RateLimit-Limit": "5000",
            }
        )
        # Should not raise
        await budget.update_from_headers(headers)

    def test_update_from_headers_sync(self) -> None:
        """Should update snapshot synchronously."""
        budget = RateLimitBudget()
        headers = httpx.Headers(
            {
                "X-RateLimit-Limit": "5000",
                "X-RateLimit-Remaining": "3000",
                "X-RateLimit-Reset": str(int(time.time()) + 1800),
                "X-RateLimit-Used": "2000",
            }
        )
        budget.update_from_headers_sync(headers)
        assert budget.snapshot.remaining == 3000
        assert budget.snapshot.used == 2000

    def test_update_from_headers_sync_missing(self) -> None:
        """Sync update should be a no-op when headers are absent."""
        budget = RateLimitBudget()
        original = budget.snapshot.remaining
        budget.update_from_headers_sync(httpx.Headers({}))
        assert budget.snapshot.remaining == original

    @pytest.mark.asyncio
    async def test_update_logs_warning_on_low_budget(self) -> None:
        """Should log when budget drops below low_threshold."""
        budget = RateLimitBudget(low_threshold=0.50)
        headers = httpx.Headers(
            {
                "X-RateLimit-Limit": "5000",
                "X-RateLimit-Remaining": "2000",
                "X-RateLimit-Reset": str(int(time.time()) + 3600),
                "X-RateLimit-Used": "3000",
            }
        )
        # Should not raise, just log
        await budget.update_from_headers(headers)
        assert budget.snapshot.remaining == 2000

    @pytest.mark.asyncio
    async def test_update_logs_warning_on_critical_budget(self) -> None:
        """Should log when budget drops below critical_threshold."""
        budget = RateLimitBudget(critical_threshold=0.10)
        headers = httpx.Headers(
            {
                "X-RateLimit-Limit": "5000",
                "X-RateLimit-Remaining": "100",
                "X-RateLimit-Reset": str(int(time.time()) + 3600),
                "X-RateLimit-Used": "4900",
            }
        )
        await budget.update_from_headers(headers)
        assert budget.snapshot.remaining == 100

    @pytest.mark.asyncio
    async def test_wait_if_exhausted_no_wait(self) -> None:
        """Should not wait when budget is healthy."""
        budget = RateLimitBudget(critical_threshold=0.03)
        # Default snapshot has full budget
        waited = await budget.wait_if_exhausted()
        assert waited == 0.0

    @pytest.mark.asyncio
    async def test_wait_if_exhausted_past_reset(self) -> None:
        """Should not wait when reset is in the past."""
        budget = RateLimitBudget(critical_threshold=0.50)
        headers = httpx.Headers(
            {
                "X-RateLimit-Limit": "5000",
                "X-RateLimit-Remaining": "10",
                "X-RateLimit-Reset": str(int(time.time()) - 10),
                "X-RateLimit-Used": "4990",
            }
        )
        await budget.update_from_headers(headers)
        waited = await budget.wait_if_exhausted()
        assert waited == 0.0

    @pytest.mark.asyncio
    async def test_preflight_check_success(self) -> None:
        """Should parse rate_limit API response."""
        budget = RateLimitBudget()

        mock_response = Mock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "resources": {
                "core": {
                    "limit": 5000,
                    "remaining": 4800,
                    "reset": int(time.time()) + 3600,
                    "used": 200,
                },
                "graphql": {
                    "limit": 5000,
                    "remaining": 4900,
                },
            }
        }

        client = AsyncMock()
        client.get = AsyncMock(return_value=mock_response)

        snap = await budget.preflight_check(client)
        assert snap.limit == 5000
        assert snap.remaining == 4800
        assert snap.used == 200

    @pytest.mark.asyncio
    async def test_preflight_check_failure(self) -> None:
        """Should handle preflight check failure gracefully."""
        budget = RateLimitBudget()

        client = AsyncMock()
        client.get = AsyncMock(side_effect=Exception("network error"))

        # Should not raise, returns existing snapshot
        snap = await budget.preflight_check(client)
        assert snap.limit == 5000  # Default

    @pytest.mark.asyncio
    async def test_preflight_check_non_200(self) -> None:
        """Should handle non-200 preflight response."""
        budget = RateLimitBudget()

        mock_response = Mock()
        mock_response.status_code = 500

        client = AsyncMock()
        client.get = AsyncMock(return_value=mock_response)

        snap = await budget.preflight_check(client)
        assert snap.limit == 5000  # Default unchanged

    def test_preflight_check_sync_success(self) -> None:
        """Should parse rate_limit API response synchronously."""
        budget = RateLimitBudget()

        mock_response = Mock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "resources": {
                "core": {
                    "limit": 5000,
                    "remaining": 4000,
                    "reset": int(time.time()) + 3600,
                    "used": 1000,
                },
                "graphql": {
                    "limit": 5000,
                    "remaining": 4500,
                },
            }
        }

        client = Mock()
        client.get = Mock(return_value=mock_response)

        snap = budget.preflight_check_sync(client)
        assert snap.remaining == 4000

    def test_preflight_check_sync_failure(self) -> None:
        """Should handle sync preflight failure gracefully."""
        budget = RateLimitBudget()

        client = Mock()
        client.get = Mock(side_effect=Exception("timeout"))

        snap = budget.preflight_check_sync(client)
        assert snap.limit == 5000  # Default unchanged


# -------------------------------------------------------------------
# TokenBucketLimiter
# -------------------------------------------------------------------


class TestTokenBucketLimiter:
    """Tests for TokenBucketLimiter."""

    @pytest.mark.asyncio
    async def test_acquire_immediate_when_bucket_full(self) -> None:
        """Should return immediately when tokens are available."""
        limiter = TokenBucketLimiter(rate=10.0, burst=10)
        waited = await limiter.acquire(tokens=1.0)
        assert waited == 0.0

    @pytest.mark.asyncio
    async def test_acquire_consumes_tokens(self) -> None:
        """Each acquire should reduce available tokens."""
        limiter = TokenBucketLimiter(rate=0.1, burst=5)
        # Drain the bucket
        for _ in range(5):
            await limiter.acquire(tokens=1.0)
        # Next acquire should have to wait (tokens exhausted)
        assert limiter.tokens < 1.0

    @pytest.mark.asyncio
    async def test_acquire_mutation_costs_more(self) -> None:
        """Mutations (tokens=2) should drain bucket faster."""
        limiter = TokenBucketLimiter(rate=0.1, burst=5)
        # 2 mutations = 4 tokens consumed
        await limiter.acquire(tokens=2.0)
        await limiter.acquire(tokens=2.0)
        # Only ~1 token left (minus refill time)
        assert limiter.tokens < 2.0

    @pytest.mark.asyncio
    async def test_rate_property(self) -> None:
        """Rate property should reflect current refill rate."""
        limiter = TokenBucketLimiter(rate=1.5, burst=10)
        assert limiter.rate == 1.5

    @pytest.mark.asyncio
    async def test_record_rate_limit_drains_bucket(self) -> None:
        """Recording a rate limit should drain the bucket."""
        limiter = TokenBucketLimiter(rate=1.0, burst=10)
        assert limiter.tokens == 10.0
        await limiter.record_rate_limit()
        assert limiter.tokens == 0.0

    @pytest.mark.asyncio
    async def test_record_rate_limit_reduces_rate(self) -> None:
        """Recording a rate limit should slash the refill rate."""
        limiter = TokenBucketLimiter(rate=1.0, burst=10, min_rate=0.01)
        assert limiter.rate == 1.0
        await limiter.record_rate_limit()
        assert limiter.rate == 0.5  # Halved

    @pytest.mark.asyncio
    async def test_record_rate_limit_cascading_reductions(self) -> None:
        """Multiple rate limits should keep halving the rate."""
        limiter = TokenBucketLimiter(rate=1.0, burst=10, min_rate=0.1)
        await limiter.record_rate_limit()
        assert limiter.rate == 0.5
        await limiter.record_rate_limit()
        assert limiter.rate == 0.25
        await limiter.record_rate_limit()
        assert limiter.rate == 0.125
        # Should not go below min_rate
        await limiter.record_rate_limit()
        assert limiter.rate >= 0.1

    @pytest.mark.asyncio
    async def test_record_rate_limit_respects_min_rate(self) -> None:
        """Rate should never drop below min_rate."""
        limiter = TokenBucketLimiter(rate=1.0, burst=5, min_rate=0.5)
        await limiter.record_rate_limit()
        assert limiter.rate == 0.5
        await limiter.record_rate_limit()
        assert limiter.rate == 0.5  # Clamped

    @pytest.mark.asyncio
    async def test_record_rate_limit_with_retry_after(self) -> None:
        """Should set global retry-after when provided."""
        limiter = TokenBucketLimiter(rate=1.0, burst=5)
        await limiter.record_rate_limit(retry_after=30.0)
        # The global retry-after should be set
        assert limiter._global_retry_until > 0

    @pytest.mark.asyncio
    async def test_set_global_retry_after(self) -> None:
        """Should set a global pause deadline."""
        limiter = TokenBucketLimiter(rate=1.0, burst=5)
        await limiter.set_global_retry_after(10.0)
        assert limiter._global_retry_until > 0
        assert limiter.tokens == 0.0

    @pytest.mark.asyncio
    async def test_record_success_is_noop(self) -> None:
        """record_success should not raise or change state."""
        limiter = TokenBucketLimiter(rate=1.0, burst=5)
        original_rate = limiter.rate
        await limiter.record_success()
        assert limiter.rate == original_rate

    @pytest.mark.asyncio
    async def test_time_based_recovery(self) -> None:
        """Rate should recover after recovery_seconds elapse."""
        limiter = TokenBucketLimiter(
            rate=1.0, burst=5, min_rate=0.1, recovery_seconds=0.2
        )
        await limiter.record_rate_limit()
        assert limiter.rate < 1.0

        # Simulate time passing beyond recovery_seconds
        async with limiter._lock:
            limiter._last_rate_limit_time = (
                time.monotonic() - 0.3  # Past recovery
            )

        # Trigger _refill by acquiring
        # Set tokens high so acquire doesn't block
        async with limiter._lock:
            limiter._tokens = 5.0
        await limiter.acquire(tokens=1.0)

        # Rate should have recovered
        assert limiter.rate == 1.0

    @pytest.mark.asyncio
    async def test_partial_recovery(self) -> None:
        """Rate should partially recover at 50%+ of recovery period."""
        recovery_secs = 1.0
        limiter = TokenBucketLimiter(
            rate=1.0, burst=5, min_rate=0.1, recovery_seconds=recovery_secs
        )
        await limiter.record_rate_limit()
        reduced_rate = limiter.rate

        # Simulate 60% of recovery elapsed
        async with limiter._lock:
            limiter._last_rate_limit_time = time.monotonic() - recovery_secs * 0.6
            limiter._tokens = 5.0

        await limiter.acquire(tokens=1.0)

        # Rate should be between reduced and full
        assert limiter.rate > reduced_rate
        assert limiter.rate <= 1.0

    @pytest.mark.asyncio
    async def test_adjust_rate_from_budget(self) -> None:
        """Should slow down when budget suggests lower rate."""
        limiter = TokenBucketLimiter(rate=2.0, burst=10)
        budget = RateLimitBudget()

        # Simulate low remaining budget
        headers = httpx.Headers(
            {
                "X-RateLimit-Limit": "5000",
                "X-RateLimit-Remaining": "100",
                "X-RateLimit-Reset": str(int(time.time()) + 3600),
                "X-RateLimit-Used": "4900",
            }
        )
        await budget.update_from_headers(headers)

        await limiter.adjust_rate_from_budget(budget)
        # Rate should have decreased (100 remaining over 3600s is slow)
        assert limiter.rate < 2.0

    @pytest.mark.asyncio
    async def test_adjust_rate_never_exceeds_base(self) -> None:
        """Should never increase rate above the base rate."""
        limiter = TokenBucketLimiter(rate=0.5, burst=5)
        budget = RateLimitBudget()

        # Simulate very high remaining budget
        headers = httpx.Headers(
            {
                "X-RateLimit-Limit": "5000",
                "X-RateLimit-Remaining": "5000",
                "X-RateLimit-Reset": str(int(time.time()) + 60),
                "X-RateLimit-Used": "0",
            }
        )
        await budget.update_from_headers(headers)

        await limiter.adjust_rate_from_budget(budget)
        # Rate should not exceed base
        assert limiter.rate <= 0.5

    @pytest.mark.asyncio
    async def test_global_retry_after_blocks_acquire(self) -> None:
        """Acquire should wait when global retry-after is active."""
        # Stateful monotonic: starts at 1000.0, advances by the
        # sleep duration so the limiter sees the retry-after
        # deadline as passed after the first sleep.
        current_time = 1000.0
        sleep_durations: list[float] = []

        def fake_monotonic() -> float:
            return current_time

        async def fake_sleep(duration: float) -> None:
            nonlocal current_time
            sleep_durations.append(duration)
            current_time += duration  # advance virtual clock

        with (
            patch(
                "gerrit_clone.rate_limit.time_mod.monotonic", side_effect=fake_monotonic
            ),
            patch("asyncio.sleep", side_effect=fake_sleep),
        ):
            # Create limiter INSIDE the patch so _last_refill uses
            # the fake clock (1000.0) instead of the real monotonic.
            limiter = TokenBucketLimiter(rate=100.0, burst=100)
            await limiter.set_global_retry_after(10.0)
            await limiter.acquire(tokens=1.0)

        # acquire should have slept for the full retry-after duration
        assert len(sleep_durations) >= 1
        assert sleep_durations[0] == pytest.approx(10.0, abs=0.5)

    @pytest.mark.asyncio
    async def test_acquire_rejects_zero_tokens(self) -> None:
        """acquire(tokens=0) should raise ValueError."""
        limiter = TokenBucketLimiter(rate=10.0, burst=10)
        with pytest.raises(ValueError, match="tokens must be in the range"):
            await limiter.acquire(tokens=0)

    @pytest.mark.asyncio
    async def test_acquire_rejects_negative_tokens(self) -> None:
        """acquire(tokens=-1) should raise ValueError."""
        limiter = TokenBucketLimiter(rate=10.0, burst=10)
        with pytest.raises(ValueError, match="tokens must be in the range"):
            await limiter.acquire(tokens=-1.0)

    @pytest.mark.asyncio
    async def test_acquire_rejects_tokens_exceeding_burst(self) -> None:
        """acquire(tokens > burst) should raise ValueError."""
        limiter = TokenBucketLimiter(rate=10.0, burst=5)
        with pytest.raises(ValueError, match="tokens must be in the range"):
            await limiter.acquire(tokens=6.0)


# -------------------------------------------------------------------
# AsyncProgressCounter
# -------------------------------------------------------------------


class TestAsyncProgressCounter:
    """Tests for AsyncProgressCounter."""

    @pytest.mark.asyncio
    async def test_counts_successes(self) -> None:
        """Should track success count."""
        counter = AsyncProgressCounter(total=5, label="Test", report_every=10)
        await counter.record(success=True, name="repo1")
        await counter.record(success=True, name="repo2")
        assert counter._success == 2
        assert counter._count == 2

    @pytest.mark.asyncio
    async def test_counts_failures(self) -> None:
        """Should track failure count."""
        counter = AsyncProgressCounter(total=5, label="Test", report_every=10)
        await counter.record(success=False, name="repo1")
        assert counter._failed == 1
        assert counter._count == 1

    @pytest.mark.asyncio
    async def test_mixed_results(self) -> None:
        """Should track mixed success/failure counts."""
        counter = AsyncProgressCounter(total=4, label="Test", report_every=10)
        await counter.record(success=True, name="repo1")
        await counter.record(success=False, name="repo2")
        await counter.record(success=True, name="repo3")
        await counter.record(success=False, name="repo4")
        assert counter._success == 2
        assert counter._failed == 2
        assert counter._count == 4


# -------------------------------------------------------------------
# Helper functions
# -------------------------------------------------------------------


class TestParseRetryAfter:
    """Tests for parse_retry_after helper."""

    def test_present_numeric(self) -> None:
        """Should parse numeric Retry-After header."""
        response = Mock()
        response.headers = {"Retry-After": "60"}
        assert parse_retry_after(response) == 60.0

    def test_present_float(self) -> None:
        """Should parse float Retry-After header."""
        response = Mock()
        response.headers = {"Retry-After": "30.5"}
        assert parse_retry_after(response) == 30.5

    def test_absent(self) -> None:
        """Should return None when header is absent."""
        response = Mock()
        response.headers = {}
        assert parse_retry_after(response) is None

    def test_unparseable(self) -> None:
        """Should return None for non-numeric values."""
        response = Mock()
        response.headers = {"Retry-After": "Thu, 01 Jan 2026 00:00:00 GMT"}
        assert parse_retry_after(response) is None


class TestIsRateLimited:
    """Tests for is_rate_limited helper."""

    def test_not_403(self) -> None:
        """Non-403 should return False."""
        response = Mock()
        response.status_code = 200
        response.headers = {}
        assert is_rate_limited(response) is False

    def test_403_with_retry_after(self) -> None:
        """403 with Retry-After header is a rate limit."""
        response = Mock()
        response.status_code = 403
        response.headers = {"Retry-After": "60"}
        assert is_rate_limited(response) is True

    def test_403_with_remaining_zero(self) -> None:
        """403 with X-RateLimit-Remaining: 0 is a rate limit."""
        response = Mock()
        response.status_code = 403
        response.headers = {"X-RateLimit-Remaining": "0"}
        response.text = "API rate limit exceeded"
        assert is_rate_limited(response) is True

    def test_403_with_secondary_text(self) -> None:
        """403 with 'secondary rate limit' in body is a rate limit."""
        response = Mock()
        response.status_code = 403
        response.headers = {}
        response.text = (
            "You have exceeded a secondary rate limit and have been temporarily blocked"
        )
        assert is_rate_limited(response) is True

    def test_403_with_rate_limit_text(self) -> None:
        """403 with generic 'rate limit' text is a rate limit."""
        response = Mock()
        response.status_code = 403
        response.headers = {}
        response.text = "API rate limit exceeded"
        assert is_rate_limited(response) is True

    def test_403_with_abuse_text(self) -> None:
        """403 with 'abuse' + 'rate limit' is a rate limit."""
        response = Mock()
        response.status_code = 403
        response.headers = {}
        response.text = "You have triggered an abuse rate limit"
        assert is_rate_limited(response) is True

    def test_403_permission_denied(self) -> None:
        """403 without rate limit indicators is not a rate limit."""
        response = Mock()
        response.status_code = 403
        response.headers = {}
        response.text = "Permission denied: insufficient access"
        assert is_rate_limited(response) is False

    def test_403_content_creation_text(self) -> None:
        """403 with 'rate limit' + 'content creation' is detected."""
        response = Mock()
        response.status_code = 403
        response.headers = {}
        response.text = "Rate limit exceeded for content creation requests"
        assert is_rate_limited(response) is True


class TestExtractRateLimitInfo:
    """Tests for extract_rate_limit_info helper."""

    def test_all_headers_present(self) -> None:
        """Should extract all rate-limit headers."""
        response = Mock()
        response.headers = {
            "X-RateLimit-Limit": "5000",
            "X-RateLimit-Remaining": "4500",
            "X-RateLimit-Reset": "1234567890",
            "X-RateLimit-Used": "500",
            "X-RateLimit-Resource": "core",
            "Retry-After": "60",
        }
        info = extract_rate_limit_info(response)
        assert len(info) == 6
        assert info["X-RateLimit-Remaining"] == "4500"
        assert info["Retry-After"] == "60"

    def test_no_headers(self) -> None:
        """Should return empty dict when no rate-limit headers."""
        response = Mock()
        response.headers = {"Content-Type": "application/json"}
        info = extract_rate_limit_info(response)
        assert info == {}

    def test_partial_headers(self) -> None:
        """Should return only present headers."""
        response = Mock()
        response.headers = {
            "X-RateLimit-Remaining": "100",
            "Content-Type": "application/json",
        }
        info = extract_rate_limit_info(response)
        assert len(info) == 1
        assert "X-RateLimit-Remaining" in info


# -------------------------------------------------------------------
# Integration-style tests
# -------------------------------------------------------------------


class TestTokenBucketLimiterIntegration:
    """Integration tests for token bucket behaviour."""

    @pytest.mark.asyncio
    async def test_multiple_concurrent_acquires(self) -> None:
        """Multiple tasks should share the bucket fairly."""
        limiter = TokenBucketLimiter(rate=100.0, burst=100)

        results = []

        async def worker(name: str) -> None:
            await limiter.acquire(tokens=1.0)
            results.append(name)

        tasks = [worker(f"w{i}") for i in range(10)]
        await asyncio.gather(*tasks)

        assert len(results) == 10

    @pytest.mark.asyncio
    async def test_rate_limit_slows_all_tasks(self) -> None:
        """A rate limit should slow all concurrent tasks."""
        limiter = TokenBucketLimiter(rate=10.0, burst=20, min_rate=0.5)

        # Record a rate limit
        await limiter.record_rate_limit()

        # Rate should be halved
        assert limiter.rate == 5.0
        # Bucket should be drained
        assert limiter.tokens == 0.0

    @pytest.mark.asyncio
    async def test_retry_after_pauses_all_tasks(self) -> None:
        """Global retry-after should pause all tasks."""
        # Stateful monotonic: starts at 1000.0, advances by the
        # sleep duration each time fake_sleep is called.
        current_time = 1000.0
        sleep_durations: list[float] = []

        def fake_monotonic() -> float:
            return current_time

        async def fake_sleep(duration: float) -> None:
            nonlocal current_time
            sleep_durations.append(duration)
            current_time += duration

        with (
            patch(
                "gerrit_clone.rate_limit.time_mod.monotonic", side_effect=fake_monotonic
            ),
            patch("asyncio.sleep", side_effect=fake_sleep),
        ):
            # Create limiter INSIDE the patch so _last_refill uses
            # the fake clock (1000.0) instead of the real monotonic.
            limiter = TokenBucketLimiter(rate=100.0, burst=100)
            await limiter.record_rate_limit(retry_after=10.0)

            await asyncio.gather(
                limiter.acquire(tokens=1.0),
                limiter.acquire(tokens=1.0),
                limiter.acquire(tokens=1.0),
            )

        # All three tasks should have encountered the global pause
        assert any(d >= 9.0 for d in sleep_durations)

    @pytest.mark.asyncio
    async def test_full_lifecycle(self) -> None:
        """Test rate limit → drain → slow → recover lifecycle."""
        limiter = TokenBucketLimiter(
            rate=10.0,
            burst=10,
            min_rate=1.0,
            recovery_seconds=0.3,
        )

        # Phase 1: Normal operation (full bucket)
        await limiter.acquire(tokens=1.0)
        assert limiter.rate == 10.0

        # Phase 2: Rate limit hit
        await limiter.record_rate_limit()
        assert limiter.rate == 5.0
        assert limiter.tokens == 0.0

        # Phase 3: Simulate recovery time passing
        async with limiter._lock:
            limiter._last_rate_limit_time = time.monotonic() - 0.4
            limiter._tokens = 10.0  # Refill manually

        # Phase 4: Acquire triggers _refill which checks recovery
        await limiter.acquire(tokens=1.0)
        assert limiter.rate == 10.0  # Fully recovered


class TestBudgetAndLimiterIntegration:
    """Test RateLimitBudget and TokenBucketLimiter working together."""

    @pytest.mark.asyncio
    async def test_budget_adjusts_limiter(self) -> None:
        """Budget data should adjust the limiter rate."""
        budget = RateLimitBudget()
        limiter = TokenBucketLimiter(rate=5.0, burst=10)

        # Simulate low budget
        headers = httpx.Headers(
            {
                "X-RateLimit-Limit": "5000",
                "X-RateLimit-Remaining": "50",
                "X-RateLimit-Reset": str(int(time.time()) + 3600),
                "X-RateLimit-Used": "4950",
            }
        )
        await budget.update_from_headers(headers)
        await limiter.adjust_rate_from_budget(budget)

        # With only 50 remaining over 3600s, rate should be very low
        assert limiter.rate < 5.0

    @pytest.mark.asyncio
    async def test_healthy_budget_no_change(self) -> None:
        """Healthy budget should not slow down the limiter."""
        budget = RateLimitBudget()
        limiter = TokenBucketLimiter(rate=0.5, burst=5)

        # Simulate healthy budget
        headers = httpx.Headers(
            {
                "X-RateLimit-Limit": "5000",
                "X-RateLimit-Remaining": "4500",
                "X-RateLimit-Reset": str(int(time.time()) + 3600),
                "X-RateLimit-Used": "500",
            }
        )
        await budget.update_from_headers(headers)
        await limiter.adjust_rate_from_budget(budget)

        # Rate should remain at base (budget suggests faster than base)
        assert limiter.rate == 0.5
