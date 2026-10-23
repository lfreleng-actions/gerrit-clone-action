# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""Planning and reporting helpers for batched GitHub mutations.

Batch create and delete share the same shape: derive a retry budget and
a token-bucket rate from the batch size, fan out, then fold the
``asyncio.gather`` results back into a name-keyed mapping and report
the outcome.  Those concerns live here so the batch entry points stay
readable.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, TypeVar

from source_clone.logging import get_logger
from source_clone.rate_limit import TokenBucketLimiter

if TYPE_CHECKING:
    from collections.abc import Sequence
    from typing import Any

    from source_clone.github_models import GitHubRepo

logger = get_logger(__name__)

T = TypeVar("T")


def batch_retry_budget(item_count: int) -> int:
    """Derive the per-item retry budget for a batch of this size.

    Args:
        item_count: Number of repositories in the batch

    Returns:
        Maximum retry attempts allowed per repository
    """
    return max(5, min(15, item_count // 10))


def scale_create_interval(item_count: int, rate_limit_interval: float) -> float:
    """Widen the baseline request interval for large create batches.

    Large batches trip GitHub's secondary rate limit quickly, so start
    more conservatively rather than discovering the limit the hard way.

    Args:
        item_count: Number of repositories in the batch
        rate_limit_interval: Caller-supplied baseline interval

    Returns:
        The interval to derive the token-bucket rate from
    """
    effective_interval = rate_limit_interval
    if item_count > 100:
        effective_interval = max(effective_interval, 3.0)
    if item_count > 200:
        effective_interval = max(effective_interval, 4.0)
    return effective_interval


def build_batch_limiter(
    rate_limit_interval: float,
    burst: int,
    shared_limiter: TokenBucketLimiter | None = None,
) -> TokenBucketLimiter:
    """Return the limiter to pace a batch with.

    Mutations cost 2 tokens each, so the effective mutation rate is
    half the token rate derived here.

    Args:
        rate_limit_interval: Baseline seconds between requests
        burst: Token-bucket burst capacity
        shared_limiter: Optional pre-existing limiter to share state
            across phases (e.g. delete → create)

    Returns:
        The shared limiter when supplied, otherwise a new one
    """
    rate = 1.0 / max(rate_limit_interval, 0.1)
    return shared_limiter or TokenBucketLimiter(
        rate=rate,
        burst=burst,
        min_rate=0.02,
        recovery_seconds=120.0,
    )


def collect_batch_results(
    results: Sequence[tuple[str, T] | BaseException],
    failure_message: str,
) -> dict[str, T]:
    """Fold gathered task results into a name-keyed mapping.

    Args:
        results: Values returned by ``asyncio.gather`` with
            ``return_exceptions=True``
        failure_message: Log prefix for tasks that raised

    Returns:
        Mapping of repository name to task result, skipping failures
    """
    results_map: dict[str, T] = {}
    for result in results:
        if isinstance(result, BaseException):
            logger.error(f"{failure_message}: {result}")
            continue
        name, value = result
        results_map[name] = value
    return results_map


def log_delete_batch_outcome(
    results_map: dict[str, tuple[bool, str | None]],
    repo_names: list[str],
) -> None:
    """Report the outcome of a batch delete.

    Args:
        results_map: Mapping of repository name to (success, error)
        repo_names: Names originally submitted to the batch
    """
    success_count = sum(1 for s, _ in results_map.values() if s)
    failed_count = len(repo_names) - success_count

    if failed_count > 0:
        failed_repos = [
            name for name, (success, error) in (results_map.items()) if not success
        ]
        logger.error(
            "Batch delete: %d/%d successful, %d FAILED",
            success_count,
            len(repo_names),
            failed_count,
        )
        logger.error(f"Failed repos: {failed_repos}")
        for name in failed_repos[:5]:
            _, error = results_map[name]
            logger.error(f"  - {name}: {error}")
    else:
        logger.info(
            "Batch delete completed: %d/%d successful",
            success_count,
            len(repo_names),
        )


def log_create_batch_outcome(
    results_map: dict[str, tuple[GitHubRepo | None, str | None]],
    repo_configs: list[dict[str, Any]],
) -> None:
    """Report the outcome of a batch create.

    Args:
        results_map: Mapping of repository name to (repo, error)
        repo_configs: Configurations originally submitted to the batch
    """
    success_count = sum(1 for repo, _ in results_map.values() if repo is not None)
    failed_count = len(repo_configs) - success_count

    if failed_count > 0:
        failed_repos = [
            cfg["name"]
            for cfg in repo_configs
            if results_map.get(cfg["name"], (None, None))[0] is None
        ]
        logger.warning(
            "Batch create: %d/%d successful, %d failed",
            success_count,
            len(repo_configs),
            failed_count,
        )
        logger.warning(f"Failed repos: {failed_repos[:10]}")
    else:
        logger.info(
            "Batch create completed: %d/%d successful",
            success_count,
            len(repo_configs),
        )
