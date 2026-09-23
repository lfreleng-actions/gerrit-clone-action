# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""Nested (parent/child) repository support for clone operations.

Gerrit hierarchies routinely contain projects such as ``foo`` and ``foo/bar``,
which map onto a git working tree nested inside another git working tree.  This
module owns the two concerns that arise from that:

* locating the git ancestor a project would be nested under, and
* keeping the parent repository's ``.git/info/exclude`` up to date so the child
  does not show up as untracked content in its parent.

Batch-level bookkeeping for how much nesting was detected stays with the
caller: :class:`~gerrit_clone.clone_orchestrator.CloneManager` owns the sets
and :func:`~gerrit_clone.clone_ordering.log_nested_summary` reports them.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

from gerrit_clone.logging import get_logger
from gerrit_clone.models import CloneStatus
from gerrit_clone.subprocess_tracking import ProcessAbandonedError, unless_abandoned

if TYPE_CHECKING:
    from pathlib import Path, PurePath

    from gerrit_clone.models import CloneResult, Config

logger = get_logger(__name__)


def find_git_ancestor(path: Path) -> Path | None:
    """Find the nearest enclosing git working tree, ignoring project membership.

    Args:
        path: Path whose ancestors should be searched

    Returns:
        The closest ancestor directory containing a ``.git`` directory, or None
    """
    cur = path.parent
    while cur != cur.parent:
        if (cur / ".git").is_dir():
            return cur
        cur = cur.parent
    return None


def find_project_git_ancestor(
    path: Path, base_path: Path, project_index: set[str]
) -> Path | None:
    """Find the cloned project that *path* would be nested under.

    Only treats a directory as a parent if it is itself a cloned project (its
    path relative to the workspace root appears in the project index) and we are
    not crossing above the configured path prefix.  This avoids misreporting an
    unrelated git repository higher up the filesystem as the parent.

    Args:
        path: Target path of the project being cloned
        base_path: Workspace root that clones are written beneath
        project_index: Names of every project in this run

    Returns:
        Ancestor project directory, or None if the project is top-level
    """
    try:
        base = base_path.resolve()
        current = path.parent.resolve()
    except OSError:
        return None

    while True:
        if current == base:
            # Do not treat the workspace root as a project ancestor
            return None
        try:
            rel = current.relative_to(base)
        except ValueError:
            # Stepped outside base
            return None
        rel_str = rel.as_posix()
        if rel_str in project_index and (current / ".git").is_dir():
            return current
        if current == current.parent:
            return None
        current = current.parent


def redetect_project_git_ancestor(
    project_name: str, path: Path, base_path: Path, project_index: set[str]
) -> tuple[Path, str] | None:
    """Re-run ancestor detection immediately before the clone subprocess starts.

    A parent repository may finish cloning after this worker began, so the
    lookup is repeated late.  Detection failures are deliberately non-fatal: the
    project is simply treated as a top-level clone.

    Args:
        project_name: Project being cloned (for logging)
        path: Target path of the project being cloned
        base_path: Workspace root that clones are written beneath
        project_index: Names of every project in this run

    Returns:
        Tuple of (ancestor directory, its path relative to the workspace root),
        or None if no ancestor could be determined
    """
    try:
        base = base_path.resolve()
        ancestor = find_project_git_ancestor(path, base_path, project_index)
        if ancestor is None:
            return None
        return ancestor, ancestor.relative_to(base).as_posix()
    except Exception as late_e:
        logger.debug(f"Late ancestor re-check failed for {project_name}: {late_e}")
        return None


def _read_exclude_lines(exclude_file: Path) -> list[str]:
    """Read the parent repository's exclude file, tolerating decode errors.

    Args:
        exclude_file: Path to ``.git/info/exclude``

    Returns:
        Existing lines, or an empty list if the file does not exist yet
    """
    if exclude_file.exists():
        return exclude_file.read_text(encoding="utf-8", errors="ignore").splitlines()
    return []


# Exclude patterns are compared and written in POSIX form.  ``str()`` on a
# WindowsPath renders backslashes, which git reads as escapes rather than
# separators: the rule would not match, the child would still show as
# untracked content in its parent, and -- because the membership test has
# the same defect -- a duplicate entry would be appended every run.
def exclude_pattern(path: PurePath, ancestor_repo: PurePath) -> str:
    """Render *path* relative to *ancestor_repo* as a git exclude pattern.

    Args:
        path: Target path of the nested project
        ancestor_repo: Parent repository directory

    Returns:
        The child's path relative to the parent, in POSIX form

    Raises:
        ValueError: If *path* is not below *ancestor_repo*
    """
    return path.relative_to(ancestor_repo).as_posix()


def _append_exclude(
    exclude_file: Path, marker: str, rel_child: str, project_name: str
) -> None:
    """Append *rel_child* to the parent's exclude file, or refuse to.

    Written atomically with the abandonment check: this changes a
    repository the batch does not own, so a batch that has given up
    must not, and one giving up mid-write must find it finished.
    """
    with unless_abandoned(f"Nested protection for {project_name}"):
        exclude_file.parent.mkdir(parents=True, exist_ok=True)
        with exclude_file.open("a", encoding="utf-8") as ef:
            ef.write(f"\n# {marker}\n{rel_child}\n")


