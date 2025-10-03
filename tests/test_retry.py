# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2025 Matthew Watkins <mwatkins@linuxfoundation.org>

"""Unit tests for retry module."""

from __future__ import annotations

import time
from unittest.mock import Mock, patch

import pytest

from gerrit_clone.models import RetryPolicy
from gerrit_clone.retry import (
    FatalError,
    RetryableError,
    RetryManager,
    calculate_delay,
    execute_with_retry,
    is_retryable_error,
    retry_sync,
)


class TestRetryableError:
    """Test RetryableError exception."""

    def test_retryable_error_creation(self):
        """Test basic RetryableError creation."""
        error = RetryableError("Test error")
        assert str(error) == "Test error"
        assert isinstance(error, Exception)

    def test_retryable_error_with_cause(self):
        """Test RetryableError with underlying cause."""
        cause = ValueError("Original error")
        try:
            raise RetryableError("Wrapper error") from cause
        except RetryableError as error:
            assert str(error) == "Wrapper error"
            assert error.__cause__ is cause


class TestCalculateDelay:
    """Test delay calculation function."""

    def test_calculate_delay_first_attempt(self):
        """Test delay calculation for first retry."""
        policy = RetryPolicy(base_delay=2.0, factor=2.0, jitter=False)
        delay = calculate_delay(1, policy)
        assert delay == 2.0

    def test_calculate_delay_exponential_backoff(self):
        """Test exponential backoff calculation."""
        policy = RetryPolicy(base_delay=1.0, factor=2.0, jitter=False)

        # First retry: base_delay * factor^0 = 1.0
        assert calculate_delay(1, policy) == 1.0

        # Second retry: base_delay * factor^1 = 2.0
        assert calculate_delay(2, policy) == 2.0

        # Third retry: base_delay * factor^2 = 4.0
        assert calculate_delay(3, policy) == 4.0

    def test_calculate_delay_max_delay_cap(self):
        """Test delay is capped at max_delay."""
        policy = RetryPolicy(base_delay=10.0, factor=2.0, max_delay=15.0, jitter=False)

        # Should be capped at max_delay
        delay = calculate_delay(5, policy)  # Would be 160.0 without cap
        assert delay == 15.0

    @patch("gerrit_clone.retry.random.uniform")
    def test_calculate_delay_with_jitter(self, mock_uniform):
        """Test jitter application."""
        mock_uniform.return_value = 0.5  # 50% jitter
        policy = RetryPolicy(base_delay=4.0, factor=1.0, jitter=True)

        delay = calculate_delay(1, policy)

        # Should call random.uniform(0, 4.0) - full jitter implementation
        mock_uniform.assert_called_once_with(0, 4.0)
        assert delay == 0.5

    def test_calculate_delay_no_jitter(self):
        """Test delay without jitter."""
        policy = RetryPolicy(base_delay=3.0, factor=1.5, jitter=False)
        delay = calculate_delay(2, policy)
        assert delay == 3.0 * 1.5  # 4.5

    def test_calculate_delay_zero_attempt(self):
        """Test delay for attempt 0 (should not happen in practice)."""
        policy = RetryPolicy(base_delay=2.0, factor=2.0, jitter=False)
        delay = calculate_delay(0, policy)
        assert delay == 0.0  # Returns 0.0 for attempt <= 0


