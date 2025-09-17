# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2025 Matthew Watkins <mwatkins@linuxfoundation.org>

"""Retry mechanism with exponential backoff and jitter."""

from __future__ import annotations

import asyncio
import functools
import random
import time
from collections.abc import Awaitable, Callable
from typing import Any, TypeVar

from gerrit_clone.logging import get_logger
from gerrit_clone.models import RetryPolicy

T = TypeVar("T")

logger = get_logger(__name__)


class RetryableError(Exception):
    """Base class for errors that should trigger a retry."""


class FatalError(Exception):
    """Base class for errors that should not trigger a retry."""


def is_retryable_error(error: Exception) -> bool:
    """Determine if an error is retryable.

    Args:
        error: Exception to check

    Returns:
        True if the error should trigger a retry
    """
    # Explicit retry/fatal error types
    if isinstance(error, RetryableError):
        return True
    if isinstance(error, FatalError):
        return False

    # Common retryable conditions
    error_str = str(error).lower()

    # Network/connection issues
    if any(
        keyword in error_str
        for keyword in [
            "connection",
            "timeout",
            "timed out",
            "network",
            "temporary failure",
            "service unavailable",
            "bad gateway",
            "gateway timeout",
            "too many requests",
            "rate limit",
            "ssh_exchange_identification",
            "could not resolve hostname",
        ]
    ):
        return True

    # Git-specific retryable errors
    if any(
        keyword in error_str
        for keyword in [
            "fetch failed",
            "clone failed",
            "unable to access",
            "transfer closed",
            "early eof",
            "rpc failed",
            "remote end hung up",
            "could not lock config file",
        ]
    ):
        return True

    # Non-retryable conditions
    if any(
        keyword in error_str
        for keyword in [
            "authentication failed",
            "permission denied",
            "not found",
            "repository not found",
            "does not exist",
            "host key verification failed",
            "no such file or directory",
            "invalid",
            "malformed",
            "fatal:",
        ]
    ):
        return False

    # Default to retryable for unknown errors
    return True


def calculate_delay(
    attempt: int,
    policy: RetryPolicy,
    error: Exception | None = None,
) -> float:
    """Calculate delay for retry attempt with exponential backoff and jitter.

    Args:
        attempt: Current attempt number (1-based)
        policy: Retry policy configuration
        error: Optional error to customize delay based on error type

    Returns:
        Delay in seconds
    """
    if attempt <= 0:
        return 0.0

    # Check for file locking errors that should have shorter delays
    if error and "could not lock config file" in str(error).lower():
        # Use shorter delays for file locking issues - they resolve quickly
        delay = min(1.0, policy.base_delay) * (1.5 ** (attempt - 1))
        delay = min(delay, 5.0)  # Cap at 5 seconds for file locks
    else:
        # Normal exponential backoff: base_delay * (factor ^ (attempt - 1))
        delay = policy.base_delay * (policy.factor ** (attempt - 1))
        delay = min(delay, policy.max_delay)

    # Add jitter if enabled
    if policy.jitter:
        # Full jitter: random value between 0 and calculated delay
        delay = random.uniform(0, delay)

    return delay


def retry_sync(
    policy: RetryPolicy,
    operation_name: str = "operation",
) -> Callable[[Callable[..., T]], Callable[..., T]]:
    """Decorator for synchronous retry with exponential backoff.

    Args:
        policy: Retry policy configuration
        operation_name: Name of operation for logging

    Returns:
        Decorated function
    """

    def decorator(func: Callable[..., T]) -> Callable[..., T]:
        @functools.wraps(func)
        def wrapper(*args: Any, **kwargs: Any) -> T:
            last_error: Exception | None = None

            for attempt in range(1, policy.max_attempts + 1):
                try:
                    return func(*args, **kwargs)
                except Exception as error:
                    last_error = error

                    # Check if we should retry
                    if not is_retryable_error(error):
                        logger.debug(
                            f"Non-retryable error in {operation_name} "
                            f"(attempt {attempt}): {error}"
                        )
                        raise

                    # Don't delay/log on last attempt
                    if attempt >= policy.max_attempts:
                        break

                    # Calculate delay and log retry
                    delay = calculate_delay(attempt, policy, error)
                    logger.warning(
                        f"Retry {operation_name} (attempt {attempt + 1}/"
                        f"{policy.max_attempts}) after {delay:.2f}s: {error}"
                    )

                    # Wait before retry
                    if delay > 0:
                        time.sleep(delay)

            # All attempts failed
            if last_error:
                logger.error(
                    f"Failed {operation_name} after {policy.max_attempts} attempts: "
                    f"{last_error}"
                )
                raise last_error
            else:
                raise RuntimeError(f"Failed {operation_name} with unknown error")

        return wrapper

    return decorator


