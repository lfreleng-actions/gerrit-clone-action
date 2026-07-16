# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2025 Matthew Watkins <mwatkins@linuxfoundation.org>

"""Dual-channel output utilities for consistent logging and console display.

This module implements a dual-channel output strategy: every user-visible
message is dispatched to both the Python logging subsystem (for structured
log files, CI runners, and post-mortem analysis) and to a Rich console
(for immediate, styled terminal feedback).

The approach is inspired by the dependamerge project's ``output_utils``
module but adapted for the gerrit-clone-action domain, adding a
``format_rate_limit_table`` helper that renders GitHub API rate-limit
diagnostics as a compact Rich table suitable for both interactive
terminals and CI log output.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

from rich.console import Console
from rich.table import Table


def log_and_print(
    logger: logging.Logger,
    console: Console,
    message: str,
    style: str | None = None,
    level: str = "info",
) -> None:
    """Log a message and print it to the console for immediate visibility.

    Sends *message* to both the logging framework (at the requested
    severity) and to the user-facing console.  When a Rich *style* is
    provided the console output is styled via ``console.print``; when
    it is ``None`` the message is emitted through ``console.print``
    without styling so that all output flows through the same Rich
    console (typically configured to stderr).

    Args:
        logger: Logger instance to use for the structured log entry.
        console: Rich Console instance for styled terminal output.
        message: The human-readable message to emit.
        style: Optional Rich style string (e.g. ``"bold red"``).
            When provided, ``console.print`` is used with this style.
            When ``None``, ``console.print`` is used without styling.
        level: Log level name — one of ``'debug'``, ``'info'``,
            ``'warning'``, or ``'error'``.  Defaults to ``'info'``.

    Examples:
        >>> import logging
        >>> from rich.console import Console
        >>> logger = logging.getLogger(__name__)
        >>> console = Console()
        >>> log_and_print(logger, console, "Clone complete", level="info")
        >>> log_and_print(
        ...     logger, console, "Rate limit exceeded",
        ...     style="bold red", level="error",
        ... )
    """
    log_func = getattr(logger, level.lower(), logger.info)
    log_func(message)
    if style:
        console.print(message, style=style)
    else:
        console.print(message)


def format_rate_limit_table(
    rate_info: dict[str, Any],
    budget_snapshot: Any | None = None,
    response_status: int | None = None,
    response_body: str | None = None,
) -> Table:
    """Build a Rich table summarising GitHub API rate-limit diagnostics.

    The returned table is suitable for rendering via ``console.print``
    and provides a concise, at-a-glance view of the current rate-limit
    state together with optional response metadata.

    Args:
        rate_info: Dictionary of rate-limit headers as returned by
            ``extract_rate_limit_info`` (keys are header names such as
            ``X-RateLimit-Remaining``).
        budget_snapshot: Optional ``RateLimitSnapshot`` instance.  When
            provided its ``budget_fraction`` property is included in
            the table.
        response_status: Optional HTTP status code from the response
            that triggered this diagnostic.
        response_body: Optional response body text.  Truncated to 200
            characters if longer.

    Returns:
        A ``rich.table.Table`` titled *⚠️ API Error Diagnostics*
        containing one row per available metric.
    """
    table = Table(title="⚠️ API Error Diagnostics")
    table.add_column("Metric", style="cyan", no_wrap=True)
    table.add_column("Value", style="magenta")

    # -- Rate-limit remaining / limit --
    remaining = rate_info.get("X-RateLimit-Remaining")
    limit = rate_info.get("X-RateLimit-Limit")
    if remaining is not None or limit is not None:
        remaining_display = "?" if remaining is None else str(remaining)
        limit_display = "?" if limit is None else str(limit)
        table.add_row(
            "Remaining / Limit",
            f"{remaining_display} / {limit_display}",
        )

    # -- Resource type --
    resource = rate_info.get("X-RateLimit-Resource")
    if resource is not None:
        table.add_row("Resource", str(resource))

    # -- Reset time (human-readable) --
    reset_epoch = rate_info.get("X-RateLimit-Reset")
    if reset_epoch is not None:
        try:
            reset_dt = datetime.fromtimestamp(
                int(reset_epoch), tz=UTC
            )
            table.add_row(
                "Reset Time",
                reset_dt.strftime("%Y-%m-%d %H:%M:%S UTC"),
            )
        except (ValueError, TypeError, OSError):
            table.add_row("Reset Time", str(reset_epoch))

    # -- Used count --
    used = rate_info.get("X-RateLimit-Used")
    if used is not None:
        table.add_row("Used", str(used))

    # -- Retry-After (if present) --
    retry_after = rate_info.get("Retry-After")
    if retry_after is not None:
        retry_after_str = str(retry_after).strip()
        try:
            float(retry_after_str)
        except (TypeError, ValueError):
            # HTTP-date or other non-numeric value — show as-is
            display = retry_after_str
        else:
            display = f"{retry_after_str}s"
        table.add_row("Retry-After", display)

    # -- Response status --
    if response_status is not None:
        table.add_row("Response Status", str(response_status))

    # -- Response body (truncated) --
    if response_body is not None:
        body_display = (
            response_body[:200] + "…"
            if len(response_body) > 200
            else response_body
        )
        table.add_row("Response Body", body_display)

    # -- Budget fraction from snapshot --
    if budget_snapshot is not None:
        fraction = getattr(budget_snapshot, "budget_fraction", None)
        if fraction is not None:
            table.add_row(
                "Budget Fraction",
                f"{fraction:.1%}",
            )

    return table
