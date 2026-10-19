# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2025 Matthew Watkins <mwatkins@linuxfoundation.org>

"""Unit tests for reset manager."""

from __future__ import annotations

import io
from typing import Any
from unittest.mock import AsyncMock, Mock, patch

import pytest
from rich.console import Console

from source_clone.github_api import GitHubAPIError, GitHubNotFoundError
from source_clone.reset_manager import ResetManager
from source_clone.reset_models import GitHubRepoStatus


class TestResetManager:
    """Test suite for ResetManager."""

    @pytest.fixture
    def mock_console(self):
        """Create a real console for tests."""
        return Console(file=io.StringIO(), force_terminal=False, width=80)

    @pytest.fixture
    def reset_manager(self, mock_console):
        """Create a ResetManager instance with mocked dependencies."""
        with patch("source_clone.reset_manager.GitHubAPI"):
            manager = ResetManager(
                org="test-org",
                github_token="fake-token",
                console=mock_console,
                include_automation_prs=False,
            )
            manager.github_api = Mock()
            return manager

    def test_is_automation_author_dependabot(self, reset_manager):
        """Test that dependabot is recognized as automation."""
        assert reset_manager.is_automation_author("dependabot[bot]")

    def test_is_automation_author_precommit(self, reset_manager):
        """Test that pre-commit.ci is recognized as automation."""
        assert reset_manager.is_automation_author("pre-commit-ci[bot]")

    def test_is_automation_author_renovate(self, reset_manager):
        """Test that renovate is recognized as automation."""
        assert reset_manager.is_automation_author("renovate[bot]")

    def test_is_automation_author_github_actions(self, reset_manager):
        """Test that github-actions is recognized as automation."""
        assert reset_manager.is_automation_author("github-actions[bot]")

    def test_is_automation_author_allcontributors(self, reset_manager):
        """Test that allcontributors is recognized as automation."""
        assert reset_manager.is_automation_author("allcontributors[bot]")

    def test_is_automation_author_human(self, reset_manager):
        """Test that human users are not recognized as automation."""
        assert not reset_manager.is_automation_author("john-doe")
        assert not reset_manager.is_automation_author("jane-smith")
        assert not reset_manager.is_automation_author("some-user")

    def test_is_automation_author_partial_match(self, reset_manager):
        """Test that partial matches don't count as automation."""
        # Should not match unless exact
        assert not reset_manager.is_automation_author("dependabot")
        assert not reset_manager.is_automation_author("bot")
        assert not reset_manager.is_automation_author("[bot]")

    @pytest.mark.asyncio
    async def test_fetch_repos_excludes_automation_prs_by_default(
        self, reset_manager, mock_console
    ):
        """Test that automation PRs are excluded from counts by default."""
        # Setup mock responses
        reset_manager.github_api.list_all_repos_graphql.return_value = {
            "test-repo": {
                "name": "test-repo",
                "full_name": "test-org/test-repo",
                "html_url": "https://github.com/test-org/test-repo",
            }
        }

        # Mock PR response with mix of automation and human PRs
        mock_prs = [
            {"user": {"login": "dependabot[bot]"}, "number": 1},
            {"user": {"login": "pre-commit-ci[bot]"}, "number": 2},
            {"user": {"login": "human-user"}, "number": 3},
            {"user": {"login": "another-human"}, "number": 4},
        ]

        mock_issues: list[Any] = []

        reset_manager.github_api._request_paginated.side_effect = [
            mock_prs,  # First call for PRs
            mock_issues,  # Second call for issues
        ]

        # Execute with Live mocked
        with patch("source_clone.reset_manager.Live"):
            repos_data = await reset_manager._fetch_repos_with_graphql()

        # Verify - should only count human PRs (2 out of 4)
        assert repos_data["test-repo"]["open_prs"] == 2
        assert repos_data["test-repo"]["open_issues"] == 0

    @pytest.mark.asyncio
    async def test_fetch_repos_includes_automation_prs_when_enabled(self, mock_console):
        """Test that automation PRs are included when flag is set."""
        with patch("source_clone.reset_manager.GitHubAPI"):
            # Create manager with include_automation_prs=True
            manager = ResetManager(
                org="test-org",
                github_token="fake-token",
                console=mock_console,
                include_automation_prs=True,
            )
            manager.github_api = Mock()

            # Setup mock responses
            manager.github_api.list_all_repos_graphql.return_value = {
                "test-repo": {
                    "name": "test-repo",
                    "full_name": "test-org/test-repo",
                    "html_url": "https://github.com/test-org/test-repo",
                }
            }

            # Mock PR response with mix of automation and human PRs
            mock_prs = [
                {"user": {"login": "dependabot[bot]"}, "number": 1},
                {"user": {"login": "pre-commit-ci[bot]"}, "number": 2},
                {"user": {"login": "human-user"}, "number": 3},
                {"user": {"login": "another-human"}, "number": 4},
            ]

            mock_issues: list[Any] = []

            manager.github_api._request_paginated.side_effect = [
                mock_prs,  # First call for PRs
                mock_issues,  # Second call for issues
            ]

            # Execute with Live mocked
            with patch("source_clone.reset_manager.Live"):
                repos_data = await manager._fetch_repos_with_graphql()

            # Verify - should count all PRs (4 out of 4)
            assert repos_data["test-repo"]["open_prs"] == 4
            assert repos_data["test-repo"]["open_issues"] == 0

    @pytest.mark.asyncio
    async def test_fetch_repos_handles_all_automation_prs(
        self, reset_manager, mock_console
    ):
        """Test that repos with only automation PRs show 0 count."""
        # Setup mock responses
        reset_manager.github_api.list_all_repos_graphql.return_value = {
            "test-repo": {
                "name": "test-repo",
                "full_name": "test-org/test-repo",
                "html_url": "https://github.com/test-org/test-repo",
            }
        }

        # Mock PR response with only automation PRs
        mock_prs = [
            {"user": {"login": "dependabot[bot]"}, "number": 1},
            {"user": {"login": "pre-commit-ci[bot]"}, "number": 2},
            {"user": {"login": "renovate[bot]"}, "number": 3},
        ]

        mock_issues: list[Any] = []

        reset_manager.github_api._request_paginated.side_effect = [
            mock_prs,  # First call for PRs
            mock_issues,  # Second call for issues
        ]

        # Execute with Live mocked
        with patch("source_clone.reset_manager.Live"):
            repos_data = await reset_manager._fetch_repos_with_graphql()

        # Verify - should show 0 PRs
        assert repos_data["test-repo"]["open_prs"] == 0
        assert repos_data["test-repo"]["open_issues"] == 0

    @pytest.mark.asyncio
    async def test_fetch_repos_handles_missing_user_info(
        self, reset_manager, mock_console
    ):
        """Test that PRs with missing user info are handled gracefully."""
        # Setup mock responses
        reset_manager.github_api.list_all_repos_graphql.return_value = {
            "test-repo": {
                "name": "test-repo",
                "full_name": "test-org/test-repo",
                "html_url": "https://github.com/test-org/test-repo",
            }
        }

        # Mock PR response with missing/malformed user data
        mock_prs = [
            {"user": {}, "number": 1},  # Empty user dict
            {"user": {"login": ""}, "number": 2},  # Empty login
            {"number": 3},  # Missing user entirely
            {"user": {"login": "human-user"}, "number": 4},  # Valid human
        ]

        mock_issues: list[Any] = []

        reset_manager.github_api._request_paginated.side_effect = [
            mock_prs,  # First call for PRs
            mock_issues,  # Second call for issues
        ]

        # Execute with Live mocked
        with patch("source_clone.reset_manager.Live"):
            repos_data = await reset_manager._fetch_repos_with_graphql()

        # Verify - should count all non-automation PRs including malformed ones (4)
        assert repos_data["test-repo"]["open_prs"] == 4
        assert repos_data["test-repo"]["open_issues"] == 0

    @pytest.mark.asyncio
    async def test_fetch_repos_correct_issue_count_with_automation_filter(
        self, reset_manager, mock_console
    ):
        """Test that issues are correctly counted when automation PRs are filtered.

        GitHub's /issues endpoint returns both issues AND PRs.
        We must subtract the TOTAL PR count (not filtered) to get true issues.
        """
        # Setup mock responses
        reset_manager.github_api.list_all_repos_graphql.return_value = {
            "test-repo": {
                "name": "test-repo",
                "full_name": "test-org/test-repo",
                "html_url": "https://github.com/test-org/test-repo",
            }
        }

        # Mock PR response - 3 automation PRs, 0 human PRs
        mock_prs = [
            {"user": {"login": "dependabot[bot]"}, "number": 1},
            {"user": {"login": "pre-commit-ci[bot]"}, "number": 2},
            {"user": {"login": "renovate[bot]"}, "number": 3},
        ]

        # Mock issues response - returns 3 items (which are the PRs above)
        # GitHub's /issues endpoint returns both issues and PRs
        mock_issues = [
            {"user": {"login": "dependabot[bot]"}, "number": 1, "pull_request": {}},
            {"user": {"login": "pre-commit-ci[bot]"}, "number": 2, "pull_request": {}},
            {"user": {"login": "renovate[bot]"}, "number": 3, "pull_request": {}},
        ]

        reset_manager.github_api._request_paginated.side_effect = [
            mock_prs,  # First call for PRs
            mock_issues,  # Second call for issues
        ]

        # Execute with Live mocked
        with patch("source_clone.reset_manager.Live"):
            repos_data = await reset_manager._fetch_repos_with_graphql()

        # Verify:
        # - open_prs should be 0 (all automation filtered out)
        # - open_issues should be 0 (3 items - 3 total PRs = 0 true issues)
        assert repos_data["test-repo"]["open_prs"] == 0
        assert repos_data["test-repo"]["open_issues"] == 0

    def test_display_repos_table_shows_automation_note(self):
        """Test that summary includes automation exclusion note."""
        # Create a console that captures output
        output = io.StringIO()
        console = Console(file=output, force_terminal=False, width=80)

        with patch("source_clone.reset_manager.GitHubAPI"):
            manager = ResetManager(
                org="test-org",
                github_token="fake-token",
                console=console,
                include_automation_prs=False,
            )

            repos = {
                "test-repo": GitHubRepoStatus(
                    name="test-repo",
                    full_name="test-org/test-repo",
                    url="https://github.com/test-org/test-repo",
                    open_prs=5,
                    open_issues=2,
                    last_commit_sha="abc123",
                    last_commit_date="2025-01-18",
                    default_branch="main",
                )
            }

            # Execute
            manager.display_repos_table(repos)

            # Verify summary includes automation note
            output_text = output.getvalue()
            assert "excluding automation" in output_text

    def test_display_repos_table_no_automation_note_when_included(self):
        """Test that summary does not include automation note when flag is set."""
        # Create a console that captures output
        output = io.StringIO()
        console = Console(file=output, force_terminal=False, width=80)

        with patch("source_clone.reset_manager.GitHubAPI"):
            manager = ResetManager(
                org="test-org",
                github_token="fake-token",
                console=console,
                include_automation_prs=True,
            )

            repos = {
                "test-repo": GitHubRepoStatus(
                    name="test-repo",
                    full_name="test-org/test-repo",
                    url="https://github.com/test-org/test-repo",
                    open_prs=5,
                    open_issues=2,
                    last_commit_sha="abc123",
                    last_commit_date="2025-01-18",
                    default_branch="main",
                )
            }

            # Execute
            manager.display_repos_table(repos)

            # Verify summary does not include automation note
            output_text = output.getvalue()
            assert "excluding automation" not in output_text

    def test_validate_repo_name_valid(self, reset_manager):
        """Test validation of valid repository names."""
        # Valid names
        assert reset_manager._validate_repo_name("valid-repo")[0]
        assert reset_manager._validate_repo_name("repo123")[0]
        assert reset_manager._validate_repo_name("my_repo")[0]
        assert reset_manager._validate_repo_name("repo.name")[0]
        assert reset_manager._validate_repo_name("a")[0]
        assert reset_manager._validate_repo_name("repo-with-dashes")[0]
        assert reset_manager._validate_repo_name("repo_with_underscores")[0]
        assert reset_manager._validate_repo_name("repo.with.dots")[0]
        assert reset_manager._validate_repo_name("MixedCase123")[0]

    def test_validate_repo_name_dot_prefixed(self, reset_manager):
        """Test validation accepts dot-prefixed repository names like .github."""
        # .github is a standard GitHub org-level config/profile repository
        assert reset_manager._validate_repo_name(".github")[0]
        # Other dot-prefixed names should also be valid
        assert reset_manager._validate_repo_name(".config")[0]
        assert reset_manager._validate_repo_name(".dotfile-repo")[0]

        # But a bare dot or double-dot prefix is not valid
        assert not reset_manager._validate_repo_name(".")[0]
        assert not reset_manager._validate_repo_name("..")[0]
        assert not reset_manager._validate_repo_name("..github")[0]

    def test_validate_repo_name_invalid_empty(self, reset_manager):
        """Test validation rejects empty repository names."""
        is_valid, error = reset_manager._validate_repo_name("")
        assert not is_valid
        assert "empty" in error.lower()

        is_valid, error = reset_manager._validate_repo_name("   ")
        assert not is_valid
        assert "empty" in error.lower()

    def test_validate_repo_name_invalid_too_long(self, reset_manager):
        """Test validation rejects names over 100 characters."""
        long_name = "a" * 101
        is_valid, error = reset_manager._validate_repo_name(long_name)
        assert not is_valid
        assert "100 characters" in error

    def test_validate_repo_name_invalid_special_chars(self, reset_manager):
        """Test validation rejects invalid special characters."""
        # Names starting with hyphen or underscore
        assert not reset_manager._validate_repo_name("-starts-with-dash")[0]
        assert not reset_manager._validate_repo_name("_starts-with-underscore")[0]

        # Names ending with special chars
        assert not reset_manager._validate_repo_name("ends-with-dash-")[0]
        assert not reset_manager._validate_repo_name("ends-with-underscore_")[0]
        assert not reset_manager._validate_repo_name("ends-with-dot.")[0]

        # Invalid characters
        assert not reset_manager._validate_repo_name("repo@name")[0]
        assert not reset_manager._validate_repo_name("repo#name")[0]
        assert not reset_manager._validate_repo_name("repo name")[0]
        assert not reset_manager._validate_repo_name("repo/name")[0]

    @pytest.mark.asyncio
    async def test_delete_all_repos_validates_names(self, reset_manager):
        """Test that delete_all_repos validates repository names."""
        repo_names = [
            "valid-repo",
            "",  # Invalid: empty
            "-invalid-start",  # Invalid: starts with dash
            "valid-repo-2",
            "repo with spaces",  # Invalid: spaces
        ]

        reset_manager.github_api.batch_delete_repos = AsyncMock(
            return_value={
                "valid-repo": (True, None),
                "valid-repo-2": (True, None),
            }
        )

        results = await reset_manager.delete_all_repos(repo_names)

        # Should only attempt to delete valid repos
        reset_manager.github_api.batch_delete_repos.assert_called_once()
        call_args = reset_manager.github_api.batch_delete_repos.call_args
        assert set(call_args[1]["repo_names"]) == {"valid-repo", "valid-repo-2"}

        # Results should include all repos with appropriate errors
        assert len(results) == 5
        assert results["valid-repo"][0]  # Success
        assert results["valid-repo-2"][0]  # Success
        assert not results[""][0]  # Failed - invalid
        assert not results["-invalid-start"][0]  # Failed - invalid
        assert not results["repo with spaces"][0]  # Failed - invalid

    def test_format_commit_date_iso8601_with_timezone(self, reset_manager):
        """Test formatting ISO 8601 date with timezone."""
        date_str = "2025-01-18T12:34:56Z"
        result = reset_manager._format_commit_date(date_str)
        assert result == "2025-01-18"

    def test_format_commit_date_iso8601_without_timezone(self, reset_manager):
        """Test formatting ISO 8601 date without timezone."""
        date_str = "2025-01-18T12:34:56"
        result = reset_manager._format_commit_date(date_str)
        assert result == "2025-01-18"

    def test_format_commit_date_space_separated(self, reset_manager):
        """Test formatting space-separated date format."""
        date_str = "2025-01-18 12:34:56"
        result = reset_manager._format_commit_date(date_str)
        assert result == "2025-01-18"

    def test_format_commit_date_date_only(self, reset_manager):
        """Test formatting date-only format."""
        date_str = "2025-01-18"
        result = reset_manager._format_commit_date(date_str)
        assert result == "2025-01-18"

    def test_format_commit_date_empty_string(self, reset_manager):
        """Test formatting empty string returns N/A."""
        result = reset_manager._format_commit_date("")
        assert result == "N/A"

        result = reset_manager._format_commit_date("   ")
        assert result == "N/A"

    def test_format_commit_date_malformed_safe_truncation(self, reset_manager):
        """Test safe truncation for malformed but date-like strings."""
        # String that looks like a date but doesn't parse
        date_str = "2025-01-18T99:99:99Z"
        result = reset_manager._format_commit_date(date_str)
        assert result == "2025-01-18"

    def test_format_commit_date_short_string(self, reset_manager):
        """Test handling of short strings."""
        date_str = "2025-01"
        result = reset_manager._format_commit_date(date_str)
        assert result == "2025-01"

    def test_format_commit_date_completely_invalid(self, reset_manager):
        """Test handling of completely invalid date strings."""
        date_str = "not-a-date"
        result = reset_manager._format_commit_date(date_str)
        # Should return truncated or N/A
        assert result in ["not-a-date", "not-a-date"[:10]]

    def test_format_commit_date_with_whitespace(self, reset_manager):
        """Test formatting date with leading/trailing whitespace."""
        date_str = "  2025-01-18T12:34:56Z  "
        result = reset_manager._format_commit_date(date_str)
        assert result == "2025-01-18"

    @pytest.mark.asyncio
    async def test_fetch_repos_handles_api_errors_gracefully(self, reset_manager):
        """Test that API errors are handled gracefully with proper error indication."""
        # Setup mock responses
        reset_manager.github_api.list_all_repos_graphql.return_value = {
            "test-repo": {
                "name": "test-repo",
                "full_name": "test-org/test-repo",
                "html_url": "https://github.com/test-org/test-repo",
            }
        }

        # Mock _request_paginated to raise an API error
        reset_manager.github_api._request_paginated.side_effect = GitHubAPIError(
            "API rate limit exceeded"
        )

        # Execute with Live mocked
        with patch("source_clone.reset_manager.Live"):
            repos_data = await reset_manager._fetch_repos_with_graphql()

        # Verify - should mark counts as unavailable (-1) rather than 0
        assert repos_data["test-repo"]["open_prs"] == -1
        assert repos_data["test-repo"]["open_issues"] == -1

    @pytest.mark.asyncio
    async def test_fetch_repos_handles_not_found_error(self, reset_manager):
        """Test that NotFound errors are handled as info-level logs."""
        # Setup mock responses
        reset_manager.github_api.list_all_repos_graphql.return_value = {
            "test-repo": {
                "name": "test-repo",
                "full_name": "test-org/test-repo",
                "html_url": "https://github.com/test-org/test-repo",
            }
        }

        # Mock _request_paginated to raise NotFound error
        reset_manager.github_api._request_paginated.side_effect = GitHubNotFoundError(
            "Repository not found"
        )

        # Execute with Live mocked
        with patch("source_clone.reset_manager.Live"):
            repos_data = await reset_manager._fetch_repos_with_graphql()

        # Verify - should mark counts as unavailable
        assert repos_data["test-repo"]["open_prs"] == -1
        assert repos_data["test-repo"]["open_issues"] == -1

    def test_display_repos_table_shows_question_mark_for_unavailable(self):
        """Test that unavailable PR/issue counts show as '?' instead of a number."""
        # Create a console that captures output
        output = io.StringIO()
        console = Console(file=output, force_terminal=False, width=80)

        with patch("source_clone.reset_manager.GitHubAPI"):
            manager = ResetManager(
                org="test-org",
                github_token="fake-token",
                console=console,
                include_automation_prs=False,
            )

            repos = {
                "test-repo": GitHubRepoStatus(
                    name="test-repo",
                    full_name="test-org/test-repo",
                    url="https://github.com/test-org/test-repo",
                    open_prs=-1,  # Unavailable
                    open_issues=-1,  # Unavailable
                    last_commit_sha="abc123",
                    last_commit_date="2025-01-18",
                    default_branch="main",
                )
            }

            # Execute
            total_prs, total_issues = manager.display_repos_table(repos)

            # Verify output contains "?" for unavailable counts
            output_text = output.getvalue()
            assert "?" in output_text

            # Verify totals don't include unavailable repos
            assert total_prs == 0
            assert total_issues == 0

    def test_generate_confirmation_hash_deterministic(self, reset_manager):
        """Test that confirmation hash is deterministic based on org state."""
        # Same inputs should produce same hash
        hash1 = reset_manager.generate_confirmation_hash(
            repo_count=5,
            total_prs=10,
            total_issues=20,
        )
        hash2 = reset_manager.generate_confirmation_hash(
            repo_count=5,
            total_prs=10,
            total_issues=20,
        )
        assert hash1 == hash2
        assert len(hash1) == 16

        # Different inputs should produce different hash
        hash3 = reset_manager.generate_confirmation_hash(
            repo_count=6,  # Different count
            total_prs=10,
            total_issues=20,
        )
        assert hash1 != hash3

    def test_generate_confirmation_hash_format(self, reset_manager):
        """Test that confirmation hash uses clear alphanumeric characters."""
        hash_code = reset_manager.generate_confirmation_hash(
            repo_count=5,
            total_prs=10,
            total_issues=20,
        )

        # Should be 16 characters
        assert len(hash_code) == 16

        # Should only contain clear characters (no 0, O, 1, l, i)
        ambiguous_chars = "0O1li"
        for char in hash_code:
            assert char not in ambiguous_chars

        # Should be lowercase alphanumeric
        assert hash_code.isalnum()
        assert hash_code.islower()

    @pytest.mark.asyncio
    async def test_fetch_repos_skip_pr_issue_counts(self, reset_manager):
        """Test that skip_pr_issue_counts=True skips REST API calls entirely.

        When --no-confirm is used, we only need the repository list from
        GraphQL and should not make expensive per-repo REST calls for
        PR/issue counts.
        """
        reset_manager.github_api.list_all_repos_graphql.return_value = {
            "repo-a": {
                "name": "repo-a",
                "full_name": "test-org/repo-a",
                "html_url": "https://github.com/test-org/repo-a",
                "last_commit_date": "2025-01-18T12:34:56Z",
            },
            "repo-b": {
                "name": "repo-b",
                "full_name": "test-org/repo-b",
                "html_url": "https://github.com/test-org/repo-b",
                "last_commit_date": None,
            },
        }

        repos_data = await reset_manager._fetch_repos_with_graphql(
            skip_pr_issue_counts=True
        )

        # Should return the repos without modification
        assert len(repos_data) == 2
        assert "repo-a" in repos_data
        assert "repo-b" in repos_data

        # _request_paginated should never have been called
        reset_manager.github_api._request_paginated.assert_not_called()

    @pytest.mark.asyncio
    async def test_fetch_repos_default_fetches_pr_issue_counts(self, reset_manager):
        """Test that skip_pr_issue_counts=False (default) does fetch PR/issue counts."""
        reset_manager.github_api.list_all_repos_graphql.return_value = {
            "test-repo": {
                "name": "test-repo",
                "full_name": "test-org/test-repo",
                "html_url": "https://github.com/test-org/test-repo",
            }
        }

        mock_prs = [
            {"user": {"login": "human-user"}, "number": 1},
        ]
        mock_issues: list[Any] = []

        reset_manager.github_api._request_paginated.side_effect = [
            mock_prs,
            mock_issues,
        ]

        with patch("source_clone.reset_manager.Live"):
            repos_data = await reset_manager._fetch_repos_with_graphql(
                skip_pr_issue_counts=False
            )

        # Should have made REST API calls
        assert reset_manager.github_api._request_paginated.call_count == 2
        assert repos_data["test-repo"]["open_prs"] == 1

    @pytest.mark.asyncio
    async def test_execute_reset_no_confirm_skips_table_and_pr_fetching(
        self, reset_manager
    ):
        """Test that execute_reset with no_confirm=True skips table display and PR fetching.

        When --no-confirm is provided, the tabulated output of PRs/issues is
        superfluous and the time to gather that data is wasted.
        """
        # Mock scan to return repos
        mock_repos = {
            "repo-a": GitHubRepoStatus(
                name="repo-a",
                full_name="test-org/repo-a",
                url="https://github.com/test-org/repo-a",
                open_prs=0,
                open_issues=0,
                last_commit_sha=None,
                last_commit_date=None,
                default_branch="main",
            ),
        }

        with (
            patch.object(
                reset_manager,
                "scan_github_organization",
                new_callable=AsyncMock,
                return_value=mock_repos,
            ) as mock_scan,
            patch.object(
                reset_manager,
                "display_repos_table",
                return_value=(0, 0),
            ) as mock_table,
            patch.object(
                reset_manager,
                "delete_all_repos",
                new_callable=AsyncMock,
                return_value={"repo-a": (True, None)},
            ),
        ):
            result = await reset_manager.execute_reset(no_confirm=True)

            # scan_github_organization should be called with skip_pr_issue_counts=True
            mock_scan.assert_awaited_once_with(skip_pr_issue_counts=True)

            # display_repos_table should NOT be called
            mock_table.assert_not_called()

            assert result.deleted_repos == 1
            assert result.total_prs == 0
            assert result.total_issues == 0

    @pytest.mark.asyncio
    async def test_execute_reset_with_confirm_shows_table_and_fetches_prs(
        self, reset_manager
    ):
        """Test that execute_reset with no_confirm=False displays the table and fetches data."""
        mock_repos = {
            "repo-a": GitHubRepoStatus(
                name="repo-a",
                full_name="test-org/repo-a",
                url="https://github.com/test-org/repo-a",
                open_prs=3,
                open_issues=1,
                last_commit_sha="abc123",
                last_commit_date="2025-06-01T10:00:00Z",
                default_branch="main",
            ),
        }

        with (
            patch.object(
                reset_manager,
                "scan_github_organization",
                new_callable=AsyncMock,
                return_value=mock_repos,
            ) as mock_scan,
            patch.object(
                reset_manager,
                "display_repos_table",
                return_value=(3, 1),
            ) as mock_table,
            patch.object(
                reset_manager,
                "prompt_for_confirmation",
                return_value=False,
            ),
        ):
            result = await reset_manager.execute_reset(no_confirm=False)

            # scan should NOT skip PR/issue counts
            mock_scan.assert_awaited_once_with(skip_pr_issue_counts=False)

            # display_repos_table SHOULD be called
            mock_table.assert_called_once_with(mock_repos)

            # User declined confirmation, so nothing deleted
            assert result.deleted_repos == 0
            assert result.total_prs == 3
            assert result.total_issues == 1

    def test_display_repos_table_renders_commit_date(self, reset_manager, mock_console):
        """Test that display_repos_table renders last_commit_date from GraphQL data.

        The GraphQL committedDate field should flow through to the table's
        'Last Commit' column.
        """
        repos = {
            "repo-with-date": GitHubRepoStatus(
                name="repo-with-date",
                full_name="test-org/repo-with-date",
                url="https://github.com/test-org/repo-with-date",
                open_prs=0,
                open_issues=0,
                last_commit_sha="abc123",
                last_commit_date="2025-06-15T14:30:00Z",
                default_branch="main",
            ),
            "repo-no-date": GitHubRepoStatus(
                name="repo-no-date",
                full_name="test-org/repo-no-date",
                url="https://github.com/test-org/repo-no-date",
                open_prs=0,
                open_issues=0,
                last_commit_sha=None,
                last_commit_date=None,
                default_branch="main",
            ),
        }

        total_prs, total_issues = reset_manager.display_repos_table(repos)

        output = mock_console.file.getvalue()

        # Repo with a commit date should show the formatted date
        assert "2025-06-15" in output

        # Repo without a commit date should show N/A
        assert "N/A" in output

        assert total_prs == 0
        assert total_issues == 0

    @pytest.mark.asyncio
    async def test_scan_github_organization_passes_skip_flag(self, reset_manager):
        """Test that scan_github_organization passes skip_pr_issue_counts to _fetch_repos_with_graphql."""
        reset_manager.github_api.list_all_repos_graphql.return_value = {
            "repo-a": {
                "name": "repo-a",
                "full_name": "test-org/repo-a",
                "html_url": "https://github.com/test-org/repo-a",
                "last_commit_date": None,
            },
        }

        with patch.object(
            reset_manager,
            "_fetch_repos_with_graphql",
            new_callable=AsyncMock,
            return_value={
                "repo-a": {
                    "name": "repo-a",
                    "full_name": "test-org/repo-a",
                    "html_url": "https://github.com/test-org/repo-a",
                    "last_commit_date": None,
                },
            },
        ) as mock_fetch:
            await reset_manager.scan_github_organization(skip_pr_issue_counts=True)
            mock_fetch.assert_awaited_once_with(skip_pr_issue_counts=True)

            mock_fetch.reset_mock()
            await reset_manager.scan_github_organization(skip_pr_issue_counts=False)
            mock_fetch.assert_awaited_once_with(skip_pr_issue_counts=False)

    @pytest.mark.asyncio
    async def test_execute_reset_no_confirm_still_deletes_repos(self, reset_manager):
        """Test that execute_reset with no_confirm=True proceeds directly to deletion."""
        mock_repos = {
            "repo-x": GitHubRepoStatus(
                name="repo-x",
                full_name="test-org/repo-x",
                url="https://github.com/test-org/repo-x",
                open_prs=0,
                open_issues=0,
                last_commit_sha=None,
                last_commit_date=None,
                default_branch="main",
            ),
            "repo-y": GitHubRepoStatus(
                name="repo-y",
                full_name="test-org/repo-y",
                url="https://github.com/test-org/repo-y",
                open_prs=0,
                open_issues=0,
                last_commit_sha=None,
                last_commit_date=None,
                default_branch="main",
            ),
        }

        with (
            patch.object(
                reset_manager,
                "scan_github_organization",
                new_callable=AsyncMock,
                return_value=mock_repos,
            ),
            patch.object(
                reset_manager,
                "delete_all_repos",
                new_callable=AsyncMock,
                return_value={
                    "repo-x": (True, None),
                    "repo-y": (True, None),
                },
            ) as mock_delete,
        ):
            result = await reset_manager.execute_reset(no_confirm=True)

            # Should have called delete with both repo names
            mock_delete.assert_awaited_once()
            deleted_names = mock_delete.call_args[0][0]
            assert set(deleted_names) == {"repo-x", "repo-y"}

            assert result.deleted_repos == 2
            assert result.total_repos == 2
