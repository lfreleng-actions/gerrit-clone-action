# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""Retry policy for GitHub GraphQL page requests.

GitHub answers large or cold listings with transient failures: 502,
503 and 429 responses, GraphQL error payloads, and timeouts.  Each
helper here decides whether a failed page is worth another attempt,
and if so reports it and waits with exponential backoff.
"""

from __future__ import annotations

import time as time_mod
from typing import TYPE_CHECKING, Any

from rich.console import Console

from source_clone.logging import get_logger

if TYPE_CHECKING:
    from source_clone.rate_limit import RateLimitBudget

logger = get_logger(__name__)

# HTTP statuses that indicate a transient GitHub failure worth retrying.
TRANSIENT_STATUS_CODES = (502, 503, 429)


def retry_backoff(retry: int) -> int:
    """Return the backoff for a retry attempt.

    Starts at 5 s so a cold-cache 502 has time to warm before we retry.

    Args:
        retry: Zero-based retry counter

    Returns:
        Seconds to wait before the next attempt
    """
    # Annotated because `int ** int` is typed as Any (a negative
    # exponent yields a float), which would otherwise leak out of the
    # declared int return.
    backoff: int = min(30, 5 * (2**retry))
    return backoff


def pause_before_retry(
    reason: str,
    org: str,
    backoff: int,
    retry: int,
    max_retries: int,
    budget: RateLimitBudget,
) -> None:
    """Print a compact console warning and sleep before retrying.

    Args:
        reason: Short description of the failure, e.g. "GraphQL error"
        org: Organization name
        backoff: Seconds to wait
        retry: Zero-based retry counter
        max_retries: Retries allowed per page
        budget: Shared rate-limit budget tracker
    """
    snap = budget.snapshot
    budget_pct = f"{snap.budget_fraction:.1%}" if snap.limit > 0 else "unknown"
    Console(stderr=True).print(
        f"[yellow]⚠️ {reason} "
        f"for GitHub owner: "
        f"{org}, retrying in {backoff}s "
        f"({retry + 1}/{max_retries}) "
        f"[Budget remaining: "
        f"{budget_pct}][/yellow]"
    )
    time_mod.sleep(backoff)


def retry_transient_status(
    status_code: int,
    org: str,
    retry: int,
    max_retries: int,
    budget: RateLimitBudget,
) -> bool:
    """Handle a transient HTTP status, returning whether to retry.

    Args:
        status_code: HTTP status returned by GitHub
        org: Organization name
        retry: Zero-based retry counter
        max_retries: Retries allowed per page
        budget: Shared rate-limit budget tracker

    Returns:
        True if the caller should retry the page
    """
    if retry >= max_retries:
        logger.error(
            "GraphQL failed after %d retries (HTTP %d) for %s",
            max_retries,
            status_code,
            org,
        )
        return False

    backoff = retry_backoff(retry)
    # Detailed message for file log only
    logger.info(
        "GraphQL transient error %d for %s, retrying in %ds (%d/%d)",
        status_code,
        org,
        backoff,
        retry + 1,
        max_retries,
    )
    # Compact one-liner for the console
    pause_before_retry(
        f"GraphQL transient error {status_code}",
        org,
        backoff,
        retry,
        max_retries,
        budget,
    )
    return True


def retry_graphql_errors(
    errors: Any,
    org: str,
    retry: int,
    max_retries: int,
    budget: RateLimitBudget,
) -> bool:
    """Handle a GraphQL-level error payload, returning whether to retry.

    Args:
        errors: The ``errors`` array from the GraphQL response
        org: Organization name
        retry: Zero-based retry counter
        max_retries: Retries allowed per page
        budget: Shared rate-limit budget tracker

    Returns:
        True if the caller should retry the page
    """
    # Full error detail for file log only
    logger.info("GraphQL errors: %s", errors)
    if retry >= max_retries:
        return False

    backoff = retry_backoff(retry)
    pause_before_retry("GraphQL error", org, backoff, retry, max_retries, budget)
    return True


def retry_after_exception(
    error: Exception,
    org: str,
    retry: int,
    max_retries: int,
    budget: RateLimitBudget,
) -> bool:
    """Handle an unexpected exception, returning whether to retry.

    Args:
        error: The exception raised while fetching or parsing the page
        org: Organization name
        retry: Zero-based retry counter
        max_retries: Retries allowed per page
        budget: Shared rate-limit budget tracker

    Returns:
        True if the caller should retry the page
    """
    if retry >= max_retries:
        logger.error(
            "GraphQL query failed after %d retries: %s",
            max_retries,
            error,
        )
        return False

    backoff = retry_backoff(retry)
    # Full traceback for file log only
    logger.info(
        "GraphQL query failed: %s (retrying in %ds, %d/%d)",
        error,
        backoff,
        retry + 1,
        max_retries,
    )
    pause_before_retry("GraphQL query failed", org, backoff, retry, max_retries, budget)
    return True
