# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2025 Matthew Watkins <mwatkins@linuxfoundation.org>

"""Integration tests for GitHub repository cloning.

These tests clone real repositories from the lfreleng-actions organization.
They require a GitHub token to be set in the GITHUB_TOKEN environment variable.
"""

from __future__ import annotations

import os
import shutil
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from pathlib import Path

from gerrit_clone.clone_manager import clone_repositories
from gerrit_clone.github_discovery import discover_github_repositories
from gerrit_clone.models import CloneStatus, Config, DiscoveryMethod, SourceType


@pytest.fixture
def github_token() -> str:
    """Get GitHub token from environment."""
    token = os.getenv("GITHUB_TOKEN")
    if not token:
        pytest.skip("GITHUB_TOKEN environment variable not set")
    # At this point, we know token is not None due to the skip above
    # But mypy doesn't understand pytest.skip() stops execution
    assert token is not None
    return token


@pytest.fixture
def tmp_clone_dir(tmp_path: Path) -> Path:
    """Temporary directory for cloning, managed by pytest."""
    # Use the pytest-provided tmp_path so pytest handles cleanup automatically
    return tmp_path


@pytest.mark.integration
class TestGitHubDiscovery:
    """Integration tests for GitHub repository discovery."""

    def test_discover_lfreleng_actions_repos(self, github_token: str) -> None:
        """Test discovering repositories from lfreleng-actions org."""
        config = Config(
            host="github.com/lfreleng-actions",
            source_type=SourceType.GITHUB,
            github_org="lfreleng-actions",
            github_token=github_token,
            discovery_method=DiscoveryMethod.GITHUB_API,
        )

        projects, stats = discover_github_repositories(config)

        # Verify we found repositories
        assert len(projects) > 0
        assert stats["total"] > 0
        assert stats["discovery_method"] == "github_api"
        assert stats["org_or_user"] == "lfreleng-actions"

        # Verify all projects have required fields
        for project in projects:
            assert project.name
            assert project.source_type == SourceType.GITHUB
            assert project.clone_url or project.ssh_url_override