class TestExecuteWithRetry:
    """Test synchronous retry decorator and function."""

    def test_execute_with_retry_success_first_attempt(self):
        """Test successful execution on first attempt."""
        mock_func = Mock(return_value="success")
        policy = RetryPolicy(max_attempts=3)

        result = execute_with_retry(mock_func, policy)

        assert result == "success"
        assert mock_func.call_count == 1

    def test_execute_with_retry_success_after_retries(self):
        """Test successful execution after some failures."""
        mock_func = Mock(
            side_effect=[RetryableError("Fail 1"), RetryableError("Fail 2"), "success"]
        )
        policy = RetryPolicy(max_attempts=3, base_delay=0.01)  # Fast for testing

        with patch("gerrit_clone.retry.time.sleep") as mock_sleep:
            result = execute_with_retry(mock_func, policy)

        assert result == "success"
        assert mock_func.call_count == 3
        assert mock_sleep.call_count == 2  # Two retries, two sleeps

    def test_execute_with_retry_max_attempts_exceeded(self):
        """Test failure when max attempts exceeded."""
        mock_func = Mock(side_effect=RetryableError("Always fail"))
        policy = RetryPolicy(max_attempts=2, base_delay=0.01)

        with (
            patch("gerrit_clone.retry.time.sleep"),
            pytest.raises(RetryableError, match="Always fail"),
        ):
            execute_with_retry(mock_func, policy)

        assert mock_func.call_count == 2

    def test_execute_with_retry_non_retryable_error(self):
        """Test failure for explicitly non-retryable errors."""
        mock_func = Mock(side_effect=Exception("permission denied"))
        policy = RetryPolicy(max_attempts=2, base_delay=0.01)

        with (
            patch("gerrit_clone.retry.time.sleep"),
            pytest.raises(Exception, match="permission denied"),
        ):
            execute_with_retry(mock_func, policy)

        # Should only try once for permission denied (non-retryable)
        assert mock_func.call_count == 1

    def test_execute_with_retry_with_args_kwargs(self) -> None:
        """Test retry with function arguments."""
        mock_func = Mock(return_value="success")
        policy = RetryPolicy()

        result = execute_with_retry(
            mock_func,
            policy,
            "operation",
            "arg1",
            "arg2",
            kwarg1="value1",
            kwarg2="value2",
        )

        assert result == "success"
        mock_func.assert_called_once_with(
            "arg1", "arg2", kwarg1="value1", kwarg2="value2"
        )

    @patch("gerrit_clone.retry.time.sleep")
    def test_execute_with_retry_delay_calculation(self, mock_sleep: Mock) -> None:
        """Test that delays are calculated correctly."""
        mock_func = Mock(
            side_effect=[RetryableError("Fail 1"), RetryableError("Fail 2"), "success"]
        )
        policy = RetryPolicy(max_attempts=3, base_delay=1.0, factor=2.0, jitter=False)

        execute_with_retry(mock_func, policy, "operation")

        # Should sleep for calculated delays
        assert mock_sleep.call_count == 2
        calls = mock_sleep.call_args_list
        assert calls[0][0][0] == 1.0  # First retry delay
        assert calls[1][0][0] == 2.0  # Second retry delay

    def test_execute_with_retry_decorator_usage(self) -> None:
        """Test using execute_with_retry function with decorator pattern."""
        policy = RetryPolicy(max_attempts=2, base_delay=0.01)
        call_count = 0

        def failing_function() -> str:
            nonlocal call_count
            call_count += 1

            if call_count < 2:
                raise RetryableError("Not yet")
            return "success"

        with patch("gerrit_clone.retry.time.sleep"):
            result = execute_with_retry(failing_function, policy, "operation")

        assert result == "success"
        assert call_count == 2

    def test_execute_with_retry_logging(self) -> None:
        """Test that retry attempts are logged."""
        mock_func = Mock(side_effect=[RetryableError("Network error"), "success"])
        policy = RetryPolicy(max_attempts=2, base_delay=0.01)

        with (
            patch("gerrit_clone.retry.time.sleep"),
            patch("gerrit_clone.retry.logger.warning") as mock_log,
        ):
            execute_with_retry(mock_func, policy, "operation")

        mock_log.assert_called()