def apply_nested_protection(
    ancestor_repo: Path, path: Path, project_name: str, nested_under: str | None
) -> None:
    """Exclude a nested child from its parent repository's working tree.

    Args:
        ancestor_repo: Parent repository directory
        path: Target path of the nested project
        project_name: Nested project being cloned (for logging)
        nested_under: Parent path recorded on the result (for logging)

    Raises:
        ProcessAbandonedError: If the batch has been abandoned.
    """
    try:
        rel_child = exclude_pattern(path, ancestor_repo)
        exclude_file = ancestor_repo / ".git" / "info" / "exclude"
        existing_lines = _read_exclude_lines(exclude_file)
        if rel_child not in existing_lines:
            _append_exclude(
                exclude_file,
                "auto-added to ignore nested repo",
                rel_child,
                project_name,
            )
            logger.debug(
                f"Added nested protection exclude entry for {project_name} under {nested_under}"
            )
        else:
            logger.debug(
                f"Nested protection exclude already present for {project_name}"
            )
    except ProcessAbandonedError:
        raise
    except Exception as e:
        logger.warning(f"Could not apply nested protection for {project_name}: {e}")


def apply_late_nested_protection(
    ancestor_repo: Path, path: Path, project_name: str, nested_under: str | None
) -> None:
    """Apply nested protection for an ancestor discovered late.

    Kept separate from :func:`apply_nested_protection` because the late path
    writes a different marker comment, emits a different log message and
    downgrades failures to debug: by this point the clone is about to start and
    a missing exclude entry is cosmetic rather than a reason to warn.

    Args:
        ancestor_repo: Parent repository directory
        path: Target path of the nested project
        project_name: Nested project being cloned (for logging)
        nested_under: Parent path recorded on the result (for logging)

    Raises:
        ProcessAbandonedError: If the batch has been abandoned.
    """
    try:
        exclude_file = ancestor_repo / ".git" / "info" / "exclude"
        rel_child = exclude_pattern(path, ancestor_repo)
        existing_lines = _read_exclude_lines(exclude_file)
        if rel_child not in existing_lines:
            _append_exclude(
                exclude_file,
                "auto-added (late) to ignore nested repo",
                rel_child,
                project_name,
            )
            logger.debug(
                f"🧬 Nested repo detected late: {project_name} (parent={nested_under})"
            )
    except ProcessAbandonedError:
        raise
    except Exception as ne:
        logger.debug(
            f"Late nested protection application failed for {project_name}: {ne}"
        )


def reject_nested_clone(
    result: CloneResult, ancestor_repo: Path, started_at: datetime
) -> CloneResult:
    """Fail a clone that would nest inside another repository.

    Args:
        result: Result to update
        ancestor_repo: Ancestor repository that blocks the clone
        started_at: Time the clone attempt began

    Returns:
        The failed result
    """
    # Treat nesting as failure if not allowed
    result.status = CloneStatus.FAILED
    result.error_message = (
        f"Nested clone forbidden (ancestor git repo at {ancestor_repo.name})"
    )
    result.completed_at = datetime.now(UTC)
    result.duration_seconds = (result.completed_at - started_at).total_seconds()
    logger.error(
        f"❌ Nested clone blocked for {result.project.name} (ancestor={ancestor_repo.name}, allow_nested_git=False)"
    )
    return result


def annotate_nested_parent(
    result: CloneResult, ancestor_repo: Path, base_path: Path, project_name: str
) -> None:
    """Record the parent that a nested clone will live under.

    Args:
        result: Result to annotate
        ancestor_repo: Ancestor repository directory
        base_path: Workspace root that clones are written beneath
        project_name: Project being cloned (for logging)
    """
    # Annotate intended nesting relationship early with relative path under base
    try:
        rel = ancestor_repo.relative_to(base_path)
        result.nested_under = rel.as_posix()
        logger.debug(
            f"🧬 Nested repo detected early: {project_name} (parent={result.nested_under})"
        )
    except Exception:
        # Fallback to directory name
        result.nested_under = ancestor_repo.name
        logger.debug(
            f"🧬 Nested repo detected early (fallback): {project_name} (parent={result.nested_under})"
        )


def recheck_nested_ancestor(
    config: Config,
    project_name: str,
    path: Path,
    project_index: set[str],
    result: CloneResult,
) -> bool:
    """Re-check for a parent repository that finished cloning meanwhile.

    Args:
        config: Configuration for clone operations
        project_name: Project about to be cloned
        path: Target path of the project being cloned
        project_index: Names of every project in this run
        result: Result to annotate if an ancestor is found

    Returns:
        True if a late re-check was actually performed
    """
    pre_late_nested = result.nested_under

    # Late ancestor re-check: parent may have finished cloning after initial
    # worker start
    if (
        not getattr(config, "allow_nested_git", False)
        or result.nested_under is not None
    ):
        return False

    detected = redetect_project_git_ancestor(
        project_name, path, config.path, project_index
    )
    if detected is not None:
        ancestor_repo, rel_str = detected
        result.nested_under = rel_str
        logger.debug(f"Late ancestor detection: {project_name} nested under {rel_str}")
        # Apply nested protection if enabled and not already applied
        if getattr(config, "nested_protection", False):
            apply_late_nested_protection(
                ancestor_repo, path, project_name, result.nested_under
            )

    if (
        pre_late_nested is None
        and result.nested_under is None
        and project_name.count("/") > 0
    ):
        logger.debug(
            f"No ancestor after late re-check: {project_name} (treat as top-level clone)"
        )
    return True
