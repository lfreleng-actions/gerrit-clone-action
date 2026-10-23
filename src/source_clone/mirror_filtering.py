# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""Hierarchical include/exclude filtering of Gerrit projects."""

from __future__ import annotations

from typing import TYPE_CHECKING

from source_clone.logging import get_logger
from source_clone.models import filter_projects

if TYPE_CHECKING:
    from source_clone.models import Project

logger = get_logger(__name__)


def filter_projects_by_hierarchy(
    projects: list[Project],
    filter_names: list[str],
    exclude_patterns: list[str] | None = None,
) -> list[Project]:
    """Filter projects using include/exclude patterns with wildcard support.

    Include patterns use hierarchical matching — a plain name like ``ccsdk``
    matches both the exact project ``ccsdk`` *and* any child such as
    ``ccsdk/apps``.  Shell-style wildcards (``*``, ``?``, ``[seq]``) are
    also supported (e.g. ``*sdk*`` matches ``ccsdk`` and ``pythonsdk-tests``).

    Exclude patterns are applied **after** inclusion and use the same
    matching rules.  A project that matches any exclude pattern is removed
    regardless of whether it matched an include pattern.

    Args:
        projects: List of all projects.
        filter_names: List of project name patterns to include.
            An empty list means "include everything".
        exclude_patterns: Optional list of project name patterns to exclude.

    Returns:
        Filtered list of projects.
    """
    include = filter_names if filter_names else None
    exclude = exclude_patterns if exclude_patterns else None

    if not include and not exclude:
        return projects

    filtered = filter_projects(
        projects,
        include_patterns=include,
        exclude_patterns=exclude,
    )

    parts: list[str] = []
    if include:
        parts.append(f"include={filter_names}")
    if exclude:
        parts.append(f"exclude={exclude_patterns}")
    logger.info(
        f"Filtered {len(projects)} projects to {len(filtered)} ({', '.join(parts)})"
    )
    return filtered