class TestIsRetryableError:
    """Test is_retryable_error function."""

    def test_retryable_error_types(self):
        """Test that RetryableError instances are retryable."""
        error = RetryableError("Test error")
        assert is_retryable_error(error) is True

    def test_fatal_error_types(self):
        """Test that FatalError instances are not retryable."""
        error = FatalError("Fatal error")
        assert is_retryable_error(error) is False

    def test_http_error_patterns(self):
        """Test HTTP error pattern recognition."""
        # Test various HTTP errors that should be retryable
        retryable_errors = [
            Exception("Connection timeout"),
            Exception("502 Bad Gateway"),
            Exception("503 Service Unavailable"),
            Exception("504 Gateway timeout"),
            Exception("Too many requests"),
            Exception("Rate limit exceeded"),
        ]

        for error in retryable_errors:
            assert is_retryable_error(error) is True

    def test_git_error_patterns(self):
        """Test Git error pattern recognition."""
        git_errors = [
            Exception("fetch failed"),
            Exception("Clone failed"),
            Exception("Unable to access repository"),
            Exception("Transfer closed with outstanding"),
            Exception("Early EOF"),
            Exception("RPC failed"),
            Exception("Remote end hung up"),
        ]

        for error in git_errors:
            assert is_retryable_error(error) is True

    def test_non_retryable_errors(self):
        """Test that specific error patterns are not retryable."""
        non_retryable_errors = [
            Exception("permission denied"),
            Exception("authentication failed"),
            Exception("not found"),
            Exception("repository not found"),
            Exception("host key verification failed"),
        ]

        for error in non_retryable_errors:
            assert is_retryable_error(error) is False

    def test_default_retryable_behavior(self):
        """Test that unknown errors are retryable by default."""
        unknown_errors = [
            ValueError("Some random error"),
            TypeError("Random type error"),
            Exception("Unknown weird error"),
        ]

        for error in unknown_errors:
            assert is_retryable_error(error) is True


class TestRetrySyncDecorator:
    """Test retry_sync decorator."""

    def test_retry_sync_decorator_success(self) -> None:
        """Test successful execution with retry_sync decorator."""
        policy = RetryPolicy(max_attempts=3, base_delay=0.01)

        @retry_sync(policy, "test_operation")
        def successful_function() -> str:
            return "success"

        result = successful_function()
        assert result == "success"

    def test_retry_sync_decorator_with_retries(self) -> None:
        """Test retry_sync decorator with some failures."""
        policy = RetryPolicy(max_attempts=3, base_delay=0.01)
        call_count = 0

        @retry_sync(policy, "test_operation")
        def flaky_function() -> str:
            nonlocal call_count
            call_count += 1
            if call_count < 3:
                raise RetryableError(f"Fail {call_count}")
            return "success"

        with patch("gerrit_clone.retry.time.sleep"):
            result = flaky_function()

        assert result == "success"
        assert call_count == 3

    def test_retry_sync_decorator_max_attempts(self) -> None:
        """Test retry_sync decorator when max attempts exceeded."""
        policy = RetryPolicy(max_attempts=2, base_delay=0.01)

        @retry_sync(policy, "test_operation")
        def always_fails() -> str:
            raise RetryableError("Always fail")

        with (
            patch("gerrit_clone.retry.time.sleep"),
            pytest.raises(RetryableError, match="Always fail"),
        ):
            always_fails()


class TestRetryManager:
    """Test RetryManager context manager."""

    def test_retry_manager_success_first_attempt(self) -> None:
        """Test RetryManager with successful first attempt."""
        policy = RetryPolicy(max_attempts=3)

        with RetryManager(policy):
            # Simulate successful operation
            result = "success"

        assert result == "success"

    def test_retry_manager_with_retries(self) -> None:
        """Test RetryManager with retry logic."""
        policy = RetryPolicy(max_attempts=3, base_delay=0.01)
        call_count = 0
        result = None

        with (
            patch("gerrit_clone.retry.time.sleep"),
            RetryManager(policy, "test_operation") as retry,
        ):
            while retry.should_retry():
                call_count += 1
                try:
                    if call_count < 3:
                        raise RetryableError(f"Attempt {call_count} failed")
                    result = "success"
                    break
                except RetryableError as e:
                    if not retry.handle_error(e):
                        raise

        assert result == "success"
        assert call_count == 3

    def test_retry_manager_max_attempts_exceeded(self) -> None:
        """Test RetryManager when max attempts are exceeded."""
        policy = RetryPolicy(max_attempts=2, base_delay=0.01)

        with (
            patch("gerrit_clone.retry.time.sleep"),
            pytest.raises(RetryableError),
            RetryManager(policy) as retry,
        ):
            while retry.should_retry():
                try:
                    raise RetryableError("Always fail")
                except RetryableError as e:
                    if not retry.handle_error(e):
                        raise

    def test_retry_manager_non_retryable_error(self) -> None:
        """Test RetryManager with non-retryable error."""
        policy = RetryPolicy(max_attempts=3)

        with (
            pytest.raises(ValueError, match="Not retryable"),
            RetryManager(policy) as retry,
        ):
            while retry.should_retry():
                try:
                    raise ValueError("Not retryable")
                except ValueError as e:
                    if not retry.handle_error(e):
                        raise


