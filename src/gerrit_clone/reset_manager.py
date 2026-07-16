# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2025 Matthew Watkins <mwatkins@linuxfoundation.org>

"""Manager for GitHub organization reset operations using native github_api."""

from __future__ import annotations

import random
import re
from datetime import datetime
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
from rich.table import Table

from gerrit_clone.git_comparison import (
    compare_local_with_remote,
    scan_local_gerrit_clone,
)
from gerrit_clone.github_api import (
    GitHubAPI,
    GitHubAPIError,
    GitHubAuthError,
    GitHubNotFoundError,
    GitHubRateLimitError,
)
from gerrit_clone.logging import get_logger
from gerrit_clone.reset_models import (
    GitHubRepoStatus,
    ResetResult,
    SyncComparison,
)

logger = get_logger(__name__)


class ResetManager:
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

    def is_automation_author(self, author: str) -> bool:
        """
        Check if the author is a known automation tool.

        Args:
            author: GitHub username to check

        Returns:
            True if author is a known automation tool, False otherwise
        """
        return author in self.automation_authors

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
            self.console.print(
                f"✅ Authenticated as: [cyan]{username}[/cyan]"
            )
            return True
        except Exception as e:
            logger.error(f"Error checking token permissions: {e}")
            self.console.print(
                f"[red]❌ Error checking permissions: {e}[/red]"
            )
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
        self.console.print(
            f"📥 Scanning GitHub organization: [cyan]{self.org}[/cyan]"
        )

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
            self.console.print(
                f"[red]❌ Error scanning organization: {e}[/red]"
            )
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
        task = progress_bar.add_task(
            "Fetching PR/Issue counts", total=total_repos
        )

        with Live(progress_bar, console=self.console, refresh_per_second=4, transient=True):
            # Enhance with PR/issue counts using REST API
            for name, repo_data in repos_map.items():
                progress_bar.update(task, description=f"Fetching PR/Issue counts ({name})")

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
                            1 for pr in all_prs
                            if not self.is_automation_author((pr.get("user") or {}).get("login", ""))
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
                    logger.info(f"Repository {name} not found, skipping PR/issue counts")
                    repo_data["open_prs"] = -1  # -1 indicates "unknown/unavailable"
                    repo_data["open_issues"] = -1
                except GitHubAuthError:
                    # Permission denied - log error and mark as unavailable
                    logger.error(f"Permission denied fetching PR/issue counts for {name}")
                    repo_data["open_prs"] = -1
                    repo_data["open_issues"] = -1
                except GitHubRateLimitError:
                    # Rate limit hit - this is a critical error
                    logger.error(f"Rate limit exceeded while fetching PR/issue counts for {name}")
                    repo_data["open_prs"] = -1
                    repo_data["open_issues"] = -1
                except GitHubAPIError as e:
                    # Expected API errors (4xx, 5xx) - log warning
                    logger.warning(f"GitHub API error fetching PR/issue counts for {name}: {e}")
                    repo_data["open_prs"] = -1
                    repo_data["open_issues"] = -1
                except Exception as e:
                    # Unexpected errors - log as error for investigation
                    logger.error(
                        f"Unexpected error fetching PR/issue counts for {name}: {type(e).__name__}: {e}",
                        exc_info=True
                    )
                    repo_data["open_prs"] = -1
                    repo_data["open_issues"] = -1

                progress_bar.update(task, advance=1)

        return repos_map

    def display_repos_table(
        self, repos: dict[str, GitHubRepoStatus]
    ) -> tuple[int, int]:
        """
        Display repositories in a Rich table with statistics.

        Args:
            repos: Dictionary of repository statuses

        Returns:
            Tuple of (total_prs, total_issues)
        """
        table = Table(title=f"📦 GitHub Organization: {self.org}")

        table.add_column("Repository", style="cyan", no_wrap=True, ratio=1)
        table.add_column("Pull Requests", justify="right", style="yellow", no_wrap=True, min_width=14)
        table.add_column("Issues", justify="right", style="magenta", no_wrap=True, min_width=8)
        table.add_column("Last Commit", style="dim", no_wrap=True, min_width=12)

        total_prs = 0
        total_issues = 0

        # Sort repos alphabetically
        for repo in sorted(repos.values(), key=lambda r: r.name):
            last_commit = "N/A"
            if repo.last_commit_date:
                last_commit = self._format_commit_date(repo.last_commit_date)

            # Format counts, showing "?" for unavailable data (-1)
            prs_display = "?" if repo.open_prs < 0 else str(repo.open_prs)
            issues_display = "?" if repo.open_issues < 0 else str(repo.open_issues)

            table.add_row(
                repo.name,
                prs_display,
                issues_display,
                last_commit,
            )
            # Only count valid values in totals
            if repo.open_prs >= 0:
                total_prs += repo.open_prs
            if repo.open_issues >= 0:
                total_issues += repo.open_issues

        self.console.print(table)

        summary_parts = [
            f"\n📊 Summary: [cyan]{len(repos)}[/cyan] repositories, "
            f"[yellow]{total_prs}[/yellow] open PRs"
        ]

        if not self.include_automation_prs:
            summary_parts.append(" (excluding automation)")

        summary_parts.append(f", [magenta]{total_issues}[/magenta] open issues")

        self.console.print("".join(summary_parts))

        return total_prs, total_issues

    def compare_with_local(
        self,
        remote_repos: dict[str, GitHubRepoStatus],
    ) -> list[SyncComparison]:
        """
        Compare remote GitHub repos with local Gerrit clone.

        Args:
            remote_repos: Dictionary of remote repository statuses

        Returns:
            List of SyncComparison objects
        """
        self.console.print(
            f"\n🔍 Scanning local repositories at: [cyan]{self.local_path}[/cyan]"
        )

        local_repos = scan_local_gerrit_clone(self.local_path)
        self.console.print(f"Found {len(local_repos)} local repositories")

        comparisons = compare_local_with_remote(local_repos, remote_repos)

        # Display unsynchronized repos
        unsynchronized = [c for c in comparisons if not c.is_synchronized]

        if unsynchronized:
            table = Table(
                title=f"⚠️  Unsynchronized Repositories ({len(unsynchronized)})"
            )
            table.add_column("Repository", style="cyan")
            table.add_column("Local SHA", style="dim")
            table.add_column("Remote SHA", style="dim")
            table.add_column("Status", style="yellow")

            for comp in unsynchronized:
                local_sha = (
                    comp.local_status.last_commit_sha[:8]
                    if comp.local_status and comp.local_status.last_commit_sha
                    else "N/A"
                )
                remote_sha = (
                    comp.remote_status.last_commit_sha[:8]
                    if comp.remote_status.last_commit_sha
                    else "N/A"
                )

                table.add_row(
                    comp.repo_name,
                    local_sha,
                    remote_sha,
                    comp.difference_description,
                )

            self.console.print(table)
            self.console.print(
                f"\n⚠️  [yellow]WARNING:[/yellow] {len(unsynchronized)} "
                "repositories have differences between local and remote!"
            )
        else:
            self.console.print("\n✅ All repositories are synchronized")

        return comparisons

    def generate_confirmation_hash(
        self,
        repo_count: int,
        total_prs: int,
        total_issues: int,
    ) -> str:
        """
        Generate unique confirmation code for user confirmation.

        NOTE: This is NOT a security feature. The code is a UX mechanism to:
        - Require users to think more carefully than typing Y/yes
        - Change each time to prevent muscle-memory confirmations
        - Ensure users review the displayed statistics before proceeding

        Uses a simple random code based on org state to provide sufficient
        variation for this UX purpose without implying cryptographic security.

        Args:
            repo_count: Number of repositories to delete
            total_prs: Total open PRs across all repositories
            total_issues: Total open issues across all repositories

        Returns:
            Random alphanumeric code (16 characters) - for UX confirmation only
        """
        # Generate a pseudo-random confirmation code based on current org state
        # Use a fixed seed so the same state produces the same code
        combined_seed = f"reset:{self.org}:{repo_count}:{total_prs}:{total_issues}"
        seed_value = sum(ord(c) for c in combined_seed)

        rng = random.Random(seed_value)

        # Generate 16-character alphanumeric code (avoiding ambiguous chars)
        chars = "23456789abcdefghjkmnpqrstuvwxyz"  # No 0, O, 1, l, i for clarity
        return "".join(rng.choices(chars, k=16))

    def prompt_for_confirmation(
        self,
        repo_count: int,
        total_prs: int,
        total_issues: int,
    ) -> bool:
        """
        Prompt user for confirmation hash.

        Args:
            repo_count: Number of repositories to delete
            total_prs: Total open PRs
            total_issues: Total open issues

        Returns:
            True if user confirmed with correct hash, False otherwise
        """
        confirmation_hash = self.generate_confirmation_hash(
            repo_count, total_prs, total_issues
        )

        self.console.print()
        self.console.print(
            f"[red]⚠️  WARNING: This will PERMANENTLY DELETE {repo_count} repositories![/red]"
        )
        self.console.print(f"Organization: [cyan]{self.org}[/cyan]")
        self.console.print(
            f"Open PRs that will be lost: [yellow]{total_prs}[/yellow]"
        )
        self.console.print(
            f"Open Issues that will be lost: [magenta]{total_issues}[/magenta]"
        )
        self.console.print()
        self.console.print(
            f"To proceed, enter: [green]{confirmation_hash}[/green]"
        )

        try:
            user_input = input(
                "Enter the hash above to continue (or press Enter to cancel): "
            ).strip()

            if user_input == confirmation_hash:
                self.console.print("✅ Confirmation received")
                return True
            elif user_input == "":
                self.console.print("❌ Reset cancelled by user")
                return False
            else:
                self.console.print("❌ Invalid hash. Reset cancelled.")
                return False
        except (KeyboardInterrupt, EOFError):
            self.console.print("\n❌ Reset cancelled by user")
            return False

    def _format_commit_date(self, date_str: str) -> str:
        """
        Format a commit date string to YYYY-MM-DD format.

        Handles various ISO 8601 formats from GitHub API and falls back
        to safe truncation if parsing fails.

        Args:
            date_str: Date string from GitHub API (typically ISO 8601)

        Returns:
            Formatted date string (YYYY-MM-DD) or "N/A" if invalid
        """
        if not date_str or not date_str.strip():
            return "N/A"

        try:
            # Try to parse as ISO 8601 format (e.g., "2025-01-18T12:34:56Z")
            # Handle both with and without timezone
            for fmt in [
                "%Y-%m-%dT%H:%M:%SZ",
                "%Y-%m-%dT%H:%M:%S",
                "%Y-%m-%d %H:%M:%S",
                "%Y-%m-%d",
            ]:
                try:
                    dt = datetime.strptime(date_str.strip(), fmt)
                    return dt.strftime("%Y-%m-%d")
                except ValueError:
                    continue

            # If parsing fails, try safe truncation as fallback
            # Only if it looks like a date (starts with YYYY-MM-DD pattern)
            if len(date_str) >= 10 and date_str[4] == "-" and date_str[7] == "-":
                return date_str[:10]

            # Last resort: return as-is if short enough, otherwise truncate
            return date_str[:10] if len(date_str) > 10 else date_str

        except Exception as e:
            logger.warning(f"Failed to format date '{date_str}': {e}")
            return "N/A"

    def _validate_repo_name(self, name: str) -> tuple[bool, str | None]:
        """
        Validate a GitHub repository name.

        GitHub repository names must:
        - Not be empty
        - Contain only alphanumeric characters, hyphens, underscores, and dots
        - Not start or end with a hyphen or underscore
        - May start with a dot (e.g. ``.github`` for org-level config)
        - Be between 1 and 100 characters

        Args:
            name: Repository name to validate

        Returns:
            Tuple of (is_valid, error_message)
        """
        if not name or not name.strip():
            return False, "Repository name cannot be empty"

        name = name.strip()

        if len(name) > 100:
            return False, "Repository name exceeds 100 characters"

        # GitHub allows alphanumeric, hyphens, underscores, and dots.
        # Names may start with a dot (e.g. ".github") but must not
        # start or end with a hyphen or underscore.
        if not re.match(
            r"^\.?[a-zA-Z0-9]([a-zA-Z0-9._-]*[a-zA-Z0-9])?$", name
        ):
            return False, "Repository name contains invalid characters or format"

        return True, None

    async def delete_all_repos(
        self,
        repo_names: list[str],
    ) -> dict[str, tuple[bool, str | None]]:
        """
        Delete all repositories in the organization.

        Args:
            repo_names: List of repository names to delete

        Returns:
            Dictionary mapping repo name to (success, error_message)
        """
        invalid_names: dict[str, str] = {}
        valid_names: list[str] = []

        for name in repo_names:
            is_valid, error = self._validate_repo_name(name)
            if not is_valid:
                invalid_names[name] = error or "Invalid repository name"
            else:
                valid_names.append(name)

        if invalid_names:
            self.console.print(
                f"\n⚠️  [yellow]Skipping {len(invalid_names)} invalid repository names:[/yellow]"
            )
            for name, error in list(invalid_names.items())[:5]:
                self.console.print(f"  - {name}: {error}")
            if len(invalid_names) > 5:
                self.console.print(
                    f"  ... and {len(invalid_names) - 5} more"
                )

        if not valid_names:
            self.console.print("\n❌ No valid repository names to delete")
            return {name: (False, error) for name, error in invalid_names.items()}

        self.console.print(
            f"\n🗑️  Deleting {len(valid_names)} repositories..."
        )

        # Use existing batch_delete_repos from github_api.py
        results = await self.github_api.batch_delete_repos(
            owner=self.org,
            repo_names=valid_names,
            max_concurrent=10,
        )

        # Merge invalid names into results
        for name, error in invalid_names.items():
            results[name] = (False, error)

        success_count = sum(1 for success, _ in results.values() if success)
        failed_count = len(results) - success_count

        if failed_count > 0:
            failed_repos = [
                name for name, (success, _) in results.items() if not success
            ]
            self.console.print(
                f"\n⚠️  [yellow]Failed to delete {failed_count} repositories:[/yellow]"
            )
            for name in failed_repos[:5]:  # Show first 5
                _, error = results[name]
                self.console.print(f"  - {name}: {error}")

            if len(failed_repos) > 5:
                self.console.print(
                    f"  ... and {len(failed_repos) - 5} more"
                )

        self.console.print(
            f"\n✅ Successfully deleted {success_count}/{len(results)} repositories"
        )

        return results

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
        failed_repos = [
            name for name, (success, _) in results.items() if not success
        ]

        return ResetResult(
            organization=self.org,
            total_repos=len(remote_repos),
            deleted_repos=success_count,
            failed_deletions=failed_repos,
            unsynchronized_repos=unsynchronized,
            total_prs=total_prs,
            total_issues=total_issues,
        )
