# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""Post-push repair of GitHub repositories with no default branch.

Inspects every existing GitHub repository whose ``defaultBranchRef``
was ``null`` and, where the local clone provides a usable candidate,
configures a default branch through the GitHub API.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from source_clone.github_api import transform_gerrit_name_to_github
from source_clone.logging import get_logger
from source_clone.mirror_git_refs import pick_default_branch
from source_clone.mirror_models import MirrorResult, MirrorStatus

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from source_clone.github_api import GitHubRepo
    from source_clone.mirror_git_refs import GitRefInspector

logger = get_logger(__name__)


@dataclass(frozen=True)
class BranchRepairContext:
    """Collaborators needed to repair default branches."""

    github_org: str
    inspector: GitRefInspector
    set_default_branch: Callable[[str, str, str], bool]


@dataclass
class _RepairCounts:
    """Tally of the outcomes of a repair pass."""

    push_failed: int = 0
    parent: int = 0
    fixed: int = 0
    no_clone: int = 0
    no_branches: int = 0


def _select_repos_needing_fix(
    existing_repos: dict[str, dict[str, Any]],
    mirror_results: list[MirrorResult] | None,
) -> tuple[list[str], set[str]]:
    """Determine which repos should be repaired.

    Returns the candidate repo names alongside the names that were
    excluded because their push failed.
    """
    repos_needing_fix: list[str] = [
        name
        for name, data in existing_repos.items()
        if data.get("default_branch") is None
    ]

    if not repos_needing_fix:
        return [], set()

    # Build a set of GitHub repo names whose push failed so we can
    # skip them.  Attempting to set the default branch on an empty
    # repo (where the push was rejected) always produces a 422 error
    # that just adds noise to the logs.
    push_failed_names: set[str] = set()
    if mirror_results:
        for mr in mirror_results:
            if mr.status == MirrorStatus.FAILED:
                push_failed_names.add(mr.github_name)

    # Exclude repos whose push failed — they are still empty on
    # GitHub so setting a default branch is impossible.
    repos_to_skip = push_failed_names & set(repos_needing_fix)
    if repos_to_skip:
        logger.info(
            "🔧 Skipping default branch repair for %d repo(s) whose "
            "push failed (repo is still empty on GitHub): %s",
            len(repos_to_skip),
            ", ".join(sorted(repos_to_skip)),
        )
        repos_needing_fix = [
            name for name in repos_needing_fix if name not in push_failed_names
        ]

    return repos_needing_fix, repos_to_skip


def _build_clone_path_lookup(clone_results: list[Any]) -> dict[str, Path]:
    """Map GitHub repo names to the local clone paths that back them."""
    clone_path_lookup: dict[str, Path] = {}
    for cr in clone_results:
        if cr.success and cr.path:
            gh_name = transform_gerrit_name_to_github(cr.project.name)
            clone_path_lookup[gh_name] = cr.path
    return clone_path_lookup


def _resolve_repair_branch(
    ctx: BranchRepairContext,
    github_name: str,
    local_path: Path,
    counts: _RepairCounts,
) -> str | None:
    """Choose a default branch for *github_name*, or ``None`` to skip."""
    if ctx.inspector.is_parent_project(local_path):
        logger.info(
            "ℹ️  %s/%s is a Gerrit parent project "  # noqa: RUF001
            "(HEAD → refs/meta/config, no branches) — "
            "no default branch to set",
            ctx.github_org,
            github_name,
        )
        counts.parent += 1
        return None

    # Try to find a suitable branch
    branches = ctx.inspector.list_branches(local_path)
    if not branches:
        logger.info(
            "ℹ️  %s/%s has no branches under refs/heads/; "  # noqa: RUF001
            "cannot set a default branch",
            ctx.github_org,
            github_name,
        )
        counts.no_branches += 1
        return None

    # Pick best candidate branch
    return pick_default_branch(branches)