class TestRetryIntegration:
    """Integration tests for retry functionality."""

    def test_retry_with_real_timing(self) -> None:
        """Test retry with actual time delays (short ones)."""
        start_time = time.time()
        call_count = 0

        def flaky_function() -> str:
            nonlocal call_count
            call_count += 1
            if call_count < 2:
                raise RetryableError("Temporary failure")
            return "success"

        policy = RetryPolicy(max_attempts=2, base_delay=0.05, jitter=False)
        result = execute_with_retry(flaky_function, policy, "test_operation")
        end_time = time.time()

        assert result == "success"
        assert call_count == 2
        # Should have taken at least the delay time
        assert end_time - start_time >= 0.05

    def test_retry_manager_timing(self) -> None:
        """Test RetryManager with actual timing."""
        start_time = time.time()
        policy = RetryPolicy(max_attempts=2, base_delay=0.05, jitter=False)
        call_count = 0
        result = None

        with RetryManager(policy) as retry:
            while retry.should_retry():
                call_count += 1
                try:
                    if call_count < 2:
                        raise RetryableError("Temporary failure")
                    result = "success"
                    break
                except RetryableError as e:
                    if not retry.handle_error(e):
                        raise

        end_time = time.time()
        assert result == "success"
        assert call_count == 2
        # Should have taken at least the delay time
        assert end_time - start_time >= 0.05

    def test_retry_preserves_return_types(self) -> None:
        """Test that retry preserves different return types."""
        policy = RetryPolicy(max_attempts=1)

        # Test different return types
        assert execute_with_retry(lambda: "string", policy, "test") == "string"
        assert execute_with_retry(lambda: 42, policy, "test") == 42
        assert execute_with_retry(lambda: [1, 2, 3], policy, "test") == [1, 2, 3]
        assert execute_with_retry(lambda: {"key": "value"}, policy, "test") == {
            "key": "value"
        }
        assert execute_with_retry(lambda: None, policy, "test") is None

    def test_retry_with_complex_exception_hierarchy(self) -> None:
        """Test retry behavior with complex exception inheritance."""

        class CustomRetryableError(RetryableError):
            pass

        class NonRetryableError(Exception):
            pass

        policy = RetryPolicy(max_attempts=2, base_delay=0.01)

        # Custom retryable error should be retried
        call_count = 0

        def func_with_custom_error() -> str:
            nonlocal call_count
            call_count += 1
            if call_count < 2:
                raise CustomRetryableError("Custom failure")
            return "success"

        with patch("gerrit_clone.retry.time.sleep"):
            result = execute_with_retry(func_with_custom_error, policy, "test")

        assert result == "success"
        assert call_count == 2

        # Non-retryable error should not be retried
        def func_with_non_retryable() -> str:
            raise NonRetryableError("Non-retryable")

        with pytest.raises(NonRetryableError):
            execute_with_retry(func_with_non_retryable, policy, "test")

    def test_fatal_error_inheritance(self) -> None:
        """Test FatalError behavior."""
        error = FatalError("Fatal issue")
        assert str(error) == "Fatal issue"
        assert isinstance(error, Exception)
        assert not is_retryable_error(error)
