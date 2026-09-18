# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""Bookkeeping around a dependency-safe batch.

Projects are cloned in batches chosen so that a parent repository is
always complete before anything nested inside it starts.  The two
decisions that belong to a batch rather than to a clone live here: which
of its projects are parents of nested repositories, and whether a
failure in it should stop the run.

The batching itself is in :mod:`gerrit_clone.clone_ordering`, and the
loop that drives it in :mod:`gerrit_clone.clone_orchestrator`.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from gerrit_clone.logging import get_logger

if TYPE_CHECKING:
    from gerrit_clone.models import CloneResult, Project

logger = get_logger(__name__)


def mark_batch_parents(
    batch: list[Project],
    nested_candidates: set[str],
    nested_parent_usage: set[str],
    project_name_index: set[str],
) -> None:
    """Record which projects in *batch* are parents of nested repositories.

    Args:
        batch: Projects about to be cloned together.
        nested_candidates: Names that look like they sit inside another
            project, from the planning pass.
        nested_parent_usage: Parents seen so far, added to in place.
        project_name_index: Every project name in the run, so that a
            prefix match is only believed when the parent is really
            being cloned.
    """
    # Mark parents in this batch (depth == 0 or any project with children)
    batch_depth = batch[0].name.count("/") if batch else 0
    for pr in batch:
        prefix = pr.name + "/"
        if (
            any(cand.startswith(prefix) for cand in nested_candidates)
            and pr.name in project_name_index
        ):
            if pr.name not in nested_parent_usage and batch_depth == 0:
                # First time we see this parent (top-level batch)
                logger.debug(
                    f"👪 Parent ready for nesting: {pr.name} (children pending)"
                )
            nested_parent_usage.add(pr.name)

    # Promote first few nested parents summary (only for top-level batch)
    if batch_depth == 0 and nested_parent_usage:
        sample_parents = sorted(nested_parent_usage)[:5]
        suffix = " ..." if len(nested_parent_usage) > 5 else ""
        logger.debug(
            f"📂 Parent repositories prepared "
            f"({len(nested_parent_usage)}): {sample_parents}{suffix}"
        )


def batch_should_stop(
    batch_results: list[CloneResult],
    batch_number: int,
    label: str,
    exit_on_error: bool,
) -> bool:
    """Return ``True`` when ``--exit-on-error`` should halt batching.

    Args:
        batch_results: Results from the batch just completed.
        batch_number: 1-based index of that batch, named alone in the
            error so it reads as an ordinal rather than a fraction.
        label: ``"<number>/<total>"`` progress label for the debug line.
        exit_on_error: Whether a failure should stop the run at all.

    Returns:
        Whether to stop after this batch.
    """
    if not exit_on_error:
        return False
    failed_results = [r for r in batch_results if r.failed]
    if not failed_results:
        return False
    failed_project = failed_results[0]
    logger.error(
        f"🛑 Stopping after batch {batch_number}: "
        f"{failed_project.project.name} failed with: "
        f"{failed_project.error_message}"
    )
    logger.debug(f"📊 Processed {label} batches before stopping")
    return True
