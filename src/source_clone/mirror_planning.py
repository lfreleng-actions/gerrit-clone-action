# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""Planning and execution of the GitHub repository mutations.

Decides which repositories must be deleted, created or reused for a
mirror batch, validates the pre-fetched GraphQL view of the target
organisation, and runs the batched delete/create phases under a shared
rate limiter.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from source_clone.github_api import GitHubRepo, transform_gerrit_name_to_github
from source_clone.logging import get_logger
from source_clone.rate_limit import TokenBucketLimiter

if TYPE_CHECKING:
    from source_clone.github_api import GitHubAPI

logger = get_logger(__name__)


@dataclass
class GitHubMirrorPlan:
    """Repository mutations required to mirror a batch of clones."""

    repos_to_delete: list[str] = field(default_factory=list)
    repos_to_create: list[dict[str, Any]] = field(default_factory=list)
    repos_lookup: dict[str, GitHubRepo] = field(default_factory=dict)


def _rest_fallback(github_api: GitHubAPI, github_org: str) -> dict[str, dict[str, Any]]:
    """Re-fetch the organisation's repositories through the REST API."""
    rest_repos = github_api.list_repos(org=github_org)
    fallback: dict[str, dict[str, Any]] = {}
    for repo in rest_repos:
        fallback[repo.name] = {
            "name": repo.name,
            "full_name": repo.full_name,
            "html_url": repo.html_url,
            "clone_url": repo.clone_url,
            "ssh_url": repo.ssh_url,
            "private": repo.private,
            "description": repo.description,
            "default_branch": repo.default_branch,
            "latest_commit_sha": None,
            "last_commit_date": None,
        }
    logger.info(
        "REST API fallback found %d existing repos",
        len(fallback),
    )
    return fallback


def validate_graphql_results(
    github_api: GitHubAPI,
    github_org: str,
    existing_repos: dict[str, dict[str, Any]],
    successful_clones: int,
) -> dict[str, dict[str, Any]]:
    """Validate GraphQL results and fall back to REST if suspect.

    If the GraphQL query returned zero repos for an org that
    should have repos (based on the number of successful clones
    and the recreate flag), something went wrong — typically a
    transient 502.  Rather than proceeding to mass-create repos
    that already exist (burning secondary rate-limit budget), we
    fall back to the paginated REST API.

    Args:
        github_api: GitHub API client used for the REST fallback.
        github_org: Target GitHub organisation or user.
        existing_repos: Result from ``list_all_repos_graphql``.
        successful_clones: Number of successfully cloned projects.

    Returns:
        Validated (possibly re-fetched) repo map.
    """
    if existing_repos:
        return existing_repos

    # GraphQL returned nothing but we have cloned projects; this is
    # suspicious regardless of recreate mode and warrants a REST
    # fallback to avoid unnecessary repo creation attempts.
    if successful_clones > 0:
        logger.warning(
            "⚠️  GraphQL returned 0 existing repos but we have "
            "%d successful clones.  Falling back to REST API "
            "to avoid unnecessary repo creation attempts.",
            successful_clones,
        )
        try:
            return _rest_fallback(github_api, github_org)
        except Exception as exc:
            logger.error("REST API fallback also failed: %s", exc)
            # Both GraphQL and REST failed — proceeding with an
            # empty existence set would recreate the original
            # cascade failure (mass POST → 422 → rate-limit
            # exhaustion).  Raise so the caller can abort.
            raise RuntimeError(
                "Cannot determine existing GitHub repos: "
                "both GraphQL and REST API failed.  Aborting "
                "mirror to avoid mass-creation of duplicates."
            ) from exc

    return existing_repos


def _create_config(clone_result: Any, github_name: str) -> dict[str, Any]:
    """Build the repository creation payload for a cloned project."""
    return {
        "name": github_name,
        "description": clone_result.project.description
        or f"Mirror of Gerrit project {clone_result.project.name}",
        "private": False,
    }


