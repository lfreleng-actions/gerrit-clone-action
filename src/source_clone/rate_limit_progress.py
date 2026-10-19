# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""Progress reporting for batched asynchronous GitHub operations."""

from __future__ import annotations

import asyncio

from source_clone.logging import get_logger

logger = get_logger(__name__)


# AsyncProgressCounter - batch operation progress reporting


class AsyncProgressCounter:
    """Thread-safe counter for tracking batch operation progress.

    Emits a log message every *report_every* completions so the
    operator can see that work is proceeding.
    """

    def __init__(self, total: int, label: str, report_every: int = 10) -> None:
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

    async def record(self, *, success: bool, name: str) -> None:
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
                "📊 %s progress: %d/%d completed (%d succeeded, %d failed)",
                self._label,
                count,
                self._total,
                success_count,
                failed_count,
            )


__all__ = [
    "AsyncProgressCounter",
]
