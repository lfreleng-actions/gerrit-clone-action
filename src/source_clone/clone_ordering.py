# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""Project ordering, batching and sizing for dependency-safe cloning.

Pure scheduling helpers used by :class:`~source_clone.clone_orchestrator
.CloneManager`: de-duplication, topological ordering so parent projects
are always cloned before their children, grouping into depth-based
batches that can safely run in parallel, and thread/disk sizing.
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING

from source_clone.logging import get_logger

if TYPE_CHECKING:
    from pathlib import Path

    from source_clone.models import Project

logger = get_logger(__name__)


def remove_duplicate_projects(projects: list[Project]) -> list[Project]:
    """Remove duplicate projects by name.

    Args:
        projects: Input projects

    Returns:
        Unique projects
    """
    seen = set()
    unique_projects = []
    for project in projects:
        if project.name not in seen:
            unique_projects.append(project)
            seen.add(project.name)

    if len(unique_projects) != len(projects):
        logger.debug(
            f"Removed {len(projects) - len(unique_projects)} duplicate projects"
        )

    return unique_projects


def _build_dependency_graph(
    project_names: set[str],
) -> tuple[dict[str, str], dict[str, set[str]], dict[str, int]]:
    """Derive parent/child dependencies from hierarchical project names.

    Returns:
        Tuple of ``(dependencies, dependents, in_degree)`` where
        ``dependencies`` maps a child to its immediate parent.
    """
    dependencies: dict[str, str] = {}  # child -> parent (only immediate parent)
    dependents: dict[str, set[str]] = {}  # parent -> set of children
    in_degree: dict[str, int] = {}  # project -> number of dependencies

    for name in project_names:
        in_degree[name] = 0

    for project_name in project_names:
        path_parts = project_name.split("/")
        # Find immediate parent dependency
        for i in range(len(path_parts) - 1, 0, -1):
            parent_path = "/".join(path_parts[:i])
            if parent_path in project_names:
                # Found immediate parent
                dependencies[project_name] = parent_path
                in_degree[project_name] = 1

                if parent_path not in dependents:
                    dependents[parent_path] = set()
                dependents[parent_path].add(project_name)
                break

    return dependencies, dependents, in_degree


def topological_sort_projects(projects: list[Project]) -> list[Project]:
    """Sort projects by dependencies using topological sort.

    This ensures parent projects are always processed before their children,
    completely eliminating directory conflicts during parallel processing.

    Args:
        projects: List of projects to sort

    Returns:
        Projects ordered by dependencies (parents before children)
    """
    project_map = {p.name: p for p in projects}
    project_names = set(project_map.keys())

    dependencies, dependents, in_degree = _build_dependency_graph(project_names)

    # Topological sort using Kahn's algorithm
    result = []
    queue = [name for name in project_names if in_degree[name] == 0]

    logger.debug(f"Dependency analysis: {len(dependencies)} dependencies found")
    if dependencies:
        sample_deps = dict(list(dependencies.items())[:3])
        logger.debug(f"Sample dependencies: {sample_deps}")

    while queue:
        current = queue.pop(0)
        result.append(project_map[current])

        for dependent in dependents.get(current, set()):
            in_degree[dependent] -= 1
            if in_degree[dependent] == 0:
                queue.append(dependent)

    # Verify all projects were processed
    if len(result) != len(projects):
        remaining = [
            name for name in project_names if name not in {p.name for p in result}
        ]
        logger.error(f"Topological sort failed - remaining projects: {remaining}")
        # Add remaining projects (shouldn't happen with valid hierarchies)
        for name in remaining:
            result.append(project_map[name])

    dependency_count = sum(1 for p in result if "/" in p.name)
    logger.debug(
        f"Dependency ordering complete: {len(result)} projects, {dependency_count} have dependencies"
    )

    # Show sample of ordering for debugging
    if result:
        sample_order = [p.name for p in result[:10]]
        logger.debug(f"Dependency order sample (first 10): {sample_order}")

    return result


def _log_batch_composition(batches: list[list[Project]]) -> None:
    """Log a preview of the first few depth-based batches."""
    for idx, batch in enumerate(batches[:5]):  # Show up to first 5 batches
        sample = [p.name for p in batch[:4]]
        if len(batch) > 4:
            sample.append(f"... +{len(batch) - 4} more")
        logger.debug(
            f"Batch {idx + 1} (depth={batch[0].name.count('/') if batch else 'n/a'}): {len(batch)} projects -> {sample}"
        )


