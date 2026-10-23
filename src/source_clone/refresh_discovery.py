# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""Local repository discovery and project filtering for bulk refreshes.

Answers the question "which repositories should this refresh touch?" — walking
a directory tree for Git repositories, working copies and bare mirrors alike,
and then applying the include/exclude project patterns. Kept separate from
:mod:`source_clone.refresh_manager` because selection is purely a filesystem
and pattern-matching concern with no bearing on how repositories are then
refreshed.
"""

from __future__ import annotations

import os
from pathlib import Path

from source_clone.logging import get_logger
from source_clone.models import match_project_pattern, normalize_project_list

logger = get_logger(__name__)

#: Directories git itself keeps at the top of a git directory.  A bare
#: repository has no ``.git`` to step over: its git directory is the
#: repository, so these are what the walk must not descend into.
#: ``modules`` matters most, holding whole git directories of its own
#: that would otherwise be reported as repositories.  Any other
#: subdirectory can be a nested project -- ``ccsdk/apps`` is cloned
#: inside the ``ccsdk`` mirror -- and is searched as usual, as is one
#: of these names that is a repository in its own right.
_GIT_DIR_INTERNALS = frozenset(
    {
        "branches",
        "filter-repo",
        "hooks",
        "info",
        "lfs",
        "logs",
        "modules",
        "objects",
        "refs",
        "rr-cache",
        "worktrees",
    }
)


def _is_bare_repository(dirs: list[str], files: list[str]) -> bool:
    """Whether a directory, as listed by :func:`os.walk`, is a bare repository.

    Recognised by the layout git gives every git directory -- ``HEAD``
    and ``config`` files beside ``objects`` and ``refs`` directories --
    rather than by asking git, which would mean a subprocess for every
    directory in the tree.
    """
    return {"HEAD", "config"} <= set(files) and {"objects", "refs"} <= set(dirs)


def _is_repository(path: Path) -> bool:
    """Whether *path* is itself a repository, working copy or bare.

    Asked only of the few directories that share a name with one of
    git's own, so a nested project called ``logs`` or ``modules`` is
    still searched while git's directories of that name are not.
    """
    return (path / ".git").is_dir() or (
        (path / "HEAD").is_file()
        and (path / "config").is_file()
        and (path / "objects").is_dir()
        and (path / "refs").is_dir()
    )


class RepositoryDiscoveryMixin:
    """Discovery of local Git repositories, with include/exclude filtering."""

    # Supplied by RefreshManager.__init__; declared here because this layer
    # reads them.
    recursive: bool
    include_projects: list[str] | None
    exclude_projects: list[str] | None

    def discover_local_repositories(self, base_path: Path) -> list[Path]:
        """Discover all Git repositories under base_path.

        Both working copies and bare repositories are found.  Clones are
        bare mirrors by default, and a mirror has no ``.git`` directory
        to recognise it by.

        Args:
            base_path: Base directory to search

        Returns:
            Sorted list of repository root paths (sorted alphabetically for
            deterministic processing order and consistent progress display)
        """
        if not base_path.exists():
            raise ValueError(f"Base path does not exist: {base_path}")

        if not base_path.is_dir():
            raise ValueError(f"Base path is not a directory: {base_path}")

        logger.debug(f"🔍 Discovering Git repositories in {base_path}")

        repositories: list[Path] = []
        visited_repos: set[Path] = set()

        # Walk directory tree
        for root, dirs, files in os.walk(base_path):
            root_path = Path(root)

            # A working copy keeps its git directory in .git; a bare
            # repository is its own git directory.
            bare = _is_bare_repository(dirs, files)
            if bare or (".git" in dirs and (root_path / ".git").is_dir()):
                # Normalize path
                repo_path = root_path.resolve()

                # Skip if we've already visited this repo
                if repo_path in visited_repos:
                    continue

                repositories.append(repo_path)
                visited_repos.add(repo_path)

                logger.debug(f"Found repository: {repo_path.name}")

                if self.recursive:
                    # Continue searching subdirectories for Gerrit hierarchical projects
                    # In Gerrit, projects like ccsdk/apps, ccsdk/features are separate
                    # independent repos, not nested submodules within ccsdk
                    # We only skip the git directory itself: .git in a
                    # working copy, git's own directories in a bare one,
                    # and hidden ones, as everywhere else -- no Gerrit
                    # project path has a hidden component, and a
                    # refresh stages filtered mirrors in one.
                    if bare:
                        dirs[:] = [
                            d
                            for d in dirs
                            if not d.startswith(".")
                            and (
                                d not in _GIT_DIR_INTERNALS
                                or _is_repository(root_path / d)
                            )
                        ]
                    else:
                        dirs[:] = [d for d in dirs if d != ".git"]
                else:
                    # Non-recursive mode: don't descend into subdirectories
                    dirs[:] = []
                continue

            # Skip hidden directories (except .git which we already handled)
            dirs[:] = [d for d in dirs if not d.startswith(".")]

        logger.debug(f"📂 Discovered {len(repositories)} Git repositories")

        # Sort repositories alphabetically for:
        # 1. Deterministic processing order across runs
        # 2. Better progress tracking (alphabetical display)
        # 3. Easier debugging and log analysis
        sorted_repos = sorted(repositories)

        # Apply include/exclude project filtering using relative paths
        # from base_path as project names (matching Gerrit's hierarchical
        # naming convention, e.g. "testsuite/pythonsdk-tests").
        if self.include_projects or self.exclude_projects:
            return self._filter_by_project_patterns(base_path, sorted_repos)

        return sorted_repos

    def _filter_by_project_patterns(
        self, base_path: Path, sorted_repos: list[Path]
    ) -> list[Path]:
        """Apply the include/exclude project patterns to discovered repos.

        Args:
            base_path: Base directory the repositories were discovered under
            sorted_repos: Discovered repository paths, in processing order

        Returns:
            The repositories that survive the include and exclude filters,
            in the same order.
        """
        include_pats = (
            normalize_project_list(self.include_projects)
            if self.include_projects
            else []
        )
        exclude_pats = (
            normalize_project_list(self.exclude_projects)
            if self.exclude_projects
            else []
        )

        before_count = len(sorted_repos)
        filtered: list[Path] = []
        base_resolved = base_path.resolve()
        for repo_path in sorted_repos:
            project_name = project_name_for(repo_path, base_resolved)

            # Apply include filter (if specified, only keep matches)
            if include_pats and not any(
                match_project_pattern(project_name, p) for p in include_pats
            ):
                continue

            # Apply exclude filter
            if exclude_pats and any(
                match_project_pattern(project_name, p) for p in exclude_pats
            ):
                continue

            filtered.append(repo_path)

        after_count = len(filtered)
        filter_desc: list[str] = []
        if include_pats:
            filter_desc.append(f"include={sorted(include_pats)}")
        if exclude_pats:
            filter_desc.append(f"exclude={sorted(exclude_pats)}")
        logger.debug(
            f"Project filter: kept {after_count}/{before_count} repositories "
            f"({', '.join(filter_desc)})"
        )
        return filtered


def project_name_for(repo_path: Path, base_resolved: Path) -> str:
    """Derive a Gerrit-style project name from a repository path.

    Uses the path relative to the base directory, with ``as_posix()`` for
    consistent forward-slash separators matching Gerrit's hierarchical
    naming convention.

    Args:
        repo_path: Repository path
        base_resolved: Resolved base directory

    Returns:
        Project name to match project patterns against -- the
        include/exclude filters here, and ``--git-filter`` after a
        refresh
    """
    try:
        rel = repo_path.relative_to(base_resolved)
    except ValueError:
        # Fallback: use just the directory name
        return repo_path.name

    if rel == Path():
        # repo is exactly at base_path; use directory name so filters
        # can match it.
        return repo_path.name
    return rel.as_posix()
