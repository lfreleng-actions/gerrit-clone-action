# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""Removal of pre-existing local clone directories before a mirror run.

Provides the path selection and reporting around the deletion loop; the
deletion itself stays with the mirror manager.

A repository that a release up to v2.2.4 content-filtered, and any
directory holding one, is held back from the whole batch.  Those releases
recorded no filters, so neither the tree's intent nor this run can
re-apply them: cloned again, it would be pushed with ``git push --mirror``
unfiltered, and even as it stands ``--recreate`` would replace its GitHub
repository with it.  It is left alone until the operator deletes it and
clones it again with the filters it needs.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

from source_clone.content_intent_resolve import repositories_beneath
from source_clone.content_policy import PolicyReadError, recorded_policy
from source_clone.github_api import transform_gerrit_name_to_github
from source_clone.logging import get_logger
from source_clone.mirror_models import MirrorResult, MirrorStatus

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from source_clone.models import Project

logger = get_logger(__name__)


def _kept_by_earlier_release(base_path: Path) -> list[Path]:
    """Repositories in the tree that an earlier release filtered.

    One whose record cannot be read counts too: whether it is one is
    unknown, and deleting it cannot be undone.
    """
    kept = []
    for repo in repositories_beneath(base_path):
        try:
            if recorded_policy(repo).earlier_release:
                kept.append(repo)
        except PolicyReadError:
            kept.append(repo)
    return kept


def hold_back_earlier_releases(
    base_path: Path, projects: list[Project], github_org: str
) -> tuple[list[Project], list[MirrorResult]]:
    """Split off the projects ``--overwrite`` must leave entirely alone.

    One whose directory is, or holds, a repository an earlier release
    filtered -- or one whose record cannot be read -- is taken out of the
    batch: not deleted, cloned, filtered, planned or pushed, so that
    ``--recreate`` neither replaces its GitHub repository nor pushes it.

    Returns:
        The projects to mirror, and a ``SKIPPED`` result for each one held
        back, saying why.
    """
    kept = _kept_by_earlier_release(base_path) if base_path.is_dir() else []
    to_mirror: list[Project] = []
    held: list[MirrorResult] = []
    for project in projects:
        resolved = (base_path / project.name).resolve()
        if not any(repo == resolved or resolved in repo.parents for repo in kept):
            to_mirror.append(project)
            continue
        message = (
            f"Not overwritten or pushed: {project.name} holds a repository an "
            f"earlier gerrit-clone release content-filtered without recording "
            f"its filters, so neither cloning it again nor publishing it can "
            f"keep them. Delete it and clone it again with the filters it needs"
        )
        logger.warning(message)
        name = transform_gerrit_name_to_github(project.name)
        now = datetime.now(UTC)
        held.append(
            MirrorResult(
                project=project,
                github_name=name,
                github_url=f"https://github.com/{github_org}/{name}",
                status=MirrorStatus.SKIPPED,
                local_path=base_path / project.name,
                error_message=message,
                started_at=now,
                completed_at=now,
            )
        )
    return to_mirror, held


def remove_paths(
    paths_to_remove: list[tuple[str, Path]], rmtree: Callable[[Path], None]
) -> None:
    """Delete each collected clone directory, reporting how many went."""
    removed_count = 0
    failed_removals: list[tuple[str, str]] = []
    for project_name, path in paths_to_remove:
        try:
            if path.is_dir():
                rmtree(path)
                removed_count += 1
                logger.debug(f"Removed {path}")
            elif path.exists():
                path.unlink()
                removed_count += 1
                logger.debug(f"Removed file {path}")
        except OSError as e:
            failed_removals.append((project_name, str(e)))
            logger.warning(f"Failed to remove {path}: {e}")
    log_cleanup_outcome(removed_count, failed_removals)


def collect_paths_to_remove(
    base_path: Path,
    projects: list[Project],
) -> list[tuple[str, Path]]:
    """Collect existing clone directories, deepest paths first.

    Args:
        base_path: Root directory holding the local clones
        projects: Projects whose directories should be removed

    Returns:
        ``(project_name, path)`` pairs ordered so children precede
        parents; empty when there is nothing to remove.
    """
    paths_to_remove: list[tuple[str, Path]] = []
    for project in projects:
        project_path = base_path / project.name
        if project_path.exists():
            paths_to_remove.append((project.name, project_path))

    if not paths_to_remove:
        logger.info("No existing directories to clean up")
        return []

    logger.info(f"Removing {len(paths_to_remove)} existing directories...")

    # Remove in reverse dependency order (children before parents)
    # Sort by path depth (deepest first) to avoid removing parents
    # before children
    paths_to_remove.sort(key=lambda x: x[1].as_posix().count("/"), reverse=True)
    return paths_to_remove


def log_cleanup_outcome(
    removed_count: int,
    failed_removals: list[tuple[str, str]],
) -> None:
    """Report how many clone directories were removed."""
    if failed_removals:
        logger.warning(
            f"Successfully removed {removed_count} directories, "
            f"failed to remove {len(failed_removals)}"
        )
    else:
        logger.info(f"Successfully removed {removed_count} directories")
