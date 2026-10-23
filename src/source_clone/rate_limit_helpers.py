# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""Stateless helpers for interpreting GitHub rate-limit responses."""

from __future__ import annotations

from typing import Any

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


__all__ = [
    "extract_rate_limit_info",
    "is_rate_limited",
    "parse_retry_after",
]
