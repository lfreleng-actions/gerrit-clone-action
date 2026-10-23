# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2025 Matthew Watkins <mwatkins@linuxfoundation.org>

"""Data models for GitHub organization reset operations."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path


@dataclass
class GitHubRepoStatus:
    """GitHub repository with statistics."""

    name: str
    full_name: str
    url: str
    open_prs: int
    open_issues: int
    last_commit_sha: str | None
    last_commit_date: str | None
    default_branch: str


@dataclass
class LocalRepoStatus:
    """Local Git repository status."""

    name: str
    path: Path
    last_commit_sha: str | None
    commit_count: int | None
    current_branch: str | None
    is_valid_git_repo: bool


@dataclass
class SyncComparison:
    """Comparison between local and remote repository."""

    repo_name: str
    local_status: LocalRepoStatus | None
    remote_status: GitHubRepoStatus
    is_synchronized: bool
    difference_description: str

    @property
    def commits_differ(self) -> bool:
        """Check if local and remote repositories have different commits.

        This indicates the repositories have diverged - they point to different
        commits. This is distinct from is_synchronized which may be False for
        other reasons (missing SHAs, invalid repos, etc.).

        Returns:
            True if both local and remote have valid SHAs and they differ,
            False otherwise (including when either SHA is missing)
        """
        if not self.local_status:
            return False
        if not self.local_status.last_commit_sha:
            return False
        if not self.remote_status.last_commit_sha:
            return False
        return self.local_status.last_commit_sha != self.remote_status.last_commit_sha


@dataclass
class ResetResult:
    """Result of reset operation."""

    organization: str
    total_repos: int
    deleted_repos: int
    failed_deletions: list[str]
    unsynchronized_repos: list[SyncComparison]
    total_prs: int
    total_issues: int

    @property
    def success_rate(self) -> float:
        """Calculate success rate of deletion operation.

        Returns:
            Percentage of successfully deleted repos (0.0 to 1.0)
        """
        if self.total_repos == 0:
            return 1.0
        return self.deleted_repos / self.total_repos

    @property
    def had_unsynchronized(self) -> bool:
        """Check if there were any unsynchronized repositories.

        Returns:
            True if any repos were unsynchronized
        """
        return len(self.unsynchronized_repos) > 0
