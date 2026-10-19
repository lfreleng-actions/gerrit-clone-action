# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""Post-push configuration of a GitHub repository's default branch.

Derives the default branch of a freshly mirrored repository from the
local bare clone's HEAD and applies it via the GitHub API.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from source_clone.logging import get_logger
from source_clone.mirror_git_refs import pick_default_branch

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from source_clone.github_api import GitHubRepo
    from source_clone.mirror_git_refs import GitRefInspector

logger = get_logger(__name__)

_HEADS_PREFIX = "refs/heads/"


def _branch_from_head(
    local_path: Path,
    inspector: GitRefInspector,
) -> str | None:
    """Resolve the HEAD branch of *local_path*, if HEAD names a branch."""
    try:
        branch = inspector.current_branch(local_path)
    except (FileNotFoundError, ValueError):
        branch = None

    if branch:
        return branch

    # Fall back to reading HEAD directly for bare repos where
    # ``git symbolic-ref`` might fail in unusual layouts.
    head_ref = inspector.head_ref(local_path)
    if head_ref and head_ref.startswith(_HEADS_PREFIX):
        return head_ref[len(_HEADS_PREFIX) :]
    return None


def _fallback_branch(
    local_path: Path,
    github_repo: GitHubRepo,
    inspector: GitRefInspector,
) -> str | None:
    """Pick a default branch when HEAD does not point at a branch.

    Returns ``None`` when no default branch can or should be set; the
    reason is logged here.
    """
    if inspector.is_parent_project(local_path):
        # Gerrit parent project — no branches at all.  This is
        # expected and not an error; log at INFO so operators can
        # distinguish it from genuinely broken repos.
        logger.info(
            "Gerrit parent project %s (HEAD → refs/meta/config, "
            "no branches) — skipping default branch configuration",
            github_repo.full_name,
        )
        return None

    # HEAD points to a non-branch ref (e.g. refs/meta/config)
    # but the repo *does* have branches.  Pick the best candidate.
    branches = inspector.list_branches(local_path)
    if not branches:
        # No HEAD branch and no refs/heads/* at all, but also
        # not a recognised parent project.  Log and move on.
        logger.info(
            "Repository %s has no branches under refs/heads/; "
            "cannot set a default branch on GitHub",
            github_repo.full_name,
        )
        return None

    branch = pick_default_branch(branches)
    head_ref = inspector.head_ref(local_path)
    logger.info(
        "HEAD for %s points to %s (not a branch); "
        "falling back to '%s' as default branch",
        github_repo.full_name,
        head_ref or "unknown ref",
        branch,
    )
    return branch


def set_default_branch_from_local(
    local_path: Path,
    github_repo: GitHubRepo,
    inspector: GitRefInspector,
    set_default_branch: Callable[[str, str, str], bool],
) -> None:
    """Detect the local clone's HEAD branch and set it as GitHub default.

    For bare clones (created by ``git clone --mirror``), this reads the
    symbolic ref that HEAD points to — which mirrors the Gerrit
    project's HEAD configuration.  The branch name is then set as
    the default branch on the GitHub repository via the API.

    **Gerrit parent projects** (HEAD → ``refs/meta/config`` with no
    ``refs/heads/*`` branches) are detected and logged at INFO level
    rather than treated as errors.  These are organisational
    containers in the Gerrit hierarchy and will always appear empty
    on GitHub.

    When HEAD points to a non-branch ref (e.g. ``refs/meta/config``)
    but the repository *does* contain ``refs/heads/*`` branches, the
    function falls back to the first available branch (preferring
    ``master`` or ``main``).

    This is a best-effort operation; failures are logged but do not
    cause the mirror to be marked as failed.

    Args:
        local_path: Local (bare) clone path
        github_repo: Target GitHub repository that was just pushed to
        inspector: Git ref helpers used to read the local clone
        set_default_branch: GitHub API call taking owner, repo name and
            branch name
    """
    branch = _branch_from_head(local_path, inspector)
    if not branch:
        branch = _fallback_branch(local_path, github_repo, inspector)
    if not branch:
        return

    # Skip the API call when GitHub already has the correct default
    # branch.  On a routine resync every repo would otherwise incur
    # a redundant PATCH request, wasting REST API rate-limit budget.
    if github_repo.default_branch == branch:
        logger.debug(
            "Default branch for %s already set to '%s'; skipping redundant API call",
            github_repo.full_name,
            branch,
        )
        return

    owner = github_repo.full_name.split("/")[0]
    set_default_branch(owner, github_repo.name, branch)
