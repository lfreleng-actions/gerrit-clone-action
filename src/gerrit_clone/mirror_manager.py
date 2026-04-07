# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2025 Matthew Watkins <mwatkins@linuxfoundation.org>

"""Manager for mirroring Gerrit repositories to GitHub."""

from __future__ import annotations

import asyncio
import base64
import os
import shutil
import subprocess
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import Enum
from typing import TYPE_CHECKING, Any
from urllib.parse import urlparse

from gerrit_clone.clone_manager import CloneManager
from gerrit_clone.content_filter import apply_content_filters
from gerrit_clone.git_utils import (
    get_current_branch,
    get_head_ref,
    is_gerrit_parent_project,
    list_local_branches,
)
from gerrit_clone.github_api import (
    GitHubAPI,
    GitHubRepo,
    transform_gerrit_name_to_github,
)
from gerrit_clone.logging import get_logger
from gerrit_clone.models import CloneStatus, Config, Project, filter_projects
from gerrit_clone.rate_limit import TokenBucketLimiter

if TYPE_CHECKING:
    from pathlib import Path

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
            "started_at": self.started_at.isoformat() if self.started_at else None,
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
            "generated_at": (self.completed_at or datetime.now(UTC)).isoformat(),
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
        github_token: str | None = None,
        set_default_branch: bool = True,
        fix_default_branch: bool = True,
        remove_file_patterns: list[str] | None = None,
        git_filter_projects: dict[str, list[str]] | None = None,
        redact_secrets: bool = False,
    ) -> None:
        """Initialize mirror manager.

        Args:
            config: Gerrit configuration
            github_api: GitHub API client
            github_org: Target GitHub organization or user
            recreate: Delete and recreate existing GitHub repositories
            overwrite: Overwrite local repositories
            progress_tracker: Optional progress tracker
            github_token: GitHub token for HTTPS push authentication.
                If provided, push operations will use HTTPS with token
                auth instead of SSH. This avoids requiring SSH keys
                for github.com in CI environments.
            set_default_branch: Set the default branch on GitHub after push
                (default: True). When enabled, the local HEAD symbolic ref
                is read from the bare clone and used to configure the
                default branch on the GitHub repository via the API.
            fix_default_branch: Repair GitHub repos that have no default
                branch configured (default: True).  During the post-push
                phase, any existing GitHub repository whose
                ``defaultBranchRef`` is ``null`` will be inspected.  If
                the local clone has ``refs/heads/*`` branches, the best
                candidate is set as the GitHub default.  Gerrit parent
                projects (HEAD → ``refs/meta/config``, no branches) are
                skipped with an informational message.
            remove_file_patterns: Optional list of file glob patterns to
                remove from all cloned repositories before pushing to
                GitHub (e.g. ``["*.jar", "*.bin"]``).
            git_filter_projects: Optional mapping of project names to
                lists of token strings for ``git filter-repo`` replacement.
                Only the specified projects are filtered.
            redact_secrets: When ``True``, automatically scan repository
                content for well-known credential patterns and replace
                them with safe placeholder values.
        """
        self.config = config
        self.github_api = github_api
        self.github_org = github_org
        self.recreate = recreate
        self.overwrite = overwrite
        self.progress_tracker = progress_tracker
        self.github_token = github_token
        self.set_default_branch = set_default_branch
        self.fix_default_branch = fix_default_branch
        self.remove_file_patterns = remove_file_patterns
        self.git_filter_projects = git_filter_projects
        self.redact_secrets = redact_secrets
        self.clone_manager = CloneManager(config, progress_tracker)

    def _build_push_url(self, github_repo: GitHubRepo) -> str:
        """Build the push URL for a GitHub repository.

        When a github_token is available, returns the plain HTTPS clone
        URL (no credentials embedded).  Authentication is handled
        separately via environment variables in :meth:`_push_to_github`
        so that secrets never appear on the command line or in process
        listings.

        Falls back to the SSH URL when no token is available.

        Args:
            github_repo: Target GitHub repository

        Returns:
            Push URL string (plain HTTPS or SSH)

        Raises:
            ValueError: If ``clone_url`` is not HTTPS when a token is set.
        """
        if self.github_token:
            # Validate the clone URL scheme via urllib.parse
            parsed = urlparse(github_repo.clone_url)
            if parsed.scheme != "https":
                raise ValueError(
                    f"Expected HTTPS clone URL for token auth, "
                    f"got scheme '{parsed.scheme}' in: "
                    f"{github_repo.clone_url}"
                )
            # Return the plain HTTPS URL — credentials are passed via
            # GIT_CONFIG_* environment variables in _push_to_github().
            logger.debug(f"Using HTTPS token auth for push to {github_repo.full_name}")
            return github_repo.clone_url
        else:
            # Fall back to SSH URL
            logger.debug(f"Using SSH for push to {github_repo.full_name}")
            return github_repo.ssh_url

    def _sanitize_token(self, text: str) -> str:
        """Remove the github_token from *text* if present.

        This must be applied to **all** git output (stdout *and* stderr)
        before logging or returning it, because git can include the
        credentialed URL in either stream.
        """
        if self.github_token and self.github_token in text:
            return text.replace(self.github_token, "***")
        return text

    def _push_to_github(
        self, local_path: Path, github_repo: GitHubRepo
    ) -> tuple[bool, str | None]:
        """Push repository to GitHub.

        Uses HTTPS with token authentication when a github_token is
        available (preferred in CI), otherwise falls back to SSH.

        Credentials are passed via ``GIT_CONFIG_COUNT`` /
        ``GIT_CONFIG_KEY_*`` / ``GIT_CONFIG_VALUE_*`` environment
        variables so the token never appears on the command line or
        in ``/proc`` process listings.

        Args:
            local_path: Local repository path
            github_repo: Target GitHub repository

        Returns:
            Tuple of (success, error_message)
        """
        push_url = self._build_push_url(github_repo)

        # Log the URL without exposing the token
        if self.github_token:
            logger.debug(f"Pushing to GitHub (HTTPS): {github_repo.clone_url}")
        else:
            logger.debug(f"Pushing to GitHub (SSH): {push_url}")

        # Build git push command — no secrets on the command line
        cmd = ["git", "-C", str(local_path), "push", "--mirror", push_url]

        try:
            env: dict[str, str] = {}
            if self.github_token:
                # Pass credentials via GIT_CONFIG_* env vars (Git 2.31+).
                # This avoids embedding the token in the URL *and* keeps
                # it off the command line / process listing.
                credentials = base64.b64encode(
                    f"x-access-token:{self.github_token}".encode()
                ).decode()
                env["GIT_CONFIG_COUNT"] = "1"
                env["GIT_CONFIG_KEY_0"] = "http.extraheader"
                env["GIT_CONFIG_VALUE_0"] = f"AUTHORIZATION: basic {credentials}"
            elif self.config.git_ssh_command:
                # Only set GIT_SSH_COMMAND when using SSH push
                env["GIT_SSH_COMMAND"] = self.config.git_ssh_command

            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=self.config.clone_timeout,
                env={**os.environ, **env} if env else None,
                check=True,
            )

            # Sanitize both stdout and stderr before any logging
            stdout = self._sanitize_token(result.stdout or "")
            stderr = self._sanitize_token(result.stderr or "")

            # Summarise push output; stderr can list every ref pushed
            # which is extremely verbose for repos with many branches.
            stderr_lines = stderr.strip().splitlines()
            ref_count = sum(
                1
                for line in stderr_lines
                if line.strip().startswith("*") or "->" in line
            )
            if ref_count:
                logger.debug(
                    "Push successful to %s (%d refs)",
                    github_repo.full_name,
                    ref_count,
                )
            else:
                logger.debug(
                    "Push successful to %s (up to date)",
                    github_repo.full_name,
                )
            if stdout.strip():
                logger.debug(
                    "Push stdout for %s: %s",
                    github_repo.full_name,
                    stdout.strip(),
                )

            # After a successful mirror push, set the default branch on
            # GitHub to match the source project's HEAD.  ``git push
            # --mirror`` pushes refs/heads/* but GitHub sometimes picks an
            # arbitrary branch as the default; explicitly setting it
            # ensures the GitHub repo matches the Gerrit source.
            if self.set_default_branch:
                self._set_default_branch_from_local(local_path, github_repo)

            return True, None

        except subprocess.TimeoutExpired:
            error = f"Push timeout after {self.config.clone_timeout}s"
            logger.error(f"Push failed to {github_repo.full_name}: {error}")
            return False, error
        except subprocess.CalledProcessError as e:
            # Sanitize both stdout and stderr to avoid leaking tokens
            stdout = self._sanitize_token(e.stdout or "")
            stderr = self._sanitize_token(e.stderr or "")
            if stdout.strip():
                error = f"Git push failed: {stderr} | stdout: {stdout}"
            else:
                error = f"Git push failed: {stderr}"
            logger.error(f"Push failed to {github_repo.full_name}: {error}")
            return False, error
        except Exception as e:
            error = f"Unexpected error: {self._sanitize_token(str(e))}"
            logger.error(f"Push failed to {github_repo.full_name}: {error}")
            return False, error

    def _set_default_branch_from_local(
        self, local_path: Path, github_repo: GitHubRepo
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
        method falls back to the first available branch (preferring
        ``master`` or ``main``).

        This is a best-effort operation; failures are logged but do not
        cause the mirror to be marked as failed.

        Args:
            local_path: Local (bare) clone path
            github_repo: Target GitHub repository that was just pushed to
        """
        # --- Step 1: try the fast path (HEAD is a normal branch) ----------
        try:
            branch = get_current_branch(local_path)
        except (FileNotFoundError, ValueError):
            branch = None

        if not branch:
            # Fall back to reading HEAD directly for bare repos where
            # ``git symbolic-ref`` might fail in unusual layouts.
            head_ref = get_head_ref(local_path)
            if head_ref and head_ref.startswith("refs/heads/"):
                branch = head_ref[len("refs/heads/") :]

        # --- Step 2: if HEAD isn't a branch, classify and try fallback ----
        if not branch:
            if is_gerrit_parent_project(local_path):
                # Gerrit parent project — no branches at all.  This is
                # expected and not an error; log at INFO so operators can
                # distinguish it from genuinely broken repos.
                logger.info(
                    "Gerrit parent project %s (HEAD → refs/meta/config, "
                    "no branches) — skipping default branch configuration",
                    github_repo.full_name,
                )
                return

            # HEAD points to a non-branch ref (e.g. refs/meta/config)
            # but the repo *does* have branches.  Pick the best candidate.
            branches = list_local_branches(local_path)
            if branches:
                # Prefer well-known defaults, then fall back to first
                for candidate in ("master", "main", "develop"):
                    if candidate in branches:
                        branch = candidate
                        break
                if not branch:
                    branch = branches[0]

                head_ref = get_head_ref(local_path)
                logger.info(
                    "HEAD for %s points to %s (not a branch); "
                    "falling back to '%s' as default branch",
                    github_repo.full_name,
                    head_ref or "unknown ref",
                    branch,
                )
            else:
                # No HEAD branch and no refs/heads/* at all, but also
                # not a recognised parent project.  Log and move on.
                logger.info(
                    "Repository %s has no branches under refs/heads/; "
                    "cannot set a default branch on GitHub",
                    github_repo.full_name,
                )
                return

        # --- Step 3: apply the default branch on GitHub -------------------
        # Skip the API call when GitHub already has the correct default
        # branch.  On a routine resync every repo would otherwise incur
        # a redundant PATCH request, wasting REST API rate-limit budget.
        if github_repo.default_branch == branch:
            logger.debug(
                "Default branch for %s already set to '%s'; "
                "skipping redundant API call",
                github_repo.full_name,
                branch,
            )
            return

        owner = github_repo.full_name.split("/")[0]
        self.github_api.set_default_branch(owner, github_repo.name, branch)

    def _validate_graphql_results(
        self,
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
                rest_repos = self.github_api.list_repos(org=self.github_org)
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

    def mirror_projects(self, projects: list[Project]) -> list[MirrorResult]:
        """Mirror projects from Gerrit to GitHub.

        This method reuses the existing CloneManager infrastructure for
        cloning from Gerrit, which handles parent/child dependencies and
        prevents race conditions. Then it pushes to GitHub.

        Optimizations:
        - Uses GraphQL to fetch all existing GitHub repos in one query
        - Validates GraphQL results and falls back to REST on failure
        - Shares a single TokenBucketLimiter across delete and create
          phases so rate-limit state is preserved
        - Pre-flight rate-limit budget check before batch operations
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

        # Step 0b: Pre-flight rate-limit budget check (synchronous)
        logger.info("📊 Checking rate-limit budget before batch operations...")
        self.github_api.budget.preflight_check_sync(self.github_api.client)

        # Step 1: Clone from Gerrit using existing CloneManager
        # This handles all the dependency ordering and safe parallel operations
        logger.info("📥 Cloning repositories from Gerrit...")
        clone_results = self.clone_manager.clone_projects(projects)

        successful_clones = sum(1 for cr in clone_results if cr.success)

        # Step 1b: Apply content filters to cloned repositories
        filter_failed_projects: set[str] = set()
        if self.remove_file_patterns or self.git_filter_projects or self.redact_secrets:
            logger.info("🔧 Applying content filters to cloned repositories...")
            filter_success = filter_fail = 0
            for cr in clone_results:
                if not cr.success or not cr.path:
                    continue
                success, error = apply_content_filters(
                    cr.path,
                    cr.project.name,
                    remove_patterns=self.remove_file_patterns,
                    git_filter_projects=self.git_filter_projects,
                    redact_secrets=self.redact_secrets,
                )
                if success:
                    filter_success += 1
                else:
                    filter_fail += 1
                    filter_failed_projects.add(cr.project.name)
                    logger.warning(
                        "Content filter failed for %s: %s",
                        cr.project.name,
                        error,
                    )
            logger.info(
                "Content filtering complete: %d succeeded, %d failed",
                filter_success,
                filter_fail,
            )

        # Abort the batch if any content filters failed — silently
        # dropping projects would make them disappear from the manifest.
        if filter_failed_projects:
            raise RuntimeError(
                f"Content filtering failed for {filter_fail} project(s), "
                f"aborting batch: {sorted(filter_failed_projects)}"
            )

        # Step 2: Batch fetch existing GitHub repos (GraphQL with retry)
        logger.info("🔍 Fetching existing GitHub repositories (GraphQL)...")
        existing_repos = self.github_api.list_all_repos_graphql(self.github_org)

        # Validate: if GraphQL returned nothing suspicious, try REST
        existing_repos = self._validate_graphql_results(
            existing_repos, successful_clones
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

            github_name = transform_gerrit_name_to_github(clone_result.project.name)
            exists = github_name in existing_repos

            if exists and self.recreate:
                repos_to_delete.append(github_name)
                repos_to_create.append(
                    {
                        "name": github_name,
                        "description": clone_result.project.description
                        or f"Mirror of Gerrit project {clone_result.project.name}",
                        "private": False,
                    }
                )
            elif not exists:
                repos_to_create.append(
                    {
                        "name": github_name,
                        "description": clone_result.project.description
                        or f"Mirror of Gerrit project {clone_result.project.name}",
                        "private": False,
                    }
                )
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
                    default_branch=repo_data.get("default_branch"),
                )

        logger.info(
            f"Plan: Delete {len(repos_to_delete)}, "
            f"Create {len(repos_to_create)}, "
            f"Reuse {len(repos_lookup)}"
        )

        # Step 4: Execute batch operations
        # Create a shared TokenBucketLimiter so rate-limit state
        # (including reduced rate from 403 responses) persists
        # across the delete → create transition.
        total_mutations = len(repos_to_delete) + len(repos_to_create)
        if total_mutations > 200:
            base_rate = 0.25  # 0.25 tokens/s ~ 1 mutation req per 8s (2 tokens each)
        elif total_mutations > 100:
            base_rate = 0.33  # 0.33 tokens/s ~ 1 mutation req per 6s
        else:
            base_rate = 0.5  # 0.5 tokens/s ~ 1 mutation req per 4s

        shared_limiter = TokenBucketLimiter(
            rate=base_rate,
            burst=max(2, min(5, total_mutations // 30)),
            min_rate=0.02,
            recovery_seconds=120.0,
        )

        if repos_to_delete:
            logger.info(f"🗑️  Batch deleting {len(repos_to_delete)} repositories...")
            delete_results = asyncio.run(
                self.github_api.batch_delete_repos(
                    self.github_org,
                    repos_to_delete,
                    max_concurrent=5,
                    shared_limiter=shared_limiter,
                )
            )
            failed_deletes = [
                name for name, (success, _) in delete_results.items() if not success
            ]
            if failed_deletes:
                logger.error(
                    f"❌ Failed to delete {len(failed_deletes)} repos: "
                    f"{failed_deletes[:10]}"
                )
                # Remove failed deletes from create list to avoid 422 errors
                repos_to_create = [
                    cfg for cfg in repos_to_create if cfg["name"] not in failed_deletes
                ]
                logger.info(
                    f"Adjusted create list: {len(repos_to_create)} repos "
                    "(excluded failed deletes)"
                )
            else:
                logger.info(f"✓ All {len(repos_to_delete)} repos deleted successfully")

            # The shared_limiter already carries reduced rate from
            # any 403s encountered during deletes.  Its time-based
            # recovery will gradually restore throughput during the
            # create phase.  No fixed cooldown needed — the token
            # bucket handles pacing automatically.

        if repos_to_create:
            logger.info(f"🏗️  Batch creating {len(repos_to_create)} repositories...")
            create_results = asyncio.run(
                self.github_api.batch_create_repos(
                    self.github_org,
                    repos_to_create,
                    max_concurrent=3,
                    shared_limiter=shared_limiter,
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
        push_success = 0
        push_failed = 0
        push_skipped = 0
        report_every = max(1, len(clone_results) // 10)

        for clone_result in clone_results:
            mirror_result = self._push_to_github_from_clone_result_optimized(
                clone_result, existing_repos, repos_lookup
            )
            mirror_results.append(mirror_result)

            # Track and report per-item status with clear icons
            idx = len(mirror_results)
            if mirror_result.status == MirrorStatus.SUCCESS:
                push_success += 1
                logger.info(
                    "✅ [%d/%d] Pushed %s -> %s (%.1fs)",
                    idx,
                    len(projects),
                    mirror_result.project.name,
                    mirror_result.github_name,
                    mirror_result.duration_seconds,
                )
            elif mirror_result.status == MirrorStatus.SKIPPED:
                push_skipped += 1
                logger.info(
                    "⏭️  [%d/%d] Skipped %s",
                    idx,
                    len(projects),
                    mirror_result.project.name,
                )
            else:
                push_failed += 1
                logger.warning(
                    "❌ [%d/%d] Failed %s: %s",
                    idx,
                    len(projects),
                    mirror_result.project.name,
                    mirror_result.error_message or "unknown error",
                )

            # Periodic summary so long-running batches show aggregate progress
            if idx % report_every == 0 and idx < len(projects):
                logger.info(
                    "📊 Push progress: %d/%d completed "
                    "(%d succeeded, %d failed, %d skipped)",
                    idx,
                    len(projects),
                    push_success,
                    push_failed,
                    push_skipped,
                )

        logger.info(
            "📊 Push complete: %d/%d succeeded, %d failed, %d skipped",
            push_success,
            len(projects),
            push_failed,
            push_skipped,
        )

        # Step 6: Repair pass — fix repos with no default branch
        if self.fix_default_branch:
            self._fix_default_branches(
                clone_results,
                existing_repos,
                repos_lookup,
                mirror_results,
            )

        return mirror_results

    def _fix_default_branches(
        self,
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
            clone_results: Results from the clone phase (used to locate
                local paths)
            existing_repos: Pre-fetched GitHub repo data from GraphQL
            repos_lookup: Map of GitHub repo names to GitHubRepo objects
            mirror_results: Results from the push phase.  Repos whose
                push failed are excluded from the repair pass to avoid
                compounding errors on empty repositories.
        """
        # Identify repos that have no default branch in the GraphQL data
        repos_needing_fix: list[str] = [
            name
            for name, data in existing_repos.items()
            if data.get("default_branch") is None
        ]

        if not repos_needing_fix:
            return

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

        if not repos_needing_fix:
            return

        logger.info(
            "🔧 Default branch repair: checking %d repositories "
            "with no default branch configured",
            len(repos_needing_fix),
        )

        # Build a lookup from GitHub name → local clone path
        clone_path_lookup: dict[str, Path] = {}
        for cr in clone_results:
            if cr.success and cr.path:
                gh_name = transform_gerrit_name_to_github(cr.project.name)
                clone_path_lookup[gh_name] = cr.path

        parent_count = 0
        fixed_count = 0
        skip_push_failed_count = len(repos_to_skip) if mirror_results else 0
        no_clone_count = 0
        no_branches_count = 0

        for github_name in repos_needing_fix:
            local_path = clone_path_lookup.get(github_name)
            if not local_path or not local_path.exists():
                logger.debug(
                    "No local clone for %s/%s; cannot repair default branch",
                    self.github_org,
                    github_name,
                )
                no_clone_count += 1
                continue

            # Check for Gerrit parent project
            if is_gerrit_parent_project(local_path):
                logger.info(
                    "ℹ️  %s/%s is a Gerrit parent project "  # noqa: RUF001
                    "(HEAD → refs/meta/config, no branches) — "
                    "no default branch to set",
                    self.github_org,
                    github_name,
                )
                parent_count += 1
                continue

            # Try to find a suitable branch
            branches = list_local_branches(local_path)
            if not branches:
                logger.info(
                    "ℹ️  %s/%s has no branches under refs/heads/; "  # noqa: RUF001
                    "cannot set a default branch",
                    self.github_org,
                    github_name,
                )
                no_branches_count += 1
                continue

            # Pick best candidate branch
            branch: str | None = None
            for candidate in ("master", "main", "develop"):
                if candidate in branches:
                    branch = candidate
                    break
            if not branch:
                branch = branches[0]

            # Get the GitHubRepo object to call the API
            github_repo = repos_lookup.get(github_name)
            if not github_repo:
                logger.debug(
                    "No GitHubRepo object for %s; skipping repair",
                    github_name,
                )
                continue

            head_ref = get_head_ref(local_path)
            logger.info(
                "🔧 Fixing default branch for %s/%s: "
                "HEAD is %s, setting default to '%s'",
                self.github_org,
                github_name,
                head_ref or "unknown",
                branch,
            )
            owner = github_repo.full_name.split("/")[0]
            success = self.github_api.set_default_branch(
                owner, github_repo.name, branch
            )
            if success:
                fixed_count += 1

        # Summary
        parts: list[str] = []
        if skip_push_failed_count:
            parts.append(f"{skip_push_failed_count} skipped (push failed, repo empty)")
        if parent_count:
            parts.append(
                f"{parent_count} Gerrit parent project(s) (expected, no action needed)"
            )
        if fixed_count:
            parts.append(f"{fixed_count} repaired")
        if no_clone_count:
            parts.append(f"{no_clone_count} skipped (no local clone)")
        if no_branches_count:
            parts.append(f"{no_branches_count} skipped (no branches)")

        if parts:
            logger.info(
                "🔧 Default branch repair complete: %s",
                "; ".join(parts),
            )

    def _push_to_github_from_clone_result_optimized(
        self,
        clone_result: Any,
        existing_repos: dict[str, dict[str, Any]],  # noqa: ARG002
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
        github_name = transform_gerrit_name_to_github(clone_result.project.name)
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
        if clone_result.status == CloneStatus.ALREADY_EXISTS and not self.recreate:
            logger.info(
                f"Repository already exists: {clone_result.project.name}, "
                f"skipping GitHub push (use --recreate to update)"
            )
            completed_at = datetime.now(UTC)
            duration = (completed_at - started_at).total_seconds()
            github_url = f"https://github.com/{self.github_org}/{github_name}"
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
            # Get GitHub repo from lookup (was created/reused in batch)
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
            push_success, push_error = self._push_to_github(local_path, github_repo)
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
            logger.error(f"Mirror failed for {clone_result.project.name}: {e}")
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