async def retry_async(
    policy: RetryPolicy,
    operation_name: str = "operation",
) -> Callable[[Callable[..., Awaitable[T]]], Callable[..., Awaitable[T]]]:
    """Decorator for asynchronous retry with exponential backoff.

    Args:
        policy: Retry policy configuration
        operation_name: Name of operation for logging

    Returns:
        Decorated async function
    """

    def decorator(func: Callable[..., Awaitable[T]]) -> Callable[..., Awaitable[T]]:
        @functools.wraps(func)
        async def wrapper(*args: Any, **kwargs: Any) -> T:
            last_error: Exception | None = None

            for attempt in range(1, policy.max_attempts + 1):
                try:
                    return await func(*args, **kwargs)
                except Exception as error:
                    last_error = error

                    # Check if we should retry
                    if not is_retryable_error(error):
                        logger.debug(
                            f"Non-retryable error in {operation_name} "
                            f"(attempt {attempt}): {error}"
                        )
                        raise

                    # Don't delay/log on last attempt
                    if attempt >= policy.max_attempts:
                        break

                    # Calculate delay and log retry
                    delay = calculate_delay(attempt, policy, error)
                    logger.warning(
                        f"Retry {operation_name} (attempt {attempt + 1}/"
                        f"{policy.max_attempts}) after {delay:.2f}s: {error}"
                    )

                    # Wait before retry
                    if delay > 0:
                        await asyncio.sleep(delay)

            # All attempts failed
            if last_error:
                logger.error(
                    f"Failed {operation_name} after {policy.max_attempts} attempts: "
                    f"{last_error}"
                )
                raise last_error
            else:
                raise RuntimeError(f"Failed {operation_name} with unknown error")

        return wrapper

    return decorator


class RetryManager:
    """Context manager for retry operations with custom logic."""

    def __init__(
        self,
        policy: RetryPolicy,
        operation_name: str = "operation",
    ) -> None:
        """Initialize retry manager.

        Args:
            policy: Retry policy configuration
            operation_name: Name of operation for logging
        """
        self.policy = policy
        self.operation_name = operation_name
        self.attempt = 0
        self.last_error: Exception | None = None

    def __enter__(self) -> RetryManager:
        """Enter retry context."""
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: Any,
    ) -> bool:
        """Exit retry context and handle exceptions."""
        if exc_type is None:
            # Success - no retry needed
            return False

        if not isinstance(exc_val, Exception):
            # Non-Exception (like KeyboardInterrupt) - don't retry
            return False

        self.last_error = exc_val
        self.attempt += 1

        # Check if we should retry
        if not is_retryable_error(exc_val):
            logger.debug(
                f"Non-retryable error in {self.operation_name} "
                f"(attempt {self.attempt}): {exc_val}"
            )
            return False

        # Check if we've exceeded max attempts
        if self.attempt >= self.policy.max_attempts:
            logger.error(
                f"Failed {self.operation_name} after {self.policy.max_attempts} "
                f"attempts: {exc_val}"
            )
            return False

        # Calculate delay and log retry
        delay = calculate_delay(self.attempt, self.policy, exc_val)
        logger.warning(
            f"Retry {self.operation_name} (attempt {self.attempt + 1}/"
            f"{self.policy.max_attempts}) after {delay:.2f}s: {exc_val}"
        )

        # Wait before retry
        if delay > 0:
            time.sleep(delay)

        # Suppress the exception to trigger retry
        return True

    def should_retry(self) -> bool:
        """Check if we should continue retrying.

        Returns:
            True if more attempts are available
        """
        return self.attempt < self.policy.max_attempts

    def handle_error(self, error: Exception) -> bool:
        """Handle an error and determine if we should retry.

        Args:
            error: The exception that occurred

        Returns:
            True if we should continue retrying, False to give up
        """
        self.last_error = error
        self.attempt += 1

        # Check if we should retry
        if not is_retryable_error(error):
            logger.debug(
                f"Non-retryable error in {self.operation_name} "
                f"(attempt {self.attempt}): {error}"
            )
            return False

        # Check if we've exceeded max attempts
        if self.attempt >= self.policy.max_attempts:
            logger.error(
                f"Failed {self.operation_name} after {self.policy.max_attempts} "
                f"attempts: {error}"
            )
            return False

        # Calculate delay and log retry
        delay = calculate_delay(self.attempt, self.policy, error)
        logger.warning(
            f"Retry {self.operation_name} (attempt {self.attempt + 1}/"
            f"{self.policy.max_attempts}) after {delay:.2f}s: {error}"
        )

        # Wait before retry
        if delay > 0:
            time.sleep(delay)

        return True


def execute_with_retry(
    func: Callable[..., T],
    policy: RetryPolicy,
    operation_name: str = "operation",
    *args: Any,
    **kwargs: Any,
) -> T:
    """Execute function with retry logic.

    Args:
        func: Function to execute
        policy: Retry policy configuration
        operation_name: Name of operation for logging
        *args: Positional arguments for function
        **kwargs: Keyword arguments for function

    Returns:
        Function result

    Raises:
        Last exception if all retries fail
    """
    retry_manager = RetryManager(policy, operation_name)

    while True:
        with retry_manager:
            return func(*args, **kwargs)

        if not retry_manager.should_retry():
            # This shouldn't happen due to __exit__ logic, but safe fallback
            if retry_manager.last_error:
                raise retry_manager.last_error
            else:
                raise RuntimeError(f"Failed {operation_name} with unknown error")