def _repair_one(
    ctx: BranchRepairContext,
    github_name: str,
    github_repo: GitHubRepo,
    local_path: Path,
    branch: str,
) -> bool:
    """Set *branch* as the default branch for one repository."""
    head_ref = ctx.inspector.head_ref(local_path)
    logger.info(
        "🔧 Fixing default branch for %s/%s: HEAD is %s, setting default to '%s'",
        ctx.github_org,
        github_name,
        head_ref or "unknown",
        branch,
    )
    owner = github_repo.full_name.split("/")[0]
    return ctx.set_default_branch(owner, github_repo.name, branch)


def _log_repair_summary(counts: _RepairCounts) -> None:
    """Emit the aggregate outcome of a repair pass."""
    parts: list[str] = []
    if counts.push_failed:
        parts.append(f"{counts.push_failed} skipped (push failed, repo empty)")
    if counts.parent:
        parts.append(
            f"{counts.parent} Gerrit parent project(s) (expected, no action needed)"
        )
    if counts.fixed:
        parts.append(f"{counts.fixed} repaired")
    if counts.no_clone:
        parts.append(f"{counts.no_clone} skipped (no local clone)")
    if counts.no_branches:
        parts.append(f"{counts.no_branches} skipped (no branches)")

    if parts:
        logger.info(
            "🔧 Default branch repair complete: %s",
            "; ".join(parts),
        )


def fix_default_branches(
    ctx: BranchRepairContext,
    clone_results: list[Any],
    existing_repos: dict[str, dict[str, Any]],
    repos_lookup: dict[str, GitHubRepo],
    mirror_results: list[MirrorResult] | None = None,
) -> None:
    """Repair GitHub repositories that have no default branch configured.

    This post-push pass inspects every existing GitHub repo whose
    ``defaultBranchRef`` was ``null`` in the GraphQL fetch.  For each
    one it checks the corresponding local clone:

    * **Push failed** — skipped immediately.  If the push to GitHub
      was rejected (e.g. secret scanning, auth errors) the remote
      repo is still empty and setting a default branch is guaranteed
      to fail with a 422.  Logging a second error would only obscure
      the real problem.
    * **Gerrit parent project** (HEAD → ``refs/meta/config``, no
      ``refs/heads/*`` branches) — logged at INFO level and skipped.
      These are organisational containers in the Gerrit hierarchy and
      will always appear empty on GitHub; this is expected.
    * **Real repository with branches** — the best candidate branch
      is selected (preferring ``master``, ``main``, ``develop``) and
      set as the GitHub default via the API.
    * **No local clone available** — skipped with a debug message.

    Args:
        ctx: Target organisation and the collaborators used to inspect
            clones and update GitHub
        clone_results: Results from the clone phase (used to locate
            local paths)
        existing_repos: Pre-fetched GitHub repo data from GraphQL
        repos_lookup: Map of GitHub repo names to GitHubRepo objects
        mirror_results: Results from the push phase.  Repos whose
            push failed are excluded from the repair pass to avoid
            compounding errors on empty repositories.
    """
    repos_needing_fix, repos_to_skip = _select_repos_needing_fix(
        existing_repos, mirror_results
    )
    if not repos_needing_fix:
        return

    logger.info(
        "🔧 Default branch repair: checking %d repositories "
        "with no default branch configured",
        len(repos_needing_fix),
    )

    clone_path_lookup = _build_clone_path_lookup(clone_results)
    counts = _RepairCounts(
        push_failed=len(repos_to_skip) if mirror_results else 0,
    )

    for github_name in repos_needing_fix:
        local_path = clone_path_lookup.get(github_name)
        if not local_path or not local_path.exists():
            logger.debug(
                "No local clone for %s/%s; cannot repair default branch",
                ctx.github_org,
                github_name,
            )
            counts.no_clone += 1
            continue

        branch = _resolve_repair_branch(ctx, github_name, local_path, counts)
        if branch is None:
            continue

        github_repo = repos_lookup.get(github_name)
        if not github_repo:
            logger.debug(
                "No GitHubRepo object for %s; skipping repair",
                github_name,
            )
            continue

        if _repair_one(ctx, github_name, github_repo, local_path, branch):
            counts.fixed += 1

    _log_repair_summary(counts)
