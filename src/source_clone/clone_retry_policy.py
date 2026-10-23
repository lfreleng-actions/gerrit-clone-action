# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""Retry policy for failed git clone operations.

Splits into two questions, both answered by reading the failure text: whether
retrying could plausibly help, and how long to wait first.

Both are expressed as ordered rule tables. Order is significant: the first
entry that matches wins, so narrow patterns must precede the broad catch-alls
that would otherwise shadow them.
"""

from __future__ import annotations

import random
from typing import TYPE_CHECKING

from source_clone.logging import get_logger

if TYPE_CHECKING:
    import subprocess
    from collections.abc import Callable

logger = get_logger(__name__)


def _missing_path_rule(error: str) -> bool | None:
    """File not found errors are generally not retryable."""
    if "no such file or directory" not in error:
        return None
    # Exception: temporary files during operations might be retryable
    return any(pattern in error for pattern in ["tmp_", "temp", ".tmp"])


def _config_lock_rule(error: str) -> bool | None:
    """Config file locking errors are retryable only on real lock contention."""
    if "could not lock config file" not in error:
        return None
    # If the config file doesn't exist, it's not a lock issue
    return "no such file or directory" not in error


def _git_dir_lock_rule(error: str) -> bool | None:
    """.git directory access issues are retryable if not missing files."""
    if not ("could not lock" in error and ".git" in error):
        return None
    return "no such file or directory" not in error


def _transient_io_rule(error: str) -> bool | None:
    """Filesystem I/O errors are generally retryable."""
    if any(
        pattern in error
        for pattern in [
            "device or resource busy",
            "resource temporarily unavailable",
            "temporary failure",
            "no space left on device",
            "disk full",
            "input/output error",
            "broken pipe",
        ]
    ):
        return True
    return None


def _post_transfer_open_rule(error: str) -> bool | None:
    """Post-transfer "could not open" errors, retryable if not missing files.

    Deliberately falls through (returns None) when the error is neither a
    missing file nor recognisably post-transfer, so the later non-retryable
    rules still get a chance to classify it.
    """
    if "fatal: could not open" not in error:
        return None
    if "no such file or directory" in error:
        return False
    # If it's after pack transfer, could be transient
    if "total" in error or "delta" in error:
        return True
    return None


def _repository_missing_rule(error: str) -> bool | None:
    """Repository not found is not retryable."""
    if "repository not found" in error or "not found" in error:
        return False
    return None


def _permission_rule(error: str) -> bool | None:
    """Permission errors are not retryable."""
    if "permission denied" in error or "access denied" in error:
        return False
    return None


def _authentication_rule(error: str) -> bool | None:
    """Authentication failures are not retryable."""
    if "authentication failed" in error or "host key verification failed" in error:
        return False
    return None


def _git_setup_rule(error: str) -> bool | None:
    """Git setup errors are not retryable."""
    if "fatal: --stdin requires a git repository" in error:
        return False
    return None


# Ordered; the first rule returning a verdict wins.  A rule returning None means
# "no opinion", letting later rules decide.
_FILESYSTEM_RETRY_RULES: tuple[Callable[[str], bool | None], ...] = (
    _missing_path_rule,
    _config_lock_rule,
    _git_dir_lock_rule,
    _transient_io_rule,
    _post_transfer_open_rule,
    _repository_missing_rule,
    _permission_rule,
    _authentication_rule,
    _git_setup_rule,
)


def is_filesystem_error_retryable(error_msg: str) -> bool:
    """Determine if a filesystem error should be retried.

    Args:
        error_msg: Error message to analyze

    Returns:
        True if error should be retried
    """
    error_lower = error_msg.lower()

    for rule in _FILESYSTEM_RETRY_RULES:
        verdict = rule(error_lower)
        if verdict is not None:
            return verdict

    # Default to retryable for unknown filesystem errors
    return True


def is_retryable_clone_error(
    process_result: subprocess.CompletedProcess[str],
) -> bool:
    """Check if a clone error is retryable.

    Args:
        process_result: Completed subprocess result

    Returns:
        True if error should be retried
    """
    stderr = process_result.stderr.strip()
    stdout = process_result.stdout.strip()
    error_output = f"{stderr}\n{stdout}".strip().lower()

    # Non-retryable errors (should not be retried)
    non_retryable_patterns = [
        "permission denied",
        "host key verification failed",
        "authentication failed",
        "repository not found",
        "not found",
        "does not exist",
        "invalid",
        "malformed",
        "fatal: not a git repository",
        "access denied",
    ]

    if (
        "fatal: could not open" in error_output
        and "total" in error_output
        and "delta" in error_output
    ):
        # Only retryable if not a missing file error
        if "no such file or directory" in error_output:
            logger.debug(
                f"Post-transfer missing file error (non-retryable): {error_output[:100]}..."
            )
            return False
        # Otherwise can be transient I/O stress - allow retries
        logger.debug(
            f"Post-transfer file error detected (retryable): {error_output[:100]}..."
        )
        return True

    if any(pattern in error_output for pattern in non_retryable_patterns):
        logger.debug(f"Non-retryable error detected: {error_output[:100]}...")
        return False

    # Retryable errors
    retryable_patterns = [
        "timeout",
        "connection refused",
        "connection timed out",
        "network",
        "temporary failure",
        "early eof",
        "remote end hung up",
        "transfer closed",
        "rpc failed",
        "could not resolve hostname",
        "ssh: connect to host",
        "connection reset",
        "could not lock config file",  # File locking is temporary and retryable (but check for missing files elsewhere)
    ]

    if any(pattern in error_output for pattern in retryable_patterns):
        logger.debug(f"Retryable error detected: {error_output[:100]}...")
        return True

    # For unknown errors, default to retryable but log it
    logger.warning(
        f"Unknown error pattern, defaulting to retryable: {error_output[:100]}..."
    )
    return True


# Ordered (base_delay, max_delay) pairs in seconds; the first match wins.
# Transient contention is retried almost immediately, whereas disk exhaustion
# and authentication problems back off hard to avoid hammering a failing
# resource.
_ADAPTIVE_DELAY_BOUNDS: tuple[
    tuple[Callable[[str], bool], tuple[float, float]], ...
] = (
    # Config file locking errors get very short delays - these are transient
    (lambda e: "could not lock config file" in e, (0.2, 1.5)),
    # Filesystem I/O errors after pack transfer - short delays, likely transient
    (
        lambda e: "could not open" in e and ("total" in e or "delta" in e),
        (0.5, 2.0),
    ),
    # Generic filesystem errors - moderate delays
    (
        lambda e: any(
            pattern in e
            for pattern in ["could not open", "device busy", "resource busy"]
        ),
        (1.0, 4.0),
    ),
    # Disk space errors get longer delays
    (lambda e: "no space left" in e or "disk full" in e, (5.0, 15.0)),
    # Network errors get standard delays
    (
        lambda e: any(
            pattern in e
            for pattern in [
                "timeout",
                "connection",
                "network",
                "early eof",
                "remote end hung up",
            ]
        ),
        (2.0, 10.0),
    ),
    # SSH/authentication errors - longer delays to avoid hammering
    (
        lambda e: any(
            pattern in e for pattern in ["ssh", "authentication", "permission"]
        ),
        (3.0, 12.0),
    ),
)


def _adaptive_delay_bounds(error_lower: str) -> tuple[float, float]:
    """Pick base and maximum backoff for an error class.

    Args:
        error_lower: Lowercased error message

    Returns:
        Tuple of (base_delay, max_delay) in seconds
    """
    for matches, bounds in _ADAPTIVE_DELAY_BOUNDS:
        if matches(error_lower):
            return bounds
    # Default delays for unknown errors
    return 1.0, 8.0


def calculate_adaptive_delay(attempt: int, error_msg: str) -> float:
    """Calculate adaptive delay based on error type and attempt.

    Args:
        attempt: Current attempt number (1-based)
        error_msg: Error message to analyze

    Returns:
        Delay in seconds
    """
    base_delay, max_delay = _adaptive_delay_bounds(error_msg.lower())

    # Exponential backoff with jitter
    delay = base_delay * (1.4 ** (attempt - 1))
    delay = min(delay, max_delay)

    # Add random jitter to prevent thundering herd (proportional to delay)
    jitter_factor = 0.2  # 20% jitter
    jitter = random.uniform(-jitter_factor * delay, jitter_factor * delay)
    return max(0.1, delay + jitter)  # Ensure minimum 100ms delay
