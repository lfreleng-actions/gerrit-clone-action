# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2025 Matthew Watkins <mwatkins@linuxfoundation.org>

"""Manager for mirroring Gerrit repositories to GitHub."""

from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import Enum
from pathlib import Path
from typing import Any

from gerrit_clone.clone_manager import CloneManager
from gerrit_clone.github_api import (
    GitHubAPI,
    GitHubRepo,
    transform_gerrit_name_to_github,
)
from gerrit_clone.logging import get_logger
from gerrit_clone.models import CloneStatus, Config, Project
from gerrit_clone.progress import ProgressTracker

logger = get_logger(__name__)


class MirrorStatus(str, Enum):
    """Status values for mirror operations."""

    PENDING = "pending"
    CLONING = "cloning"
    PUSHING = "pushing"
    SUCCESS = "success"
    FAILED = "failed"
    SKIPPED = "skipped"
    ALREADY_EXISTS = "already_exists"


@dataclass
class MirrorResult:
    """Result of mirroring a single repository."""

    project: Project
    github_name: str
    github_url: str
    status: str
    local_path: Path
    duration_seconds: float = 0.0
    error_message: str | None = None
    started_at: datetime | None = None
    completed_at: datetime | None = None
    attempts: int = 1

    @property
    def success(self) -> bool:
        """Check if mirror was successful."""
        return self.status in (MirrorStatus.SUCCESS, MirrorStatus.SKIPPED)

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary for JSON serialization."""
        return {
            "gerrit_project": self.project.name,
            "github_name": self.github_name,
            "github_url": self.github_url,
            "status": self.status,
            "local_path": str(self.local_path),
            "duration_s": round(self.duration_seconds, 3),
            "error": self.error_message,
            "started_at": self.started_at.isoformat()
            if self.started_at
            else None,
            "completed_at": self.completed_at.isoformat()
            if self.completed_at
            else None,
            "attempts": self.attempts,
        }


@dataclass
class MirrorBatchResult:
    """Results of a batch mirror operation."""

    results: list[MirrorResult]
    started_at: datetime
    completed_at: datetime | None = None
    github_org: str | None = None
    gerrit_host: str | None = None

    @property
    def total_count(self) -> int:
        """Total number of projects processed."""
        return len(self.results)

    @property
    def success_count(self) -> int:
        """Number of successful mirrors."""
        return sum(1 for r in self.results if r.success)

    @property
    def failed_count(self) -> int:
        """Number of failed mirrors."""
        return sum(1 for r in self.results if r.status == MirrorStatus.FAILED)

    @property
    def skipped_count(self) -> int:
        """Number of skipped mirrors."""
        return sum(1 for r in self.results if r.status == MirrorStatus.SKIPPED)

    @property
    def duration_seconds(self) -> float:
        """Total duration of batch operation."""
        if self.completed_at is None:
            return 0.0
        return (self.completed_at - self.started_at).total_seconds()

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary for JSON serialization."""
        return {
            "version": "1.0",
            "generated_at": (
                self.completed_at or datetime.now(UTC)
            ).isoformat(),
            "github_org": self.github_org,
            "gerrit_host": self.gerrit_host,
            "total": self.total_count,
            "succeeded": self.success_count,
            "failed": self.failed_count,
            "skipped": self.skipped_count,
            "duration_s": round(self.duration_seconds, 3),
            "results": [r.to_dict() for r in self.results],
        }