@pytest.mark.integration
class TestGitHubClone:
    """Integration tests for GitHub repository cloning."""

    def test_clone_single_repo_with_filter(
        self,
        github_token: str,
        tmp_clone_dir: Path,
    ) -> None:
        """Test cloning a single repository using include filter."""
        # Use a small, known repository from lfreleng-actions
        # Note: This test will need to be updated if the repo doesn't exist
        config = Config(
            host="github.com/lfreleng-actions",
            source_type=SourceType.GITHUB,
            github_org="lfreleng-actions",
            github_token=github_token,
            discovery_method=DiscoveryMethod.GITHUB_API,
            path=tmp_clone_dir,
            include_projects=["gerrit-clone-action"],  # Known repo
            threads=1,
            mirror=False,  # Use regular clone (not bare) for testing
        )

        # Discover and verify the repository exists
        projects, _ = discover_github_repositories(config)

        if not projects:
            pytest.skip("gerrit-clone-action repository not found in lfreleng-actions")

        # Clone the repository
        batch_result = clone_repositories(config)

        # Verify clone succeeded
        assert batch_result.success_count >= 1
        assert batch_result.failed_count == 0

        # Verify repository exists on disk
        repo_path = tmp_clone_dir / "gerrit-clone-action"
        assert repo_path.exists()

        # Verify it's a valid git repository
        # With mirror=False, .git directory should exist
        assert (repo_path / ".git").exists()
        assert (repo_path / ".git" / "config").exists()

    def test_clone_with_shallow_depth(
        self,
        github_token: str,
        tmp_clone_dir: Path,
    ) -> None:
        """Test cloning with shallow depth."""
        config = Config(
            host="github.com/lfreleng-actions",
            source_type=SourceType.GITHUB,
            github_org="lfreleng-actions",
            github_token=github_token,
            discovery_method=DiscoveryMethod.GITHUB_API,
            path=tmp_clone_dir,
            include_projects=["gerrit-clone-action"],
            depth=1,
            threads=1,
        )

        # Clone the repository
        batch_result = clone_repositories(config)

        # Verify clone succeeded
        assert batch_result.success_count >= 1
        assert batch_result.failed_count == 0

    def test_clone_multiple_repos(
        self,
        github_token: str,
        tmp_clone_dir: Path,
    ) -> None:
        """Test cloning multiple repositories in parallel."""
        config = Config(
            host="github.com/lfreleng-actions",
            source_type=SourceType.GITHUB,
            github_org="lfreleng-actions",
            github_token=github_token,
            discovery_method=DiscoveryMethod.GITHUB_API,
            path=tmp_clone_dir,
            threads=4,  # Use multiple threads
            skip_archived=True,
        )

        # Clone repositories (limit to first 5 for speed)
        projects, _ = discover_github_repositories(config)

        # Limit to 5 repos for testing
        if len(projects) > 5:
            config.include_projects = [p.name for p in projects[:5]]

        batch_result = clone_repositories(config)

        # Verify at least some repos cloned successfully
        assert batch_result.success_count > 0
        assert batch_result.total_count > 0

    def test_clone_detects_already_exists(
        self,
        github_token: str,
        tmp_clone_dir: Path,
    ) -> None:
        """Test that cloning an already-cloned repo is detected."""
        config = Config(
            host="github.com/lfreleng-actions",
            source_type=SourceType.GITHUB,
            github_org="lfreleng-actions",
            github_token=github_token,
            discovery_method=DiscoveryMethod.GITHUB_API,
            path=tmp_clone_dir,
            include_projects=["gerrit-clone-action"],
            threads=1,
        )

        # First clone
        batch_result1 = clone_repositories(config)
        assert batch_result1.success_count >= 1

        # Second clone (should detect already exists or refresh)
        batch_result2 = clone_repositories(config)

        # Check that repos were detected as already existing, refreshed, or verified
        # With auto_refresh=True (default), repos will be VERIFIED (up-to-date) or REFRESHED (updated)
        # With auto_refresh=False (--no-refresh), repos will be ALREADY_EXISTS
        already_handled = sum(
            1
            for r in batch_result2.results
            if r.status
            in {
                CloneStatus.ALREADY_EXISTS,
                CloneStatus.REFRESHED,
                CloneStatus.VERIFIED,
            }
        )
        assert already_handled >= 1

    def test_clone_with_no_refresh_flag(
        self,
        github_token: str,
        tmp_clone_dir: Path,
    ) -> None:
        """Test that --no-refresh flag returns ALREADY_EXISTS status."""
        config = Config(
            host="github.com/lfreleng-actions",
            source_type=SourceType.GITHUB,
            github_org="lfreleng-actions",
            github_token=github_token,
            discovery_method=DiscoveryMethod.GITHUB_API,
            path=tmp_clone_dir,
            include_projects=["gerrit-clone-action"],
            threads=1,
            auto_refresh=False,  # Disable auto-refresh
        )

        # First clone
        batch_result1 = clone_repositories(config)
        assert batch_result1.success_count >= 1

        # Second clone with auto_refresh=False (should return ALREADY_EXISTS)
        batch_result2 = clone_repositories(config)

        # Check that repos were detected as already existing (not refreshed)
        already_exists = sum(
            1 for r in batch_result2.results if r.status == CloneStatus.ALREADY_EXISTS
        )
        assert already_exists >= 1


@pytest.mark.integration
class TestGitHubCloneWithGhCli:
    """Integration tests for GitHub cloning using gh CLI."""

    def test_clone_with_gh_cli_if_available(
        self,
        github_token: str,
        tmp_clone_dir: Path,
    ) -> None:
        """Test cloning with gh CLI if available."""
        # Check if gh CLI is available
        if not shutil.which("gh"):
            pytest.skip("gh CLI not available")

        config = Config(
            host="github.com/lfreleng-actions",
            source_type=SourceType.GITHUB,
            github_org="lfreleng-actions",
            github_token=github_token,
            discovery_method=DiscoveryMethod.GITHUB_API,
            path=tmp_clone_dir,
            include_projects=["gerrit-clone-action"],
            use_gh_cli=True,
            threads=1,
            mirror=False,  # Use regular clone (not bare) for testing
        )

        # Clone the repository
        batch_result = clone_repositories(config)

        # Verify clone succeeded
        assert batch_result.success_count >= 1
        assert batch_result.failed_count == 0

        # Verify repository exists on disk
        repo_path = tmp_clone_dir / "gerrit-clone-action"
        assert repo_path.exists()

        # Verify it's a valid git repository
        # With mirror=False, .git directory should exist
        assert (repo_path / ".git").exists()
