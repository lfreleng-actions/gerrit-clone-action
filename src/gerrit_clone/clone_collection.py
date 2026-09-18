# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""Draining a batch's futures and recording what they report.

A clone that raises rather than returning still has to appear in the
results: a project with no result at all would simply vanish from the
report.  Every exit from here therefore leaves a result behind, whether
the worker produced one or not.

How the pool itself is built and abandoned belongs to
:mod:`gerrit_clone.clone_orchestrator`.
"""

from __future__ import annotations

from concurrent.futures import as_completed
from typing import TYPE_CHECKING

from gerrit_clone.clone_reporting import log_project_result
from gerrit_clone.clone_results import build_failure_result
from gerrit_clone.logging import get_logger

if TYPE_CHECKING:
    import threading
    from concurrent.futures import Future

    from gerrit_clone.models import CloneResult, Config, Project
    from gerrit_clone.progress import ProgressTracker

logger = get_logger(__name__)


def record_future_result(
    config: Config,
    progress_tracker: ProgressTracker | None,
    future: Future[CloneResult],
    project: Project,
    results: list[CloneResult],
) -> bool:
    """Record one completed clone future.

    Args:
        config: Active configuration, supplying the exit-on-error policy.
        progress_tracker: Display to update, if one is running.
        future: The completed future.
        project: Project that future was submitted for, named in the
            error when the worker raised instead of returning.
        results: Results recorded so far, appended to in place.

    Returns:
        ``True`` when the caller should stop consuming further futures.
    """
    try:
        result = future.result()
        results.append(result)

        if progress_tracker:
            progress_tracker.update_project_result(result)

        log_project_result(result)

        if config.exit_on_error and result.failed:
            logger.error(
                f"🛑 Exiting on error: {project.name} failed with: "
                f"{result.error_message}"
            )
            return True

    except Exception as e:
        logger.error(f"Unexpected error cloning {project.name}: {e}")
        error_result = build_failure_result(config, project, str(e))
        results.append(error_result)

        if progress_tracker:
            progress_tracker.update_project_result(error_result)

        if config.exit_on_error:
            logger.error(
                f"🛑 Exiting on error: {project.name} failed with exception: {e}"
            )
            return True

    return False


def consume_clone_futures(
    config: Config,
    progress_tracker: ProgressTracker | None,
    shutdown_event: threading.Event,
    future_to_project: dict[Future[CloneResult], Project],
    results: list[CloneResult],
    overall_timeout: int,
) -> None:
    """Collect clone results as their futures complete.

    Args:
        config: Active configuration, supplying the exit-on-error policy.
        progress_tracker: Display to update, if one is running.
        shutdown_event: Set when the manager is being shut down.
        future_to_project: Every future submitted for this batch.
        results: Results recorded so far, appended to in place.
        overall_timeout: Seconds to wait for the batch as a whole.

    Raises:
        TimeoutError: If *overall_timeout* elapses, which the caller
            handles by abandoning the batch and reporting what it has.
    """
    logger.debug("Starting to wait for clone task completion...")
    for future in as_completed(future_to_project, timeout=overall_timeout):
        logger.debug("Clone task completed, processing result...")
        if shutdown_event.is_set():
            # Cancel remaining futures on shutdown
            for remaining_future in future_to_project:
                remaining_future.cancel()
            break

        project = future_to_project[future]

        if record_future_result(config, progress_tracker, future, project, results):
            # Cancel remaining futures
            for remaining_future in future_to_project:
                if not remaining_future.done():
                    remaining_future.cancel()
            break
