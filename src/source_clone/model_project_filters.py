# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""Project name pattern matching and include/exclude filtering.

Leaf module implementing the pattern syntax shared by the ``--include`` and
``--exclude`` options: shell-style wildcards plus plain hierarchical prefix
matching. Kept free of runtime package imports so configuration and discovery
modules may depend on it.
"""

from __future__ import annotations

import fnmatch
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from source_clone.model_project import Project


def match_project_pattern(project_name: str, pattern: str) -> bool:
    """Match a project name against a pattern supporting wildcards.

    Patterns may contain shell-style wildcards (``*``, ``?``, ``[seq]``,
    ``[!seq]``).  Matching is case-sensitive and performed against the
    full project name (e.g. ``testsuite/pythonsdk-tests``).

    Additionally, **hierarchical matching** is applied for patterns that
    do **not** contain wildcard characters: a plain pattern like ``ccsdk``
    matches both the exact name ``ccsdk`` *and* any child such as
    ``ccsdk/apps``.

    Args:
        project_name: Full Gerrit project name (e.g. ``testsuite/pythonsdk-tests``).
        pattern: Pattern string, optionally containing wildcards.

    Returns:
        ``True`` if *project_name* matches *pattern*.
    """
    # Fast exact match (most common case)
    if project_name == pattern:
        return True

    has_wildcards = any(ch in pattern for ch in ("*", "?", "["))

    if has_wildcards:
        return fnmatch.fnmatchcase(project_name, pattern)

    # Plain pattern → also match hierarchical children
    return project_name.startswith(f"{pattern}/")


def normalize_project_list(raw: list[str]) -> list[str]:
    """Normalize a list of project patterns.

    Strips whitespace and leading slashes, drops empty entries,
    splits on commas and spaces, and de-duplicates while
    preserving insertion order.  Leading-slash stripping ensures
    that ``/ccsdk`` matches the discovered project name ``ccsdk``
    (Gerrit projects are stored without a leading ``/``).

    Args:
        raw: List of raw pattern strings (may contain commas/spaces).

    Returns:
        Normalized, de-duplicated list of patterns.
    """
    seen: set[str] = set()
    normalized: list[str] = []
    for entry in raw:
        # Split on commas first, then whitespace within each segment
        for comma_part in entry.split(","):
            for token in comma_part.split():
                clean = token.strip().lstrip("/")
                if clean and clean not in seen:
                    normalized.append(clean)
                    seen.add(clean)
    return normalized


def filter_projects(
    projects: list[Project],
    include_patterns: list[str] | None = None,
    exclude_patterns: list[str] | None = None,
) -> list[Project]:
    """Filter a list of projects using include/exclude patterns.

    When *include_patterns* is non-empty, only projects matching at least
    one include pattern are kept.  Then any project matching an
    *exclude_patterns* entry is removed.  Both lists support shell-style
    wildcards (``*``, ``?``, ``[seq]``) as well as plain hierarchical
    matching (see :func:`match_project_pattern`).

    Args:
        projects: Source list of projects.
        include_patterns: If non-empty, only matching projects are kept.
        exclude_patterns: Matching projects are removed (applied after include).

    Returns:
        Filtered list of projects (new list; originals are not mutated).
    """
    result = list(projects)

    if include_patterns:
        result = [
            p
            for p in result
            if any(match_project_pattern(p.name, pat) for pat in include_patterns)
        ]

    if exclude_patterns:
        result = [
            p
            for p in result
            if not any(match_project_pattern(p.name, pat) for pat in exclude_patterns)
        ]

    return result