def plan_github_operations(
    clone_results: list[Any],
    existing_repos: dict[str, dict[str, Any]],
    recreate: bool,
) -> GitHubMirrorPlan:
    """Work out which GitHub repositories to delete, create or reuse.

    Args:
        clone_results: Results from the clone phase
        existing_repos: Pre-fetched GitHub repo data
        recreate: Whether existing repositories should be recreated

    Returns:
        The planned mutations plus the reusable repository objects.
    """
    logger.info("📋 Planning GitHub operations...")
    plan = GitHubMirrorPlan()

    for clone_result in clone_results:
        if not clone_result.success:
            continue

        github_name = transform_gerrit_name_to_github(clone_result.project.name)
        exists = github_name in existing_repos

        if exists and recreate:
            plan.repos_to_delete.append(github_name)
            plan.repos_to_create.append(_create_config(clone_result, github_name))
        elif not exists:
            plan.repos_to_create.append(_create_config(clone_result, github_name))
        else:
            # Exists and not recreating - create GitHubRepo from existing data
            repo_data = existing_repos[github_name]
            plan.repos_lookup[github_name] = GitHubRepo(
                name=repo_data["name"],
                full_name=repo_data["full_name"],
                html_url=repo_data["html_url"],
                clone_url=repo_data["clone_url"],
                ssh_url=repo_data["ssh_url"],
                private=repo_data["private"],
                description=repo_data.get("description"),
                default_branch=repo_data.get("default_branch"),
            )

    logger.info(
        f"Plan: Delete {len(plan.repos_to_delete)}, "
        f"Create {len(plan.repos_to_create)}, "
        f"Reuse {len(plan.repos_lookup)}"
    )
    return plan


def build_shared_limiter(total_mutations: int) -> TokenBucketLimiter:
    """Create the limiter shared by the delete and create phases.

    A single limiter means rate-limit state (including the reduced rate
    applied after a 403 response) persists across the delete → create
    transition.
    """
    if total_mutations > 200:
        base_rate = 0.25  # 0.25 tokens/s ~ 1 mutation req per 8s (2 tokens each)
    elif total_mutations > 100:
        base_rate = 0.33  # 0.33 tokens/s ~ 1 mutation req per 6s
    else:
        base_rate = 0.5  # 0.5 tokens/s ~ 1 mutation req per 4s

    return TokenBucketLimiter(
        rate=base_rate,
        burst=max(2, min(5, total_mutations // 30)),
        min_rate=0.02,
        recovery_seconds=120.0,
    )


def _run_delete_phase(
    github_api: GitHubAPI,
    github_org: str,
    plan: GitHubMirrorPlan,
    shared_limiter: TokenBucketLimiter,
) -> None:
    """Batch delete repositories and prune failures from the create list."""
    logger.info(f"🗑️  Batch deleting {len(plan.repos_to_delete)} repositories...")
    delete_results = asyncio.run(
        github_api.batch_delete_repos(
            github_org,
            plan.repos_to_delete,
            max_concurrent=5,
            shared_limiter=shared_limiter,
        )
    )
    failed_deletes = [
        name for name, (success, _) in delete_results.items() if not success
    ]
    if failed_deletes:
        logger.error(
            f"❌ Failed to delete {len(failed_deletes)} repos: {failed_deletes[:10]}"
        )
        # Remove failed deletes from create list to avoid 422 errors
        plan.repos_to_create = [
            cfg for cfg in plan.repos_to_create if cfg["name"] not in failed_deletes
        ]
        logger.info(
            f"Adjusted create list: {len(plan.repos_to_create)} repos "
            "(excluded failed deletes)"
        )
    else:
        logger.info(f"✓ All {len(plan.repos_to_delete)} repos deleted successfully")

    # The shared_limiter already carries reduced rate from
    # any 403s encountered during deletes.  Its time-based
    # recovery will gradually restore throughput during the
    # create phase.  No fixed cooldown needed — the token
    # bucket handles pacing automatically.


def _run_create_phase(
    github_api: GitHubAPI,
    github_org: str,
    plan: GitHubMirrorPlan,
    shared_limiter: TokenBucketLimiter,
) -> None:
    """Batch create repositories and record the results in the lookup."""
    logger.info(f"🏗️  Batch creating {len(plan.repos_to_create)} repositories...")
    create_results = asyncio.run(
        github_api.batch_create_repos(
            github_org,
            plan.repos_to_create,
            max_concurrent=3,
            shared_limiter=shared_limiter,
        )
    )
    for name, (repo, error) in create_results.items():
        if repo:
            plan.repos_lookup[name] = repo
            logger.debug(f"Added {name} to lookup")
        else:
            logger.error(f"❌ Failed to create {name}: {error}")


def execute_repo_mutations(
    github_api: GitHubAPI,
    github_org: str,
    plan: GitHubMirrorPlan,
) -> None:
    """Run the delete and create phases described by *plan*.

    The plan is updated in place: repositories created here are added to
    ``repos_lookup``, and repositories whose delete failed are dropped
    from ``repos_to_create``.
    """
    total_mutations = len(plan.repos_to_delete) + len(plan.repos_to_create)
    shared_limiter = build_shared_limiter(total_mutations)

    if plan.repos_to_delete:
        _run_delete_phase(github_api, github_org, plan, shared_limiter)

    if plan.repos_to_create:
        _run_create_phase(github_api, github_org, plan, shared_limiter)
