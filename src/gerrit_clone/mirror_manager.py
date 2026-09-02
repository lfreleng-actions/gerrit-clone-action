# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2025 Matthew Watkins <mwatkins@linuxfoundation.org>

"""Manager for mirroring Gerrit repositories to GitHub.

Orchestrates the clone → filter → plan → push → repair pipeline and
delegates each phase to a dedicated ``mirror_*`` module.  The git ref
and content-filter helpers are imported here and bundled at call time,
keeping a single resolution point for them across the pipeline.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from typing import TYPE_CHECKING, Any

from gerrit_clone.clone_manager import CloneManager
from gerrit_clone.content_filter import apply_content_filters, is_shallow_repository
from gerrit_clone.git_utils import (
    get_current_branch,
    get_head_ref,
    is_gerrit_parent_project,
    list_local_branches,
)
from gerrit_clone.logging import get_logger
from gerrit_clone.mirror_branch_repair import BranchRepairContext, fix_default_branches
from gerrit_clone.mirror_cleanup import collect_paths_to_remove, log_cleanup_outcome
from gerrit_clone.mirror_content_filters import (
    ContentFilterRunner,
    ContentFilterSettings,
    apply_filters_to_clones,
)
from gerrit_clone.mirror_default_branch import set_default_branch_from_local
from gerrit_clone.mirror_filtering import filter_projects_by_hierarchy
from gerrit_clone.mirror_git_refs import GitRefInspector
from gerrit_clone.mirror_models import MirrorBatchResult, MirrorResult, MirrorStatus
from gerrit_clone.mirror_planning import (
    execute_repo_mutations,
    plan_github_operations,
    validate_graphql_results,
)
from gerrit_clone.mirror_push import (
    PushSettings,
    build_push_env,
    build_push_url,
    format_push_failure,
    log_push_success,
    sanitize_token,
)
from gerrit_clone.mirror_result_builder import (
    MirrorPushContext,
    build_mirror_result,
    run_push_phase,
)

if TYPE_CHECKING:
    from pathlib import Path

    from gerrit_clone.github_api import GitHubAPI, GitHubRepo
    from gerrit_clone.models import Config, Project
    from gerrit_clone.progress import ProgressTracker

logger = get_logger(__name__)

__all__ = [
    "MirrorBatchResult",
    "MirrorManager",
    "MirrorResult",
    "MirrorStatus",
    "filter_projects_by_hierarchy",
]


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

    def _push_settings(self) -> PushSettings:
        """Snapshot the push authentication settings for this manager."""
        return PushSettings(
            github_token=self.github_token,
            clone_timeout=self.config.clone_timeout,
            git_ssh_command=self.config.git_ssh_command,
        )

    def _git_ref_inspector(self) -> GitRefInspector:
        """Bundle the git ref helpers as bound in this module.

        Looked up per call so this module stays their definition point.
        """
        return GitRefInspector(
            current_branch=get_current_branch,
            head_ref=get_head_ref,
            is_parent_project=is_gerrit_parent_project,
            list_branches=list_local_branches,
        )

    def _content_filter_runner(self) -> ContentFilterRunner:
        """Bundle the content filter helpers as bound in this module."""
        return ContentFilterRunner(
            is_shallow=is_shallow_repository,
            apply_filters=apply_content_filters,
        )

    def _content_filter_settings(self) -> ContentFilterSettings:
        """Snapshot the content filtering options for this manager."""
        return ContentFilterSettings(
            remove_file_patterns=self.remove_file_patterns,
            git_filter_projects=self.git_filter_projects,
            redact_secrets=self.redact_secrets,
            clone_timeout=self.config.clone_timeout,
        )

    def _build_push_url(self, github_repo: GitHubRepo) -> str:
        """Build the push URL for *github_repo*.

        See :func:`gerrit_clone.mirror_push.build_push_url`.
        """
        return build_push_url(self._push_settings(), github_repo)

    def _sanitize_token(self, text: str) -> str:
        """Remove the github_token from *text* if present.

        See :func:`gerrit_clone.mirror_push.sanitize_token`.
        """
        return sanitize_token(self.github_token, text)

    def _push_to_github(
        self, local_path: Path, github_repo: GitHubRepo
    ) -> tuple[bool, str | None]:
        """Push repository to GitHub.

        Uses HTTPS with token authentication when a github_token is
        available (preferred in CI), otherwise falls back to SSH.

        Returns:
            Tuple of (success, error_message)
        """
        settings = self._push_settings()
        push_url = build_push_url(settings, github_repo)

        if self.github_token:
            logger.debug(f"Pushing to GitHub (HTTPS): {github_repo.clone_url}")
        else:
            logger.debug(f"Pushing to GitHub (SSH): {push_url}")

        cmd = ["git", "-C", str(local_path), "push", "--mirror", push_url]

        try:
            env = build_push_env(settings, push_url)
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=self.config.clone_timeout,
                env={**os.environ, **env} if env else None,
                check=True,
            )

            # Sanitize both stdout and stderr before any logging
            log_push_success(
                github_repo,
                self._sanitize_token(result.stdout or ""),
                self._sanitize_token(result.stderr or ""),
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
            error = format_push_failure(
                self._sanitize_token(e.stdout or ""),
                self._sanitize_token(e.stderr or ""),
            )
            logger.error(f"Push failed to {github_repo.full_name}: {error}")
            return False, error
        except Exception as e:
            error = f"Unexpected error: {self._sanitize_token(str(e))}"
            logger.error(f"Push failed to {github_repo.full_name}: {error}")
            return False, error

    def _set_default_branch_from_local(
        self, local_path: Path, github_repo: GitHubRepo
    ) -> None:
        """Set the GitHub default branch from the local clone's HEAD.

        Best-effort; failures are logged but do not fail the mirror.  See
        :func:`gerrit_clone.mirror_default_branch.set_default_branch_from_local`.
        """
        set_default_branch_from_local(
            local_path,
            github_repo,
            self._git_ref_inspector(),
            self.github_api.set_default_branch,
        )

    def _validate_graphql_results(
        self,
        existing_repos: dict[str, dict[str, Any]],
        successful_clones: int,
    ) -> dict[str, dict[str, Any]]:
        """Validate GraphQL results and fall back to REST if suspect."""
        return validate_graphql_results(
            self.github_api, self.github_org, existing_repos, successful_clones
        )

    def mirror_projects(self, projects: list[Project]) -> list[MirrorResult]:
        """Mirror projects from Gerrit to GitHub.

        This method reuses the existing CloneManager infrastructure for
        cloning from Gerrit, which handles parent/child dependencies and
        prevents race conditions.  The batch is then planned from a
        single GraphQL listing of the target organisation (validated
        against REST), repository deletes and creates are batched under
        one shared rate limiter, and only then are the clones pushed.

        Args:
            projects: List of Gerrit projects to mirror

        Returns:
            List of MirrorResult instances
        """
        if not projects:
            logger.info("No projects to mirror")
            return []

        logger.info(f"Starting mirror of {len(projects)} projects")

        if self.overwrite and self.config.path.exists():
            logger.info("🧹 Overwrite enabled - cleaning existing directories...")
            self._cleanup_existing_repos(projects)

        # Step 0b: Pre-flight rate-limit budget check (synchronous)
        logger.info("📊 Checking rate-limit budget before batch operations...")
        self.github_api.budget.preflight_check_sync(self.github_api.client)

        # This handles all the dependency ordering and safe parallel operations
        logger.info("📥 Cloning repositories from Gerrit...")
        clone_results = self.clone_manager.clone_projects(projects)

        successful_clones = sum(1 for cr in clone_results if cr.success)

        # Step 1b: Apply content filters to cloned repositories
        apply_filters_to_clones(
            clone_results,
            self._content_filter_settings(),
            self._content_filter_runner(),
        )

        logger.info("🔍 Fetching existing GitHub repositories (GraphQL)...")
        existing_repos = self.github_api.list_all_repos_graphql(self.github_org)

        # Validate: if GraphQL returned nothing suspicious, try REST
        existing_repos = self._validate_graphql_results(
            existing_repos, successful_clones
        )
        logger.info(f"Found {len(existing_repos)} existing GitHub repositories")

        plan = plan_github_operations(clone_results, existing_repos, self.recreate)
        execute_repo_mutations(self.github_api, self.github_org, plan)

        mirror_results = run_push_phase(
            clone_results,
            len(projects),
            lambda cr: self._push_to_github_from_clone_result_optimized(
                cr, existing_repos, plan.repos_lookup
            ),
        )

        if self.fix_default_branch:
            self._fix_default_branches(
                clone_results,
                existing_repos,
                plan.repos_lookup,
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

        Repos whose push failed are excluded from the repair pass to
        avoid compounding errors on empty repositories.  See
        :func:`gerrit_clone.mirror_branch_repair.fix_default_branches`.
        """
        fix_default_branches(
            BranchRepairContext(
                github_org=self.github_org,
                inspector=self._git_ref_inspector(),
                set_default_branch=self.github_api.set_default_branch,
            ),
            clone_results,
            existing_repos,
            repos_lookup,
            mirror_results,
        )

    def _push_to_github_from_clone_result_optimized(
        self,
        clone_result: Any,
        existing_repos: dict[str, dict[str, Any]],  # noqa: ARG002
        repos_lookup: dict[str, GitHubRepo],
    ) -> MirrorResult:
        """Convert a CloneResult to MirrorResult by pushing to GitHub.

        Uses the pre-fetched batch data rather than per-repo API calls.
        See :func:`gerrit_clone.mirror_result_builder.build_mirror_result`.
        """
        return build_mirror_result(
            clone_result,
            repos_lookup,
            MirrorPushContext(
                github_org=self.github_org,
                recreate=self.recreate,
                push=self._push_to_github,
            ),
        )

    def _cleanup_existing_repos(self, projects: list[Project]) -> None:
        """Clean up existing repository directories when overwrite is enabled.

        Args:
            projects: List of projects whose directories should be removed
        """
        paths_to_remove = collect_paths_to_remove(self.config.path, projects)
        if not paths_to_remove:
            return

        removed_count = 0
        failed_removals: list[tuple[str, str]] = []

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

        log_cleanup_outcome(removed_count, failed_removals)
