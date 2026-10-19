# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2025 Matthew Watkins <mwatkins@linuxfoundation.org>

"""Manager for GitHub organization reset operations using native github_api."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from rich.console import Console
from rich.live import Live
from rich.progress import (
    BarColumn,
    MofNCompleteColumn,
    Progress,
    SpinnerColumn,
    TextColumn,
    TimeElapsedColumn,
)

from source_clone.github_api import (
    GitHubAPI,
    GitHubAPIError,
    GitHubAuthError,
    GitHubNotFoundError,
    GitHubRateLimitError,
)
from source_clone.logging import get_logger
from source_clone.reset_deletion import ResetDeletionBase
from source_clone.reset_models import (
    GitHubRepoStatus,
    ResetResult,
    SyncComparison,
)

logger = get_logger(__name__)


class ResetManager(ResetDeletionBase):
    """Manager for GitHub organization reset operations."""

    def __init__(
        self,
        org: str,
        github_token: str,
        local_path: Path | None = None,
        console: Console | None = None,
        include_automation_prs: bool = False,
    ) -> None:
        """Initialize reset manager.

        Args:
            org: GitHub organization name
            github_token: GitHub personal access token
            local_path: Path to local Gerrit clone directory
            console: Rich console for output
            include_automation_prs: Include automation PRs in counts (default: False)
        """
        self.org = org
        self.github_token = github_token
        self.local_path = local_path or Path.cwd()
        self.console = console or Console()
        self.github_api = GitHubAPI(token=github_token)
        self.include_automation_prs = include_automation_prs

        # Known automation tool authors (based on dependamerge implementation)
        self.automation_authors = {
            "dependabot[bot]",
            "pre-commit-ci[bot]",
            "renovate[bot]",
            "github-actions[bot]",
            "allcontributors[bot]",
        }

    async def check_token_permissions(self) -> bool:
        """
        Check if GitHub token has required permissions.

        Returns:
            True if token has required permissions, False otherwise
        """
        self.console.print("🔍 Checking token permissions...")

        try:
            # Simple check - try to get authenticated user
            user_info = self.github_api.get_authenticated_user()
            username = user_info.get("login", "unknown")
            self.console.print(f"✅ Authenticated as: [cyan]{username}[/cyan]")
            return True
        except Exception as e:
            logger.error(f"Error checking token permissions: {e}")
            self.console.print(f"[red]❌ Error checking permissions: {e}[/red]")
            return False

    async def scan_github_organization(
        self, skip_pr_issue_counts: bool = False
    ) -> dict[str, GitHubRepoStatus]:
        """
        Scan GitHub organization and fetch repository information.

        Uses GraphQL to fetch repositories and basic metadata; per-repository
        REST API calls in `_fetch_repos_with_graphql` populate PR/issue counts
        unless `skip_pr_issue_counts` is True.

        Args:
            skip_pr_issue_counts: If True, skip fetching PR/issue counts
                (useful when confirmation is not needed)

        Returns:
            Dictionary mapping repository name to GitHubRepoStatus
        """
        self.console.print(f"📥 Scanning GitHub organization: [cyan]{self.org}[/cyan]")

        repos_status: dict[str, GitHubRepoStatus] = {}

        try:
            # Use enhanced GraphQL query
            repos_data = await self._fetch_repos_with_graphql(
                skip_pr_issue_counts=skip_pr_issue_counts
            )

            for name, repo in repos_data.items():
                repos_status[name] = GitHubRepoStatus(
                    name=repo["name"],
                    full_name=repo.get("full_name", f"{self.org}/{name}"),
                    url=repo.get("html_url", f"https://github.com/{self.org}/{name}"),
                    open_prs=repo.get("open_prs", 0),
                    open_issues=repo.get("open_issues", 0),
                    last_commit_sha=repo.get("latest_commit_sha"),
                    last_commit_date=repo.get("last_commit_date"),
                    default_branch=repo.get("default_branch") or "main",
                )

            self.console.print(
                f"✅ Fetched information on {len(repos_status)} repositories"
            )

        except Exception as e:
            logger.error(f"Error scanning organization: {e}")
            self.console.print(f"[red]❌ Error scanning organization: {e}[/red]")
            raise

        return repos_status

    async def _fetch_repos_with_graphql(
        self, skip_pr_issue_counts: bool = False
    ) -> dict[str, dict[str, Any]]:
        """Fetch repos with PR/issue counts using GraphQL with progress display.

        Args:
            skip_pr_issue_counts: If True, skip the per-repo REST API calls
                for PR/issue counts (significantly faster).
        """
        # Use the existing GraphQL method and enhance with PR/issue queries
        repos_map = self.github_api.list_all_repos_graphql(self.org)

        total_repos = len(repos_map)
        if total_repos == 0:
            return repos_map

        # When skipping PR/issue counts, return the repo map immediately
        # without making expensive per-repo REST API calls
        if skip_pr_issue_counts:
            return repos_map

        progress_bar = Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            BarColumn(bar_width=40),
            MofNCompleteColumn(),
            TextColumn("•"),
            TimeElapsedColumn(),
        )
        task = progress_bar.add_task("Fetching PR/Issue counts", total=total_repos)

        with Live(
            progress_bar, console=self.console, refresh_per_second=4, transient=True
        ):
            # Enhance with PR/issue counts using REST API
            for name, repo_data in repos_map.items():
                progress_bar.update(
                    task, description=f"Fetching PR/Issue counts ({name})"
                )

                try:
                    # Get all open PRs (with pagination)
                    all_prs_endpoint = f"/repos/{self.org}/{name}/pulls"
                    all_prs = self.github_api._request_paginated(
                        "GET", all_prs_endpoint, params={"state": "open"}
                    )

                    # Count total PRs for issue calculation
                    total_prs = len(all_prs)

                    # Filter automation PRs if needed for display
                    if self.include_automation_prs:
                        # Include all PRs
                        open_prs = len(all_prs)
                    else:
                        # Exclude automation PRs
                        open_prs = sum(
                            1
                            for pr in all_prs
                            if not self.is_automation_author(
                                (pr.get("user") or {}).get("login", "")
                            )
                        )

                    # Get issue count (with pagination)
                    # Note: GitHub's /issues endpoint returns both issues AND PRs
                    # We need to subtract the TOTAL PR count (not filtered) to get true issues
                    issues_endpoint = f"/repos/{self.org}/{name}/issues"
                    issues_response = self.github_api._request_paginated(
                        "GET", issues_endpoint, params={"state": "open"}
                    )
                    open_issues = len(issues_response)
                    # Subtract total PRs (including automation) from issues
                    open_issues = max(0, open_issues - total_prs)

                    repo_data["open_prs"] = open_prs
                    repo_data["open_issues"] = open_issues

                except GitHubNotFoundError:
                    # Repository might have been deleted between listing and fetching
                    logger.info(
                        f"Repository {name} not found, skipping PR/issue counts"
                    )
                    repo_data["open_prs"] = -1  # -1 indicates "unknown/unavailable"
                    repo_data["open_issues"] = -1
                except GitHubAuthError:
                    # Permission denied - log error and mark as unavailable
                    logger.error(
                        f"Permission denied fetching PR/issue counts for {name}"
                    )
                    repo_data["open_prs"] = -1
                    repo_data["open_issues"] = -1
                except GitHubRateLimitError:
                    # Rate limit hit - this is a critical error
                    logger.error(
                        f"Rate limit exceeded while fetching PR/issue counts for {name}"
                    )
                    repo_data["open_prs"] = -1
                    repo_data["open_issues"] = -1
                except GitHubAPIError as e:
                    # Expected API errors (4xx, 5xx) - log warning
                    logger.warning(
                        f"GitHub API error fetching PR/issue counts for {name}: {e}"
                    )
                    repo_data["open_prs"] = -1
                    repo_data["open_issues"] = -1
                except Exception as e:
                    # Unexpected errors - log as error for investigation
                    logger.error(
                        f"Unexpected error fetching PR/issue counts for {name}: {type(e).__name__}: {e}",
                        exc_info=True,
                    )
                    repo_data["open_prs"] = -1
                    repo_data["open_issues"] = -1

                progress_bar.update(task, advance=1)

        return repos_map

    async def execute_reset(
        self,
        compare: bool = False,
        no_confirm: bool = False,
    ) -> ResetResult:
        """
        Execute the complete reset operation.

        Args:
            compare: Whether to compare with local Gerrit clone
            no_confirm: Skip confirmation prompt

        Returns:
            ResetResult with operation details
        """
        # Scan GitHub organization
        # Skip expensive PR/issue count fetching when --no-confirm is used,
        # since the table and counts are only needed for manual confirmation
        remote_repos = await self.scan_github_organization(
            skip_pr_issue_counts=no_confirm
        )

        if not remote_repos:
            self.console.print(
                f"[yellow]No repositories found in organization: {self.org}[/yellow]"
            )
            return ResetResult(
                organization=self.org,
                total_repos=0,
                deleted_repos=0,
                failed_deletions=[],
                unsynchronized_repos=[],
                total_prs=0,
                total_issues=0,
            )

        total_prs = 0
        total_issues = 0

        # Only display the repos table when confirmation is needed;
        # when --no-confirm is used, the table output is superfluous
        if not no_confirm:
            total_prs, total_issues = self.display_repos_table(remote_repos)

        # Compare with local if requested
        unsynchronized: list[SyncComparison] = []
        if compare:
            comparisons = self.compare_with_local(remote_repos)
            unsynchronized = [c for c in comparisons if not c.is_synchronized]

        # Confirmation
        if not no_confirm:
            confirmed = self.prompt_for_confirmation(
                repo_count=len(remote_repos),
                total_prs=total_prs,
                total_issues=total_issues,
            )
            if not confirmed:
                return ResetResult(
                    organization=self.org,
                    total_repos=len(remote_repos),
                    deleted_repos=0,
                    failed_deletions=[],
                    unsynchronized_repos=unsynchronized,
                    total_prs=total_prs,
                    total_issues=total_issues,
                )
        else:
            self.console.print(
                "\n⚠️  [yellow]--no-confirm flag used, skipping confirmation[/yellow]"
            )

        repo_names = list(remote_repos.keys())
        results = await self.delete_all_repos(repo_names)

        # Calculate results
        success_count = sum(1 for success, _ in results.values() if success)
        failed_repos = [name for name, (success, _) in results.items() if not success]

        return ResetResult(
            organization=self.org,
            total_repos=len(remote_repos),
            deleted_repos=success_count,
            failed_deletions=failed_repos,
            unsynchronized_repos=unsynchronized,
            total_prs=total_prs,
            total_issues=total_issues,
        )


__all__ = [
    "ResetManager",
]