class MirrorManager:
    """Manages mirroring of Gerrit repositories to GitHub."""

    def __init__(
        self,
        config: Config,
        github_api: GitHubAPI,
        github_org: str,
        recreate: bool = False,
        overwrite: bool = False,
        progress_tracker: ProgressTracker | None = None,
    ) -> None:
        """Initialize mirror manager.

        Args:
            config: Gerrit configuration
            github_api: GitHub API client
            github_org: Target GitHub organization or user
            recreate: Delete and recreate existing GitHub repositories
            overwrite: Overwrite local repositories
            progress_tracker: Optional progress tracker
        """
        self.config = config
        self.github_api = github_api
        self.github_org = github_org
        self.recreate = recreate
        self.overwrite = overwrite
        self.progress_tracker = progress_tracker
        self.clone_manager = CloneManager(config, progress_tracker)

    def _push_to_github(
        self, local_path: Path, github_repo: GitHubRepo
    ) -> tuple[bool, str | None]:
        """Push repository to GitHub.

        Args:
            local_path: Local repository path
            github_repo: Target GitHub repository

        Returns:
            Tuple of (success, error_message)
        """
        # Use SSH URL for push
        push_url = github_repo.ssh_url

        logger.debug(f"Pushing to GitHub: {push_url}")

        # Build git push command
        cmd = ["git", "-C", str(local_path), "push", "--mirror", push_url]

        try:
            env = {}
            if self.config.git_ssh_command:
                env["GIT_SSH_COMMAND"] = self.config.git_ssh_command

            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=self.config.clone_timeout,
                env={**os.environ, **env} if env else None,
                check=True,
            )
            logger.debug(
                f"Push successful to {github_repo.full_name}. "
                f"stdout: {result.stdout}, stderr: {result.stderr}"
            )
            return True, None

        except subprocess.TimeoutExpired:
            error = f"Push timeout after {self.config.clone_timeout}s"
            logger.error(f"Push failed to {github_repo.full_name}: {error}")
            return False, error
        except subprocess.CalledProcessError as e:
            error = f"Git push failed: {e.stderr}"
            logger.error(f"Push failed to {github_repo.full_name}: {error}")
            return False, error
        except Exception as e:
            error = f"Unexpected error: {e}"
            logger.error(f"Push failed to {github_repo.full_name}: {error}")
            return False, error

    def mirror_projects(self, projects: list[Project]) -> list[MirrorResult]:
        """Mirror projects from Gerrit to GitHub.

        This method reuses the existing CloneManager infrastructure for
        cloning from Gerrit, which handles parent/child dependencies and
        prevents race conditions. Then it pushes to GitHub.

        Optimizations:
        - Uses GraphQL to fetch all existing GitHub repos in one query
        - Batch deletes repos in parallel (if recreate=True)
        - Batch creates repos in parallel
        - Push operations happen in parallel via CloneManager

        Args:
            projects: List of Gerrit projects to mirror

        Returns:
            List of MirrorResult instances
        """
        if not projects:
            logger.info("No projects to mirror")
            return []

        logger.info(f"Starting mirror of {len(projects)} projects")

        # Step 0: Clean up existing directories if overwrite is enabled
        if self.overwrite and self.config.path.exists():
            logger.info("🧹 Overwrite enabled - cleaning existing directories...")
            self._cleanup_existing_repos(projects)

        # Step 1: Clone from Gerrit using existing CloneManager
        # This handles all the dependency ordering and safe parallel operations
        logger.info("📥 Cloning repositories from Gerrit...")
        clone_results = self.clone_manager.clone_projects(projects)

        # Step 2: Batch fetch existing GitHub repos (GraphQL - one query!)
        logger.info("🔍 Fetching existing GitHub repositories (GraphQL)...")
        existing_repos = self.github_api.list_all_repos_graphql(
            self.github_org
        )
        logger.info(f"Found {len(existing_repos)} existing GitHub repositories")

        # Step 3: Plan operations (in-memory, instant)
        logger.info("📋 Planning GitHub operations...")
        repos_to_delete: list[str] = []
        repos_to_create: list[dict[str, Any]] = []
        repos_lookup: dict[str, GitHubRepo] = {}

        for clone_result in clone_results:
            if not clone_result.success:
                continue

            github_name = transform_gerrit_name_to_github(
                clone_result.project.name
            )
            exists = github_name in existing_repos

            if exists and self.recreate:
                repos_to_delete.append(github_name)
                repos_to_create.append({
                    "name": github_name,
                    "description": clone_result.project.description
                    or f"Mirror of Gerrit project {clone_result.project.name}",
                    "private": False,
                })
            elif not exists:
                repos_to_create.append({
                    "name": github_name,
                    "description": clone_result.project.description
                    or f"Mirror of Gerrit project {clone_result.project.name}",
                    "private": False,
                })
            else:
                # Exists and not recreating - create GitHubRepo from existing data
                repo_data = existing_repos[github_name]
                repos_lookup[github_name] = GitHubRepo(
                    name=repo_data["name"],
                    full_name=repo_data["full_name"],
                    html_url=repo_data["html_url"],
                    clone_url=repo_data["clone_url"],
                    ssh_url=repo_data["ssh_url"],
                    private=repo_data["private"],
                    description=repo_data.get("description"),
                )

        logger.info(
            f"Plan: Delete {len(repos_to_delete)}, "
            f"Create {len(repos_to_create)}, "
            f"Reuse {len(repos_lookup)}"
        )

        # Step 4: Execute batch operations
        if repos_to_delete:
            logger.info(f"🗑️  Batch deleting {len(repos_to_delete)} repositories...")
            delete_results = asyncio.run(
                self.github_api.batch_delete_repos(
                    self.github_org, repos_to_delete, max_concurrent=5
                )
            )
            failed_deletes = [
                name for name, (success, _) in delete_results.items()
                if not success
            ]
            if failed_deletes:
                logger.error(
                    f"❌ Failed to delete {len(failed_deletes)} repos: "
                    f"{failed_deletes[:10]}"
                )
                # Remove failed deletes from create list to avoid 422 errors
                repos_to_create = [
                    cfg for cfg in repos_to_create
                    if cfg["name"] not in failed_deletes
                ]
                logger.info(
                    f"Adjusted create list: {len(repos_to_create)} repos "
                    "(excluded failed deletes)"
                )
            else:
                logger.info(f"✓ All {len(repos_to_delete)} repos deleted successfully")

            # Wait a moment for GitHub to fully process deletes
            if repos_to_delete:
                logger.info("⏳ Waiting 2 seconds for deletes to complete...")
                time.sleep(2)

        if repos_to_create:
            logger.info(f"🏗️  Batch creating {len(repos_to_create)} repositories...")
            create_results = asyncio.run(
                self.github_api.batch_create_repos(
                    self.github_org, repos_to_create, max_concurrent=5
                )
            )
            for name, (repo, error) in create_results.items():
                if repo:
                    repos_lookup[name] = repo
                    logger.debug(f"Added {name} to lookup")
                else:
                    logger.error(f"❌ Failed to create {name}: {error}")

        # Step 5: Push to GitHub (can be parallelized further if needed)
        logger.info("📤 Pushing repositories to GitHub...")
        mirror_results: list[MirrorResult] = []

        for clone_result in clone_results:
            mirror_result = self._push_to_github_from_clone_result_optimized(
                clone_result, existing_repos, repos_lookup
            )
            mirror_results.append(mirror_result)

            logger.info(
                f"Completed {len(mirror_results)}/{len(projects)}: "
                f"{mirror_result.project.name} -> {mirror_result.status}"
            )

        return mirror_results

    def _push_to_github_from_clone_result_optimized(
        self,
        clone_result: Any,
        existing_repos: dict[str, dict[str, Any]],
        repos_lookup: dict[str, GitHubRepo],
    ) -> MirrorResult:
        """Convert a CloneResult to MirrorResult by pushing to GitHub.

        This optimized version uses pre-fetched data to avoid individual API calls.

        Args:
            clone_result: Result from CloneManager clone operation
            existing_repos: Map of existing repo names to their data
            repos_lookup: Map of repo names to GitHubRepo objects (created/reused)

        Returns:
            MirrorResult with GitHub push status
        """
        started_at = datetime.now(UTC)
        github_name = transform_gerrit_name_to_github(
            clone_result.project.name
        )
        local_path = clone_result.path

        # If clone failed, return failed mirror result
        if not clone_result.success:
            completed_at = datetime.now(UTC)
            duration = (completed_at - started_at).total_seconds()
            return MirrorResult(
                project=clone_result.project,
                github_name=github_name,
                github_url="",
                status=MirrorStatus.FAILED,
                local_path=local_path,
                duration_seconds=duration,
                error_message=f"Clone failed: {clone_result.error_message}",
                started_at=started_at,
                completed_at=completed_at,
            )

        # If clone was skipped, mark as skipped
        if clone_result.status == CloneStatus.ALREADY_EXISTS:
            if not self.recreate:
                logger.info(
                    f"Repository already exists: {clone_result.project.name}, "
                    f"skipping GitHub push (use --recreate to update)"
                )
                completed_at = datetime.now(UTC)
                duration = (completed_at - started_at).total_seconds()
                github_url = (
                    f"https://github.com/{self.github_org}/{github_name}"
                )
                return MirrorResult(
                    project=clone_result.project,
                    github_name=github_name,
                    github_url=github_url,
                    status=MirrorStatus.SKIPPED,
                    local_path=local_path,
                    duration_seconds=duration,
                    started_at=started_at,
                    completed_at=completed_at,
                )

        try:
            # Check if repo exists in our pre-fetched data
            existed_before = github_name in existing_repos

            if existed_before and not self.recreate:
                logger.info(
                    f"GitHub repository already exists: "
                    f"{self.github_org}/{github_name}, skipping"
                )
                github_url = (
                    f"https://github.com/{self.github_org}/{github_name}"
                )
                completed_at = datetime.now(UTC)
                duration = (completed_at - started_at).total_seconds()
                return MirrorResult(
                    project=clone_result.project,
                    github_name=github_name,
                    github_url=github_url,
                    status=MirrorStatus.SKIPPED,
                    local_path=local_path,
                    duration_seconds=duration,
                    started_at=started_at,
                    completed_at=completed_at,
                )

            # Get GitHub repo from lookup (was created/deleted in batch)
            github_repo = repos_lookup.get(github_name)
            if not github_repo:
                # This shouldn't happen, but handle gracefully
                error_msg = (
                    f"Repository {github_name} not found in lookup after "
                    "batch operations"
                )
                logger.error(error_msg)
                completed_at = datetime.now(UTC)
                duration = (completed_at - started_at).total_seconds()
                return MirrorResult(
                    project=clone_result.project,
                    github_name=github_name,
                    github_url="",
                    status=MirrorStatus.FAILED,
                    local_path=local_path,
                    duration_seconds=duration,
                    error_message=error_msg,
                    started_at=started_at,
                    completed_at=completed_at,
                )

            # Push to GitHub
            push_success, push_error = self._push_to_github(
                local_path, github_repo
            )
            if not push_success:
                completed_at = datetime.now(UTC)
                duration = (completed_at - started_at).total_seconds()
                return MirrorResult(
                    project=clone_result.project,
                    github_name=github_name,
                    github_url=github_repo.html_url,
                    status=MirrorStatus.FAILED,
                    local_path=local_path,
                    duration_seconds=duration,
                    error_message=f"Push failed: {push_error}",
                    started_at=started_at,
                    completed_at=completed_at,
                )

            completed_at = datetime.now(UTC)
            duration = (completed_at - started_at).total_seconds()
            return MirrorResult(
                project=clone_result.project,
                github_name=github_name,
                github_url=github_repo.html_url,
                status=MirrorStatus.SUCCESS,
                local_path=local_path,
                duration_seconds=duration,
                started_at=started_at,
                completed_at=completed_at,
            )

        except Exception as e:
            completed_at = datetime.now(UTC)
            duration = (completed_at - started_at).total_seconds()
            logger.error(
                f"Mirror failed for {clone_result.project.name}: {e}"
            )
            return MirrorResult(
                project=clone_result.project,
                github_name=github_name,
                github_url="",
                status=MirrorStatus.FAILED,
                local_path=local_path,
                duration_seconds=duration,
                error_message=str(e),
                started_at=started_at,
                completed_at=completed_at,
            )

    def _cleanup_existing_repos(self, projects: list[Project]) -> None:
        """Clean up existing repository directories when overwrite is enabled.

        Args:
            projects: List of projects whose directories should be removed
        """
        # Collect all paths that need to be removed
        paths_to_remove = []
        for project in projects:
            project_path = self.config.path / project.name
            if project_path.exists():
                paths_to_remove.append((project.name, project_path))

        if not paths_to_remove:
            logger.info("No existing directories to clean up")
            return

        logger.info(f"Removing {len(paths_to_remove)} existing directories...")

        # Remove in reverse dependency order (children before parents)
        # Sort by path depth (deepest first) to avoid removing parents
        # before children
        paths_to_remove.sort(key=lambda x: x[1].as_posix().count("/"), reverse=True)

        removed_count = 0
        failed_removals = []

        for project_name, path in paths_to_remove:
            try:
                if path.is_dir():
                    shutil.rmtree(path)
                    removed_count += 1
                    logger.debug(f"Removed {path}")
                elif path.exists():
                    path.unlink()
                    removed_count += 1
                    logger.debug(f"Removed file {path}")
            except OSError as e:
                failed_removals.append((project_name, str(e)))
                logger.warning(f"Failed to remove {path}: {e}")

        if failed_removals:
            logger.warning(
                f"Successfully removed {removed_count} directories, "
                f"failed to remove {len(failed_removals)}"
            )
        else:
            logger.info(f"Successfully removed {removed_count} directories")


def filter_projects_by_hierarchy(
    projects: list[Project], filter_names: list[str]
) -> list[Project]:
    """Filter projects based on hierarchical names.

    If a filter name like 'ccsdk' is provided, it matches:
    - Exact: 'ccsdk'
    - Children: 'ccsdk/apps', 'ccsdk/features', etc.

    Args:
        projects: List of all projects
        filter_names: List of project name prefixes to include

    Returns:
        Filtered list of projects
    """
    if not filter_names:
        return projects

    filtered: list[Project] = []
    for project in projects:
        for filter_name in filter_names:
            # Exact match
            if project.name == filter_name:
                filtered.append(project)
                break
            # Hierarchical match (must start with filter_name/)
            elif project.name.startswith(f"{filter_name}/"):
                filtered.append(project)
                break

    logger.info(
        f"Filtered {len(projects)} projects to {len(filtered)} "
        f"based on hierarchy filters: {filter_names}"
    )
    return filtered