def create_dependency_batches(projects: list[Project]) -> list[list[Project]]:
    """Create depth-based batches of projects for safe parallel cloning.

    Rationale:
      * We now deliberately allow parent + child (nested) repositories.
      * Dependency ordering alone collapsed into a single batch for most trees.
      * Grouping by hierarchical depth (slash count) gives:
          - Parents first (depth 0 / 1)
          - Immediate children next
          - Deeper descendants later
      * This reduces early I/O contention and improves log clarity.

    Args:
        projects: List of projects (already deduplicated)

    Returns:
        Ordered list of batches (each batch: projects of one depth level)
    """
    if not projects:
        return []

    depth_map: dict[int, list[Project]] = {}
    for p in projects:
        depth = p.name.count("/")
        depth_map.setdefault(depth, []).append(p)

    batches: list[list[Project]] = []
    for depth in sorted(depth_map.keys()):
        group = sorted(depth_map[depth], key=lambda pr: pr.name)
        batches.append(group)

    logger.debug(
        f"Created {len(batches)} depth-based batches (min depth={min(depth_map.keys(), default=0)}, max depth={max(depth_map.keys(), default=0)})"
    )
    _log_batch_composition(batches)

    return batches


def log_planning_summary(unique_projects: list[Project]) -> None:
    """Log the parent/child hierarchy analysis for a clone run."""
    # Planning / hierarchy analysis pass (parent -> direct child count)
    names = {p.name for p in unique_projects}
    parent_children: dict[str, int] = {}
    for parent in names:
        prefix = parent + "/"
        # Fast scan for direct descendants
        count = 0
        for candidate in names:
            if candidate != parent and candidate.startswith(prefix):
                count += 1
        if count:
            parent_children[parent] = count

    parent_count = len(parent_children)
    total_direct_children = sum(parent_children.values())
    logger.debug(
        f"Planning summary: {len(unique_projects)} repositories; "
        f"{parent_count} parents with {total_direct_children} direct child mappings"
    )
    if parent_children:
        sample_items = sorted(parent_children.items())[:5]
        logger.debug(f"Parent sample (up to 5): {sample_items}")


def get_disk_space_info(path: Path) -> str:
    """Get disk space information for logging."""
    try:
        stat = os.statvfs(path)
        free_bytes = stat.f_frsize * stat.f_bavail
        total_bytes = stat.f_frsize * stat.f_blocks
        free_gb = free_bytes / (1024**3)
        used_percent = ((total_bytes - free_bytes) / total_bytes) * 100
        return f"{free_gb:.1f}GB free ({used_percent:.1f}% used)"
    except (OSError, AttributeError):
        return "unknown"


def get_filesystem_safe_thread_count(projects: list[Project], max_threads: int) -> int:
    """Get thread count based on CPU cores unless explicitly overridden.

    With dependency-aware batching, conflicts are eliminated by proper scheduling,
    so we can safely use the full CPU-based thread count.

    Args:
        projects: Projects being processed
        max_threads: Maximum threads from config (CPU-based unless user specified)

    Returns:
        Thread count for clone operations
    """
    project_count = len(projects)

    # Use the configured thread count (CPU-based unless user explicitly set it)
    safe_count = max_threads

    logger.debug(
        f"Using {safe_count} threads for {project_count} projects (dependency conflicts eliminated by scheduling)"
    )
    return safe_count


def log_batch_preview(batches: list[list[Project]]) -> None:
    """Log a short sample of the first three dependency-safe batches."""
    for i, batch in enumerate(batches[:3]):  # Show first 3 batches
        sample_names = [p.name for p in batch[:5]]
        if len(batch) > 5:
            sample_names.append(f"... +{len(batch) - 5} more")
        logger.debug(f"Batch {i + 1} sample: {sample_names}")


def log_nested_summary(nested_candidates: set[str], nested_detected: set[str]) -> None:
    """Log how many nested repositories were detected after all batches.

    Wrapped in a broad ``try`` because this is purely diagnostic: a
    failure here must never abort an otherwise successful clone run.
    """
    try:
        total_candidates = len(nested_candidates)
        detected = len(nested_detected)
        if not total_candidates:
            return
        if not detected:
            logger.debug(
                f"🧬 No nested repositories detected out of {total_candidates} candidates"
            )
            return
        sample = sorted(nested_detected)[:5]
        logger.debug(
            f"🧬 Nested repositories detected: {detected}/{total_candidates} "
            f"(examples: {sample}{' ...' if detected > 5 else ''})"
        )
        # Undetected sample (potential missed nesting)
        undetected = sorted(nested_candidates - nested_detected)
        if undetected:
            undet_sample = undetected[:5]
            logger.debug(
                f"🔍 Nested candidates without detected parent linkage: {len(undetected)} "
                f"(examples: {undet_sample}{' ...' if len(undetected) > 5 else ''})"
            )
    except Exception as e:
        logger.debug(f"Nested summary logging failed: {e}")
