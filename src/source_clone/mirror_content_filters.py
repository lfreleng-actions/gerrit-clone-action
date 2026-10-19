# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""Content filtering of cloned repositories before they are pushed.

Filters each successful clone with the run's ``--remove-files`` /
``--git-filter`` / ``--redact-secrets`` options and the tree's recorded
intent, and aborts the batch if any repository could not be filtered.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, Any

from source_clone.content_spec import ContentFilterSpec
from source_clone.content_stage import ApplyContentFilters, filter_repository
from source_clone.logging import get_logger

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from source_clone.models import Config

logger = get_logger(__name__)


@dataclass(frozen=True)
class ContentFilterRunner:
    """Content-filter helpers, resolved by the caller at call time."""

    is_shallow: Callable[[Path], bool]
    apply_filters: ApplyContentFilters


@dataclass(frozen=True)
class ContentFilterSettings:
    """Content filtering for a mirror batch: its options and the tree's intent."""

    spec: ContentFilterSpec | None
    clone_timeout: int

    @property
    def enabled(self) -> bool:
        """Whether anything is filtered at all."""
        return self.spec is not None


@dataclass
class _FilterTally:
    """Running totals for a content filtering pass."""

    succeeded: int = 0
    failed: int = 0
    failed_projects: set[str] = field(default_factory=set)


def _filter_one_repo(
    clone_result: Any,
    settings: ContentFilterSettings,
    runner: ContentFilterRunner,
    tally: _FilterTally,
) -> None:
    """Filter one cloned repository, as its project's filters decide."""
    assert settings.spec is not None  # Checked by apply_filters_to_clones.
    reason = filter_repository(
        settings.spec,
        clone_result.path,
        settings.spec.project_name(clone_result.path),
        settings.clone_timeout,
        is_shallow=runner.is_shallow,
        apply=runner.apply_filters,
    )
    if reason is None:
        tally.succeeded += 1
        return
    tally.failed += 1
    tally.failed_projects.add(clone_result.project.name)
    logger.warning(
        "Content filter failed for %s: %s", clone_result.project.name, reason
    )


def with_content_filters(
    config: Config,
    remove_file_patterns: list[str] | None,
    git_filter_projects: dict[str, list[str]] | None,
    redact_secrets: bool,
) -> Config:
    """*config*, carrying the mirror's own content-filter options.

    :meth:`~source_clone.mirror_manager.MirrorManager.mirror_projects`
    combines them with what the tree recorded (see
    :mod:`source_clone.content_intent_resolve`) before it clones anything.

    Returns:
        *config* itself when no filter was requested, else a copy.
    """
    if not (remove_file_patterns or git_filter_projects or redact_secrets):
        return config
    spec = ContentFilterSpec(
        remove_file_patterns, git_filter_projects, redact_secrets, config.path
    )
    return replace(config, content_filters=spec)


def apply_filters_to_clones(
    clone_results: list[Any],
    settings: ContentFilterSettings,
    runner: ContentFilterRunner,
) -> None:
    """Filter every successful clone in place.

    Args:
        clone_results: Results from the clone phase
        settings: Requested filtering behaviour
        runner: Content-filter helpers to invoke

    Raises:
        RuntimeError: If any repository could not be filtered.  Silently
            dropping projects would make them disappear from the
            manifest, so the whole batch is aborted instead.
    """
    if not settings.enabled:
        return

    tally = _FilterTally()
    logger.info("🔧 Applying content filters to cloned repositories...")
    for clone_result in clone_results:
        if not clone_result.success or not clone_result.path:
            continue
        if getattr(clone_result, "content_filtered", False):
            # Re-filtered as part of its staged refresh already.
            tally.succeeded += 1
            continue
        _filter_one_repo(clone_result, settings, runner, tally)
    logger.info(
        "Content filtering complete: %d succeeded, %d failed",
        tally.succeeded,
        tally.failed,
    )

    # Abort the batch if any content filters failed — silently
    # dropping projects would make them disappear from the manifest.
    if tally.failed_projects:
        raise RuntimeError(
            f"Content filtering failed for {tally.failed} project(s), "
            f"aborting batch: {sorted(tally.failed_projects)}"
        )
