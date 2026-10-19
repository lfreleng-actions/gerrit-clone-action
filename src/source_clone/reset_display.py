# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""Console rendering and local/remote comparison for organization resets.

Holds the shared state contract for the ResetManager class hierarchy plus
the read-only presentation helpers: the repository summary table, the
local Gerrit clone comparison report, and commit date formatting.
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

from rich.table import Table

from source_clone.git_comparison import (
    compare_local_with_remote,
    scan_local_gerrit_clone,
)
from source_clone.logging import get_logger

if TYPE_CHECKING:
    from pathlib import Path

    from rich.console import Console

    from source_clone.github_api import GitHubAPI
    from source_clone.reset_models import GitHubRepoStatus, SyncComparison

logger = get_logger(__name__)


class ResetDisplayBase:
    """Shared state and presentation helpers for :class:`ResetManager`.

    Subclasses are responsible for assigning every attribute declared
    here in their ``__init__``.
    """

    org: str
    local_path: Path
    console: Console
    github_api: GitHubAPI
    include_automation_prs: bool
    automation_authors: set[str]

    def is_automation_author(self, author: str) -> bool:
        """
        Check if the author is a known automation tool.

        Args:
            author: GitHub username to check

        Returns:
            True if author is a known automation tool, False otherwise
        """
        return author in self.automation_authors

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
        table.add_column(
            "Pull Requests", justify="right", style="yellow", no_wrap=True, min_width=14
        )
        table.add_column(
            "Issues", justify="right", style="magenta", no_wrap=True, min_width=8
        )
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


__all__ = [
    "ResetDisplayBase",
]
