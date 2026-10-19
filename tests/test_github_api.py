# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2025 Matthew Watkins <mwatkins@linuxfoundation.org>

"""Unit tests for GitHub API module."""

from __future__ import annotations

import asyncio
import time as _time
from typing import Any
from unittest.mock import AsyncMock, Mock, patch

import httpx
import pytest

from source_clone.github_api import (
    GitHubAPI,
    GitHubAPIError,
    GitHubAuthError,
    GitHubNotFoundError,
    GitHubRateLimitError,
    GitHubRepo,
    get_default_org_or_user,
    sanitize_description,
    transform_gerrit_name_to_github,
)
from source_clone.github_transport import github_api_url, graphql_url_for
from source_clone.rate_limit import TokenBucketLimiter


class TestSanitizeDescription:
    """Test description sanitization."""

    def test_none_returns_none(self) -> None:
        """Test that None input returns None."""
        assert sanitize_description(None) is None

    def test_empty_string_returns_none(self) -> None:
        """Test that empty string returns None."""
        assert sanitize_description("") is None
        assert sanitize_description("   ") is None

    def test_removes_control_characters(self) -> None:
        """Test removal of control characters."""
        # Newlines
        result = sanitize_description("Line 1\nLine 2")
        assert result == "Line 1 Line 2"

        # Tabs
        result = sanitize_description("Tab\there")
        assert result == "Tab here"

        # Carriage returns
        result = sanitize_description("Text\rwith\rCR")
        assert result == "Text with CR"

    def test_replaces_multiple_spaces(self) -> None:
        """Test that multiple spaces are collapsed."""
        result = sanitize_description("Too    many     spaces")
        assert result == "Too many spaces"

    def test_trims_whitespace(self) -> None:
        """Test that leading/trailing whitespace is removed."""
        result = sanitize_description("  Trimmed  ")
        assert result == "Trimmed"

    def test_truncates_long_descriptions(self) -> None:
        """Test that descriptions longer than 350 chars are truncated."""
        long_desc = "a" * 400
        result = sanitize_description(long_desc)
        assert result is not None
        assert len(result) == 350
        assert result.endswith("...")

    def test_normal_description_unchanged(self) -> None:
        """Test that normal descriptions pass through."""
        desc = "A normal repository description"
        result = sanitize_description(desc)
        assert result == desc

    def test_preserves_double_quotes(self) -> None:
        """Test that double quotes are preserved."""
        result = sanitize_description('Description with "quotes" inside')
        assert result == 'Description with "quotes" inside'

    def test_mixed_issues(self) -> None:
        """Test description with multiple issues."""
        desc = "  Line 1\n\nLine 2\t\tTab  "
        result = sanitize_description(desc)
        assert result == "Line 1 Line 2 Tab"

    def test_real_world_cert_service_case(self) -> None:
        """Test description similar to oom/platform/cert-service."""
        desc = 'OOM Cert Service "Certificate Authority" setup'
        result = sanitize_description(desc)
        assert result == 'OOM Cert Service "Certificate Authority" setup'
        assert '"' in result  # Quotes should be preserved


class TestTransformGerritNameToGitHub:
    """Test Gerrit name to GitHub transformation."""

    def test_simple_name_no_change(self) -> None:
        """Test simple names remain unchanged."""
        assert transform_gerrit_name_to_github("ccsdk") == "ccsdk"

    def test_single_level_hierarchy(self) -> None:
        """Test single level hierarchy transformation."""
        assert transform_gerrit_name_to_github("ccsdk/apps") == "ccsdk-apps"

    def test_multi_level_hierarchy(self) -> None:
        """Test multi-level hierarchy transformation."""
        result = transform_gerrit_name_to_github("ccsdk/features/test")
        assert result == "ccsdk-features-test"

    def test_deep_hierarchy(self) -> None:
        """Test deep hierarchy transformation."""
        result = transform_gerrit_name_to_github("project/sub/subsub/deep")
        assert result == "project-sub-subsub-deep"


class TestGitHubRepo:
    """Test GitHubRepo dataclass."""

    def test_from_api_response(self) -> None:
        """Test creating GitHubRepo from API response."""
        api_data = {
            "name": "test-repo",
            "full_name": "org/test-repo",
            "html_url": "https://github.com/org/test-repo",
            "clone_url": "https://github.com/org/test-repo.git",
            "ssh_url": "git@github.com:org/test-repo.git",
            "private": False,
            "description": "Test repository",
            "default_branch": "main",
        }

        repo = GitHubRepo.from_api_response(api_data)

        assert repo.name == "test-repo"
        assert repo.full_name == "org/test-repo"
        assert repo.html_url == "https://github.com/org/test-repo"
        assert repo.clone_url == "https://github.com/org/test-repo.git"
        assert repo.ssh_url == "git@github.com:org/test-repo.git"
        assert repo.private is False
        assert repo.description == "Test repository"
        assert repo.default_branch == "main"

    def test_from_api_response_no_description(self) -> None:
        """Test creating GitHubRepo without description."""
        api_data = {
            "name": "test-repo",
            "full_name": "org/test-repo",
            "html_url": "https://github.com/org/test-repo",
            "clone_url": "https://github.com/org/test-repo.git",
            "ssh_url": "git@github.com:org/test-repo.git",
            "private": True,
        }

        repo = GitHubRepo.from_api_response(api_data)

        assert repo.name == "test-repo"
        assert repo.description is None
        assert repo.private is True
        assert repo.default_branch is None

    def test_from_api_response_with_default_branch(self) -> None:
        """Test that default_branch is captured from API response."""
        api_data = {
            "name": "test-repo",
            "full_name": "org/test-repo",
            "html_url": "https://github.com/org/test-repo",
            "clone_url": "https://github.com/org/test-repo.git",
            "ssh_url": "git@github.com:org/test-repo.git",
            "private": False,
            "default_branch": "develop",
        }

        repo = GitHubRepo.from_api_response(api_data)

        assert repo.default_branch == "develop"

    def test_default_branch_field_defaults_to_none(self) -> None:
        """Test that GitHubRepo.default_branch defaults to None."""
        repo = GitHubRepo(
            name="test-repo",
            full_name="org/test-repo",
            html_url="https://github.com/org/test-repo",
            clone_url="https://github.com/org/test-repo.git",
            ssh_url="git@github.com:org/test-repo.git",
            private=False,
        )

        assert repo.default_branch is None

    def test_from_api_response_captures_archived(self) -> None:
        """Test that the REST archived flag is captured from API response."""
        api_data = {
            "name": "test-repo",
            "full_name": "org/test-repo",
            "html_url": "https://github.com/org/test-repo",
            "clone_url": "https://github.com/org/test-repo.git",
            "ssh_url": "git@github.com:org/test-repo.git",
            "private": False,
            "archived": True,
        }

        repo = GitHubRepo.from_api_response(api_data)

        assert repo.archived is True

    def test_archived_field_defaults_to_false(self) -> None:
        """Test that GitHubRepo.archived defaults to False."""
        repo = GitHubRepo(
            name="test-repo",
            full_name="org/test-repo",
            html_url="https://github.com/org/test-repo",
            clone_url="https://github.com/org/test-repo.git",
            ssh_url="git@github.com:org/test-repo.git",
            private=False,
        )

        assert repo.archived is False


class TestGitHubAPI:
    """Test GitHubAPI client."""

    def test_init_with_token(self) -> None:
        """Test initialization with explicit token."""
        api = GitHubAPI(token="test-token")
        assert api.token == "test-token"
        api.close()

    def test_init_without_token_raises_error(self) -> None:
        """Test initialization without token raises error."""
        with (
            patch.dict("os.environ", {}, clear=True),
            pytest.raises(GitHubAuthError),
        ):
            GitHubAPI()

    def test_init_with_env_token(self) -> None:
        """Test initialization with environment variable token."""
        with patch.dict("os.environ", {"GITHUB_TOKEN": "env-token"}):
            api = GitHubAPI()
            assert api.token == "env-token"
            api.close()

    def test_context_manager(self) -> None:
        """Test GitHubAPI as context manager."""
        with GitHubAPI(token="test-token") as api:
            assert api.token == "test-token"

    def test_defaults_to_github_com(self) -> None:
        """Without a base URL the client targets api.github.com."""
        with GitHubAPI(token="test-token") as api:
            assert api.base_url == "https://api.github.com"
            assert api.graphql_url == "https://api.github.com/graphql"

    def test_enterprise_base_url(self) -> None:
        """An Enterprise base URL sets both REST and GraphQL endpoints."""
        with GitHubAPI(
            token="test-token", base_url="https://ghe.example.com/api/v3/"
        ) as api:
            assert api.base_url == "https://ghe.example.com/api/v3"
            assert api.graphql_url == "https://ghe.example.com/api/graphql"


class TestGitHubApiUrls:
    """Host to API endpoint mapping."""

    @pytest.mark.parametrize(
        ("host", "expected"),
        [
            ("github.com", "https://api.github.com"),
            ("github.com/org", "https://api.github.com"),
            ("https://github.com/org", "https://api.github.com"),
            ("GitHub.com/org", "https://api.github.com"),
            ("github.com:443/org", "https://api.github.com"),
            ("https://github.com:443/org", "https://api.github.com"),
            ("ghe.example.com/org", "https://ghe.example.com/api/v3"),
            ("https://ghe.example.com:8443/org", "https://ghe.example.com:8443/api/v3"),
            ("http://ghe.internal/org", "http://ghe.internal/api/v3"),
            ("github.com.evil.example/org", "https://github.com.evil.example/api/v3"),
        ],
    )
    def test_github_api_url(self, host: str, expected: str) -> None:
        """Each host maps to its own REST API base URL."""
        assert github_api_url(host) == expected

    @pytest.mark.parametrize(
        ("api_url", "expected"),
        [
            ("https://api.github.com", "https://api.github.com/graphql"),
            ("https://ghe.example.com/api/v3", "https://ghe.example.com/api/graphql"),
            ("https://ghe.example.com/api/v3/", "https://ghe.example.com/api/graphql"),
        ],
    )
    def test_graphql_url_for(self, api_url: str, expected: str) -> None:
        """GraphQL pairs with the REST API on the same host."""
        assert graphql_url_for(api_url) == expected

    @patch("source_clone.github_api.httpx.Client")
    def test_repo_exists_true(self, mock_client: Mock) -> None:
        """Test repo_exists returns True when repo exists."""
        mock_response = Mock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"name": "test-repo"}

        mock_client_instance = Mock()
        mock_client_instance.request.return_value = mock_response
        mock_client.return_value = mock_client_instance

        api = GitHubAPI(token="test-token")
        result = api.repo_exists("owner", "test-repo")

        assert result is True
        api.close()

    @patch("source_clone.github_api.httpx.Client")
    def test_repo_exists_false(self, mock_client: Mock) -> None:
        """Test repo_exists returns False when repo not found."""
        mock_response = Mock()
        mock_response.status_code = 404

        mock_client_instance = Mock()
        mock_client_instance.request.return_value = mock_response
        mock_client.return_value = mock_client_instance

        api = GitHubAPI(token="test-token")
        result = api.repo_exists("owner", "test-repo")

        assert result is False
        api.close()


class TestHandleResponseErrors:
    """Test error handling with GitHub API responses."""

    def test_rate_limit_detected_by_header(self) -> None:
        """Test rate limit detection using X-RateLimit-Remaining header."""
        api = GitHubAPI(token="test-token")

        mock_response = Mock()
        mock_response.status_code = 403
        mock_response.headers = {"X-RateLimit-Remaining": "0"}
        mock_response.text = "Forbidden"

        with pytest.raises(GitHubRateLimitError, match="rate limit exceeded"):
            api._handle_response_errors(mock_response, "/test/endpoint")

        api.close()

    def test_rate_limit_detected_by_retry_after_header(self) -> None:
        """Test rate limit detection using Retry-After header."""
        api = GitHubAPI(token="test-token")

        mock_response = Mock()
        mock_response.status_code = 403
        mock_response.headers = {"Retry-After": "60"}
        mock_response.text = "Forbidden"

        with pytest.raises(GitHubRateLimitError, match="Retry after 60 seconds"):
            api._handle_response_errors(mock_response, "/test/endpoint")

        api.close()

    def test_rate_limit_detected_by_text_fallback(self) -> None:
        """Test rate limit detection falls back to text matching."""
        api = GitHubAPI(token="test-token")

        mock_response = Mock()
        mock_response.status_code = 403
        mock_response.headers = {}
        mock_response.text = "API rate limit exceeded for user"

        with pytest.raises(GitHubRateLimitError, match="rate limit exceeded"):
            api._handle_response_errors(mock_response, "/test/endpoint")

        api.close()

    def test_403_without_rate_limit_raises_generic_error(self) -> None:
        """Test 403 without rate limit indicators raises generic APIError."""
        api = GitHubAPI(token="test-token")

        mock_response = Mock()
        mock_response.status_code = 403
        mock_response.headers = {}
        mock_response.text = "Access denied for other reasons"

        with pytest.raises(GitHubAPIError, match="Forbidden"):
            api._handle_response_errors(mock_response, "/test/endpoint")

        api.close()

    def test_401_raises_auth_error(self) -> None:
        """Test 401 raises authentication error."""
        api = GitHubAPI(token="test-token")

        mock_response = Mock()
        mock_response.status_code = 401
        mock_response.text = "Bad credentials"

        with pytest.raises(GitHubAuthError, match="Authentication failed"):
            api._handle_response_errors(mock_response, "/test/endpoint")

        api.close()

    def test_404_raises_not_found_error(self) -> None:
        """Test 404 raises not found error."""
        api = GitHubAPI(token="test-token")

        mock_response = Mock()
        mock_response.status_code = 404
        mock_response.text = "Not found"

        with pytest.raises(GitHubNotFoundError, match="Resource not found"):
            api._handle_response_errors(mock_response, "/test/endpoint")

        api.close()


class TestGetDefaultOrgOrUser:
    """Test get_default_org_or_user function."""

    @patch.object(GitHubAPI, "get_user_orgs")
    @patch.object(GitHubAPI, "get_authenticated_user")
    def test_returns_first_org(self, mock_get_user: Mock, mock_get_orgs: Mock) -> None:
        """Test returns first organization when available."""
        mock_get_orgs.return_value = [{"login": "test-org"}]

        api = GitHubAPI(token="test-token")
        owner, is_org = get_default_org_or_user(api)

        assert owner == "test-org"
        assert is_org is True
        api.close()

    @patch.object(GitHubAPI, "get_user_orgs")
    @patch.object(GitHubAPI, "get_authenticated_user")
    def test_returns_user_when_no_orgs(
        self, mock_get_user: Mock, mock_get_orgs: Mock
    ) -> None:
        """Test returns user when no organizations available."""
        mock_get_orgs.return_value = []
        mock_get_user.return_value = {"login": "test-user"}

        api = GitHubAPI(token="test-token")
        owner, is_org = get_default_org_or_user(api)

        assert owner == "test-user"
        assert is_org is False
        api.close()


class TestBatchDeleteRepos:
    """Test batch_delete_repos async method."""

    @pytest.mark.asyncio
    async def test_batch_delete_success(self) -> None:
        """Test successful batch deletion of repositories."""
        api = GitHubAPI(token="test-token")

        # Mock httpx.AsyncClient to return successful responses.
        # Use Mock (not AsyncMock) for the response so that
        # response.headers.get() returns a plain Mock instead of
        # an unawaited coroutine — avoids RuntimeWarning from
        # budget.update_from_headers().
        mock_response = Mock()
        mock_response.status_code = 204
        mock_response.headers = {}

        mock_client = AsyncMock()
        mock_client.delete = AsyncMock(return_value=mock_response)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)

        with (
            patch(
                "source_clone.github_api.httpx.AsyncClient",
                return_value=mock_client,
            ),
            patch(
                "source_clone.github_api.asyncio.sleep",
                new_callable=AsyncMock,
            ),
        ):
            repo_names = ["repo1", "repo2", "repo3"]
            results = await api.batch_delete_repos(
                "test-org", repo_names, rate_limit_interval=0.0
            )

            assert len(results) == 3
            assert all(success for success, _ in results.values())
            assert mock_client.delete.call_count == 3

        api.close()

    @pytest.mark.asyncio
    async def test_batch_delete_partial_failure(self) -> None:
        """Test batch deletion with some failures."""
        api = GitHubAPI(token="test-token")

        # Mock delete to fail for repo2 with a non-rate-limit 403
        # (plain "Permission denied" without "rate limit" in the text)
        call_count = [0]

        async def mock_delete_side_effect(*args: Any, **kwargs: Any):
            call_count[0] += 1
            response = Mock()  # Use Mock, not AsyncMock for response
            response.headers = {}
            if "repo2" in args[0]:
                response.status_code = 403
                response.text = "Permission denied"
            else:
                response.status_code = 204
            return response

        mock_client = AsyncMock()
        mock_client.delete = AsyncMock(side_effect=mock_delete_side_effect)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)

        with (
            patch(
                "source_clone.github_api.httpx.AsyncClient",
                return_value=mock_client,
            ),
            patch(
                "source_clone.github_api.asyncio.sleep",
                new_callable=AsyncMock,
            ),
        ):
            repo_names = ["repo1", "repo2", "repo3"]
            results = await api.batch_delete_repos(
                "test-org", repo_names, rate_limit_interval=0.0
            )

            assert len(results) == 3
            assert results["repo1"][0] is True
            assert results["repo2"][0] is False
            assert results["repo3"][0] is True

        api.close()

    @pytest.mark.asyncio
    async def test_batch_delete_respects_concurrency_limit(self) -> None:
        """Test that batch deletion respects max_concurrent limit."""
        api = GitHubAPI(token="test-token")

        max_concurrent = 3
        in_flight = 0
        peak_in_flight = 0

        async def mock_delete_with_delay(*args: Any, **kwargs: Any):
            nonlocal in_flight, peak_in_flight
            in_flight += 1
            peak_in_flight = max(peak_in_flight, in_flight)
            # Yield control so other tasks can enter concurrently;
            # multiple yields increase the chance of overlap.
            await asyncio.sleep(0)
            await asyncio.sleep(0)
            in_flight -= 1
            response = Mock()
            response.status_code = 204
            return response

        mock_client = AsyncMock()
        mock_client.delete = AsyncMock(side_effect=mock_delete_with_delay)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)

        with (
            patch(
                "source_clone.github_api.httpx.AsyncClient",
                return_value=mock_client,
            ),
            patch(
                "source_clone.github_api.asyncio.sleep",
                new_callable=AsyncMock,
            ),
        ):
            repo_names = [f"repo{i}" for i in range(10)]
            results = await api.batch_delete_repos(
                "test-org",
                repo_names,
                max_concurrent=max_concurrent,
                rate_limit_interval=0.0,
            )

            assert len(results) == 10
            assert all(success for success, _ in results.values())
            # The semaphore should have capped concurrency.
            # With 10 tasks and max_concurrent=3 the peak must
            # never exceed the limit.
            assert peak_in_flight <= max_concurrent, (
                f"Peak in-flight {peak_in_flight} exceeded "
                f"max_concurrent {max_concurrent}"
            )

        api.close()

    @pytest.mark.asyncio
    async def test_batch_delete_handles_exceptions(self) -> None:
        """Test that exceptions during deletion are handled.

        With retry logic, repo2 will be retried up to max_retries times
        before finally failing.  We pass rate_limit_interval=0.0 and
        patch asyncio.sleep so retries are instantaneous in tests.
        """
        api = GitHubAPI(token="test-token")

        async def mock_delete_with_exception(*args: Any, **kwargs: Any):
            if "repo2" in args[0]:
                raise Exception("Network error")
            response = Mock()  # Use Mock, not AsyncMock for response
            response.status_code = 204
            response.headers = {}
            return response

        mock_client = AsyncMock()
        mock_client.delete = AsyncMock(side_effect=mock_delete_with_exception)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)

        with (
            patch(
                "source_clone.github_api.httpx.AsyncClient",
                return_value=mock_client,
            ),
            patch(
                "source_clone.github_api.asyncio.sleep",
                new_callable=AsyncMock,
            ),
        ):
            repo_names = ["repo1", "repo2", "repo3"]
            results = await api.batch_delete_repos(
                "test-org", repo_names, rate_limit_interval=0.0
            )

            # repo2 retries then fails; repo1 and repo3 succeed
            assert len(results) == 3
            assert results["repo1"][0] is True
            assert results["repo2"][0] is False
            assert results["repo2"][1] is not None
            assert "Network error" in results["repo2"][1]
            assert results["repo3"][0] is True

        api.close()


class TestBatchCreateRepos:
    """Test batch_create_repos async method."""

    @pytest.mark.asyncio
    async def test_batch_create_success(self) -> None:
        """Test successful batch creation of repositories."""
        api = GitHubAPI(token="test-token")

        # Mock httpx.AsyncClient to return successful responses
        call_count = [0]

        async def mock_post(*args: Any, **kwargs: Any):
            call_count[0] += 1
            response = Mock()  # Use Mock, not AsyncMock for response
            response.status_code = 201
            response.headers = {}
            json_data = kwargs.get("json", {})
            name = json_data.get("name", f"repo{call_count[0]}")
            response.json = Mock(
                return_value={
                    "name": name,
                    "full_name": f"test-org/{name}",
                    "html_url": f"https://github.com/test-org/{name}",
                    "clone_url": f"https://github.com/test-org/{name}.git",
                    "ssh_url": f"git@github.com:test-org/{name}.git",
                    "private": False,
                }
            )
            return response

        mock_client = AsyncMock()
        mock_client.post = AsyncMock(side_effect=mock_post)
        mock_client.get = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)

        with (
            patch(
                "source_clone.github_api.httpx.AsyncClient",
                return_value=mock_client,
            ),
            patch(
                "source_clone.github_api.asyncio.sleep",
                new_callable=AsyncMock,
            ),
        ):
            repos_to_create = [
                {"name": "repo1", "description": "Test 1"},
                {"name": "repo2", "description": "Test 2"},
            ]
            results = await api.batch_create_repos(
                "test-org",
                repos_to_create,
                rate_limit_interval=0.0,
            )

            assert len(results) == 2
            assert results["repo1"][0] is not None
            assert results["repo1"][0].name == "repo1"
            assert results["repo2"][0] is not None
            assert results["repo2"][0].name == "repo2"

        api.close()

    @pytest.mark.asyncio
    async def test_batch_create_partial_failure(self) -> None:
        """Test batch creation with some failures."""
        api = GitHubAPI(token="test-token")

        async def mock_post_with_failure(*args: Any, **kwargs: Any):
            json_data = kwargs.get("json", {})
            name = json_data.get("name", "")
            response = Mock()  # Use Mock, not AsyncMock for response
            response.headers = {}

            if name == "repo2":
                response.status_code = 422
                response.text = "Repository already exists"
            else:
                response.status_code = 201
                response.json = Mock(
                    return_value={
                        "name": name,
                        "full_name": f"test-org/{name}",
                        "html_url": f"https://github.com/test-org/{name}",
                        "clone_url": f"https://github.com/test-org/{name}.git",
                        "ssh_url": f"git@github.com:test-org/{name}.git",
                        "private": False,
                    }
                )
            return response

        # For the 422 follow-up GET, return a 404 so the creation is
        # still recorded as a failure.  Use Mock (not AsyncMock) with
        # headers={} so budget.update_from_headers() doesn't receive
        # an AsyncMock (which would produce unawaited-coroutine
        # RuntimeWarnings from .headers.get()).
        mock_get_response = Mock()
        mock_get_response.status_code = 404
        mock_get_response.headers = {}

        mock_client = AsyncMock()
        mock_client.post = AsyncMock(side_effect=mock_post_with_failure)
        mock_client.get = AsyncMock(return_value=mock_get_response)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)

        with (
            patch(
                "source_clone.github_api.httpx.AsyncClient",
                return_value=mock_client,
            ),
            patch(
                "source_clone.github_api.asyncio.sleep",
                new_callable=AsyncMock,
            ),
        ):
            repos_to_create = [
                {"name": "repo1"},
                {"name": "repo2"},
                {"name": "repo3"},
            ]
            results = await api.batch_create_repos(
                "test-org",
                repos_to_create,
                rate_limit_interval=0.0,
            )

            assert len(results) == 3
            assert results["repo1"][0] is not None
            assert results["repo2"][0] is None
            error_msg = results["repo2"][1]
            assert error_msg is not None and "already exists" in error_msg.lower()
            assert results["repo3"][0] is not None

        api.close()

    @pytest.mark.asyncio
    async def test_batch_create_respects_concurrency_limit(self) -> None:
        """Test that batch creation respects max_concurrent limit."""
        api = GitHubAPI(token="test-token")

        call_count = [0]

        async def mock_post_with_delay(*args: Any, **kwargs: Any):
            call_count[0] += 1
            response = Mock()  # Use Mock, not AsyncMock for response
            response.status_code = 201
            response.headers = {}
            json_data = kwargs.get("json", {})
            name = json_data.get("name", f"repo{call_count[0]}")
            response.json = Mock(
                return_value={
                    "name": name,
                    "full_name": f"test-org/{name}",
                    "html_url": f"https://github.com/test-org/{name}",
                    "clone_url": f"https://github.com/test-org/{name}.git",
                    "ssh_url": f"git@github.com:test-org/{name}.git",
                    "private": False,
                }
            )
            return response

        mock_client = AsyncMock()
        mock_client.post = AsyncMock(side_effect=mock_post_with_delay)
        mock_client.get = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)

        with (
            patch(
                "source_clone.github_api.httpx.AsyncClient",
                return_value=mock_client,
            ),
            patch(
                "source_clone.github_api.asyncio.sleep",
                new_callable=AsyncMock,
            ),
        ):
            repos_to_create = [{"name": f"repo{i}"} for i in range(10)]
            results = await api.batch_create_repos(
                "test-org",
                repos_to_create,
                max_concurrent=3,
                rate_limit_interval=0.0,
            )

            assert len(results) == 10
            assert all(repo is not None for repo, _ in results.values())

        api.close()

    @pytest.mark.asyncio
    async def test_batch_create_handles_exceptions(self) -> None:
        """Test that exceptions during creation are handled.

        With retry logic, repo2 will be retried up to max_retries times
        before finally failing.  We pass rate_limit_interval=0.0 and
        patch asyncio.sleep so retries are instantaneous in tests.
        """
        api = GitHubAPI(token="test-token")

        async def mock_post_with_exception(*args: Any, **kwargs: Any):
            json_data = kwargs.get("json", {})
            name = json_data.get("name", "")

            if name == "repo2":
                raise Exception("Network timeout")

            response = Mock()  # Use Mock, not AsyncMock for response
            response.status_code = 201
            response.headers = {}
            response.json = Mock(
                return_value={
                    "name": name,
                    "full_name": f"test-org/{name}",
                    "html_url": f"https://github.com/test-org/{name}",
                    "clone_url": f"https://github.com/test-org/{name}.git",
                    "ssh_url": f"git@github.com:test-org/{name}.git",
                    "private": False,
                }
            )
            return response

        mock_client = AsyncMock()
        mock_client.post = AsyncMock(side_effect=mock_post_with_exception)
        mock_client.get = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)

        with (
            patch(
                "source_clone.github_api.httpx.AsyncClient",
                return_value=mock_client,
            ),
            patch(
                "source_clone.github_api.asyncio.sleep",
                new_callable=AsyncMock,
            ),
        ):
            repos_to_create = [
                {"name": "repo1"},
                {"name": "repo2"},
                {"name": "repo3"},
            ]
            results = await api.batch_create_repos(
                "test-org",
                repos_to_create,
                rate_limit_interval=0.0,
            )

            # repo2 retries then fails; repo1 and repo3 succeed
            assert len(results) == 3
            assert results["repo1"][0] is not None
            assert results["repo2"][0] is None
            assert results["repo2"][1] is not None
            assert "Network timeout" in results["repo2"][1]
            assert results["repo3"][0] is not None

        api.close()


class TestListAllReposGraphQL:
    """Test list_all_repos_graphql method."""

    def test_list_all_repos_empty_org(self) -> None:
        """Test listing repos for org with no repositories."""
        api = GitHubAPI(token="test-token")

        mock_response = Mock()
        mock_response.json.return_value = {
            "data": {
                "organization": {
                    "repositories": {
                        "nodes": [],
                        "pageInfo": {
                            "hasNextPage": False,
                            "endCursor": None,
                        },
                    },
                },
            },
        }

        with patch.object(api.client, "post", return_value=mock_response):
            result = api.list_all_repos_graphql("test-org")

            assert len(result) == 0

        api.close()

    def test_list_all_repos_single_page(self) -> None:
        """Test listing repos with single page of results."""
        api = GitHubAPI(token="test-token")

        mock_response = Mock()
        mock_response.json.return_value = {
            "data": {
                "organization": {
                    "repositories": {
                        "nodes": [
                            {
                                "name": "repo1",
                                "nameWithOwner": "test-org/repo1",
                                "url": "https://github.com/test-org/repo1",
                                "sshUrl": "git@github.com:test-org/repo1.git",
                                "isPrivate": False,
                                "description": "Test repo 1",
                                "defaultBranchRef": {"name": "main"},
                            },
                            {
                                "name": "repo2",
                                "nameWithOwner": "test-org/repo2",
                                "url": "https://github.com/test-org/repo2",
                                "sshUrl": "git@github.com:test-org/repo2.git",
                                "isPrivate": True,
                                "description": None,
                                "defaultBranchRef": None,
                            },
                        ],
                        "pageInfo": {
                            "hasNextPage": False,
                            "endCursor": None,
                        },
                    },
                },
            },
        }

        with patch.object(api.client, "post", return_value=mock_response):
            result = api.list_all_repos_graphql("test-org")

            assert len(result) == 2
            assert "repo1" in result
            assert "repo2" in result
            assert result["repo1"]["name"] == "repo1"
            assert result["repo1"]["full_name"] == "test-org/repo1"
            assert result["repo1"]["private"] is False
            assert result["repo2"]["private"] is True

        api.close()

    def test_list_all_repos_carries_archived_flag(self) -> None:
        """Test that the query selects isArchived and results carry it."""
        api = GitHubAPI(token="test-token")

        mock_response = Mock()
        mock_response.json.return_value = {
            "data": {
                "organization": {
                    "repositories": {
                        "nodes": [
                            {
                                "name": "live",
                                "nameWithOwner": "test-org/live",
                                "url": "https://github.com/test-org/live",
                                "sshUrl": "git@github.com:test-org/live.git",
                                "isPrivate": False,
                                "isArchived": False,
                                "description": None,
                                "defaultBranchRef": {"name": "main"},
                            },
                            {
                                "name": "old",
                                "nameWithOwner": "test-org/old",
                                "url": "https://github.com/test-org/old",
                                "sshUrl": "git@github.com:test-org/old.git",
                                "isPrivate": False,
                                "isArchived": True,
                                "description": None,
                                "defaultBranchRef": {"name": "main"},
                            },
                        ],
                        "pageInfo": {
                            "hasNextPage": False,
                            "endCursor": None,
                        },
                    },
                },
            },
        }

        with patch.object(api.client, "post", return_value=mock_response) as mock_post:
            result = api.list_all_repos_graphql("test-org")

            query = mock_post.call_args[1]["json"]["query"]
            assert "isArchived" in query
            assert result["live"]["archived"] is False
            assert result["old"]["archived"] is True

        api.close()

    def test_list_all_repos_pagination(self) -> None:
        """Test listing repos with pagination."""
        api = GitHubAPI(token="test-token")

        # First page response
        first_response = Mock()
        first_response.json.return_value = {
            "data": {
                "organization": {
                    "repositories": {
                        "nodes": [
                            {
                                "name": "repo1",
                                "nameWithOwner": "test-org/repo1",
                                "url": "https://github.com/test-org/repo1",
                                "sshUrl": "git@github.com:test-org/repo1.git",
                                "isPrivate": False,
                                "description": "Repo 1",
                                "defaultBranchRef": {"name": "main"},
                            },
                        ],
                        "pageInfo": {
                            "hasNextPage": True,
                            "endCursor": "cursor123",
                        },
                    },
                },
            },
        }

        # Second page response
        second_response = Mock()
        second_response.json.return_value = {
            "data": {
                "organization": {
                    "repositories": {
                        "nodes": [
                            {
                                "name": "repo2",
                                "nameWithOwner": "test-org/repo2",
                                "url": "https://github.com/test-org/repo2",
                                "sshUrl": "git@github.com:test-org/repo2.git",
                                "isPrivate": False,
                                "description": "Repo 2",
                                "defaultBranchRef": {"name": "main"},
                            },
                        ],
                        "pageInfo": {
                            "hasNextPage": False,
                            "endCursor": None,
                        },
                    },
                },
            },
        }

        with patch.object(
            api.client, "post", side_effect=[first_response, second_response]
        ) as mock_post:
            result = api.list_all_repos_graphql("test-org")

            assert len(result) == 2
            assert "repo1" in result
            assert "repo2" in result
            # Verify pagination worked - should have been called twice
            assert mock_post.call_count == 2

        api.close()

    def test_list_all_repos_handles_graphql_errors(self) -> None:
        """Test handling of GraphQL errors."""
        api = GitHubAPI(token="test-token")

        mock_response = Mock()
        mock_response.json.return_value = {
            "errors": [
                {"message": "Organization not found"},
            ],
        }

        with patch.object(api.client, "post", return_value=mock_response):
            result = api.list_all_repos_graphql("nonexistent-org")

            # Should return empty dict on error
            assert len(result) == 0

        api.close()

    def test_list_all_repos_handles_missing_org(self) -> None:
        """Test handling when organization data is missing."""
        api = GitHubAPI(token="test-token")

        mock_response = Mock()
        mock_response.json.return_value = {
            "data": {
                "organization": None,
            },
        }

        with patch.object(api.client, "post", return_value=mock_response):
            result = api.list_all_repos_graphql("missing-org")

            assert len(result) == 0

        api.close()

    def test_list_all_repos_escapes_special_chars(self) -> None:
        """Test that organization names with special characters are escaped."""
        api = GitHubAPI(token="test-token")

        mock_response = Mock()
        mock_response.json.return_value = {
            "data": {
                "organization": {
                    "repositories": {
                        "nodes": [],
                        "pageInfo": {
                            "hasNextPage": False,
                            "endCursor": None,
                        },
                    },
                },
            },
        }

        with patch.object(api.client, "post", return_value=mock_response) as mock_post:
            # Org name with quotes should be escaped
            api.list_all_repos_graphql('test"org')

            # Verify the query was made and org name was escaped
            assert mock_post.called
            call_args = mock_post.call_args
            query = call_args[1]["json"]["query"]
            # The escaped version should be in the query
            assert 'test\\"org' in query

        api.close()


def _owner_response(
    owner_type: str, names: list[str], *, has_next: bool = False
) -> Mock:
    """A repositoryOwner GraphQL response listing *names*."""
    response = Mock()
    response.json.return_value = {
        "data": {
            "repositoryOwner": {
                "__typename": owner_type,
                "repositories": {
                    "nodes": [
                        {
                            "name": name,
                            "nameWithOwner": f"owner/{name}",
                            "url": f"https://github.com/owner/{name}",
                            "sshUrl": f"git@github.com:owner/{name}.git",
                            "isPrivate": False,
                            "isArchived": False,
                            "defaultBranchRef": {"name": "main"},
                        }
                        for name in names
                    ],
                    "pageInfo": {
                        "hasNextPage": has_next,
                        "endCursor": "cursor1" if has_next else None,
                    },
                },
            },
        },
    }
    return response


class TestListOwnerReposGraphQL:
    """Test list_owner_repos_graphql method."""

    @pytest.mark.parametrize("owner_type", ["Organization", "User"])
    def test_lists_an_owners_repositories(self, owner_type: str) -> None:
        """Users and organizations are listed the same way."""
        with (
            GitHubAPI(token="test-token") as api,
            patch.object(
                api.client, "post", return_value=_owner_response(owner_type, ["r1"])
            ),
        ):
            result = api.list_owner_repos_graphql("owner")

        assert list(result) == ["r1"]
        assert result["r1"]["full_name"] == "owner/r1"

    def test_pages_with_the_cursor_as_a_variable(self) -> None:
        """Each later page sends the previous end cursor as a variable."""
        responses = [
            _owner_response("User", ["r1"], has_next=True),
            _owner_response("User", ["r2"]),
        ]
        with (
            GitHubAPI(token="test-token") as api,
            patch.object(api.client, "post", side_effect=responses) as mock_post,
        ):
            result = api.list_owner_repos_graphql("owner")

        assert sorted(result) == ["r1", "r2"]
        variables = [c.kwargs["json"]["variables"] for c in mock_post.call_args_list]
        assert variables == [
            {"login": "owner", "cursor": None},
            {"login": "owner", "cursor": "cursor1"},
        ]

    def test_unknown_login_raises_not_found(self) -> None:
        """An unknown login raises at once, without retrying."""
        response = Mock()
        response.json.return_value = {"data": {"repositoryOwner": None}}
        with (
            GitHubAPI(token="test-token") as api,
            patch.object(api.client, "post", return_value=response) as mock_post,
            pytest.raises(GitHubNotFoundError, match="nobody"),
        ):
            api.list_owner_repos_graphql("nobody")

        assert mock_post.call_count == 1

    def test_rejected_token_raises_at_once(self) -> None:
        """A 401 is an authentication error, not an empty account."""
        response = Mock(status_code=401, headers={})
        with (
            GitHubAPI(token="bad-token") as api,
            patch.object(api.client, "post", return_value=response) as mock_post,
            patch("source_clone.github_graphql_retry.time_mod.sleep") as sleep,
            pytest.raises(GitHubAuthError),
        ):
            api.list_owner_repos_graphql("owner")

        assert mock_post.call_count == 1
        sleep.assert_not_called()

    def test_exhausted_retries_raise_with_the_cause(self) -> None:
        """A page that keeps failing raises rather than returning {}."""
        response = Mock(status_code=502, headers={})
        with (
            GitHubAPI(token="test-token") as api,
            patch.object(api.client, "post", return_value=response),
            patch("source_clone.github_graphql_retry.time_mod.sleep"),
            pytest.raises(GitHubAPIError, match="HTTP 502"),
        ):
            api.list_owner_repos_graphql("owner")

    def test_org_listing_keeps_returning_empty_on_failure(self) -> None:
        """mirror and reset keep their existing failure handling."""
        response = Mock(status_code=401, headers={})
        response.raise_for_status.side_effect = httpx.HTTPStatusError(
            "401 Unauthorized", request=Mock(), response=Mock()
        )
        with (
            GitHubAPI(token="bad-token") as api,
            patch.object(api.client, "post", return_value=response),
            patch("source_clone.github_graphql_retry.time_mod.sleep"),
        ):
            assert api.list_all_repos_graphql("some-org") == {}

    def test_org_listing_still_empty_for_a_user(self) -> None:
        """list_all_repos_graphql still lists organizations only.

        mirror and reset rely on it, and reset deletes what it lists.
        """
        response = Mock()
        response.json.return_value = {"data": {"organization": None}}
        with (
            GitHubAPI(token="test-token") as api,
            patch.object(api.client, "post", return_value=response) as mock_post,
        ):
            result = api.list_all_repos_graphql("someuser")

        assert result == {}
        assert "organization(login:" in mock_post.call_args.kwargs["json"]["query"]


class TestRequestPaginated:
    """Test _request_paginated method for handling GitHub API pagination."""

    def test_single_page_response(self) -> None:
        """Test pagination with single page of results."""
        api = GitHubAPI(token="test-token")

        # Mock response with no Link header (single page)
        mock_response = Mock()
        mock_response.status_code = 200
        mock_response.json.return_value = [{"id": 1}, {"id": 2}, {"id": 3}]
        mock_response.headers.get.return_value = ""  # No Link header
        mock_response.raise_for_status = Mock()

        with patch.object(api.client, "request", return_value=mock_response):
            results = api._request_paginated("GET", "/test/endpoint")

        assert len(results) == 3
        assert results[0]["id"] == 1
        assert results[1]["id"] == 2
        assert results[2]["id"] == 3

        api.close()

    def test_multiple_pages_response(self) -> None:
        """Test pagination with multiple pages of results."""
        api = GitHubAPI(token="test-token")

        # Mock responses for 3 pages
        page1_response = Mock()
        page1_response.status_code = 200
        page1_response.json.return_value = [{"id": 1}, {"id": 2}]
        page1_response.headers.get.return_value = (
            '<https://api.github.com/test?page=2>; rel="next"'
        )
        page1_response.raise_for_status = Mock()

        page2_response = Mock()
        page2_response.status_code = 200
        page2_response.json.return_value = [{"id": 3}, {"id": 4}]
        page2_response.headers.get.return_value = (
            '<https://api.github.com/test?page=3>; rel="next"'
        )
        page2_response.raise_for_status = Mock()

        page3_response = Mock()
        page3_response.status_code = 200
        page3_response.json.return_value = [{"id": 5}]
        page3_response.headers.get.return_value = ""  # No next page
        page3_response.raise_for_status = Mock()

        with patch.object(
            api.client,
            "request",
            side_effect=[page1_response, page2_response, page3_response],
        ):
            results = api._request_paginated("GET", "/test/endpoint")

        assert len(results) == 5
        assert results[0]["id"] == 1
        assert results[4]["id"] == 5

        api.close()

    def test_empty_page_stops_pagination(self) -> None:
        """Test that empty page stops pagination."""
        api = GitHubAPI(token="test-token")

        # Mock response with empty data
        mock_response = Mock()
        mock_response.status_code = 200
        mock_response.json.return_value = []
        mock_response.headers.get.return_value = ""
        mock_response.raise_for_status = Mock()

        with patch.object(api.client, "request", return_value=mock_response):
            results = api._request_paginated("GET", "/test/endpoint")

        assert len(results) == 0

        api.close()

    def test_max_pages_limit(self) -> None:
        """Test that max_pages parameter limits pagination."""
        api = GitHubAPI(token="test-token")

        # Mock responses for multiple pages
        page_response = Mock()
        page_response.status_code = 200
        page_response.json.return_value = [{"id": 1}, {"id": 2}]
        page_response.headers.get.return_value = (
            '<https://api.github.com/test?page=2>; rel="next"'
        )
        page_response.raise_for_status = Mock()

        with patch.object(api.client, "request", return_value=page_response):
            results = api._request_paginated("GET", "/test/endpoint", max_pages=2)

        # Should only fetch 2 pages (4 items)
        assert len(results) == 4

        api.close()

    def test_custom_per_page(self) -> None:
        """Test that per_page parameter is passed correctly."""
        api = GitHubAPI(token="test-token")

        mock_response = Mock()
        mock_response.status_code = 200
        mock_response.json.return_value = [{"id": 1}]
        mock_response.headers.get.return_value = ""
        mock_response.raise_for_status = Mock()

        with patch.object(
            api.client, "request", return_value=mock_response
        ) as mock_request:
            api._request_paginated("GET", "/test/endpoint", per_page=50)

        # Verify per_page parameter was passed
        call_args = mock_request.call_args
        assert call_args[1]["params"]["per_page"] == 50

        api.close()

    def test_additional_params_preserved(self) -> None:
        """Test that additional parameters are preserved across pages."""
        api = GitHubAPI(token="test-token")

        mock_response = Mock()
        mock_response.status_code = 200
        mock_response.json.return_value = [{"id": 1}]
        mock_response.headers.get.return_value = ""
        mock_response.raise_for_status = Mock()

        with patch.object(
            api.client, "request", return_value=mock_response
        ) as mock_request:
            api._request_paginated(
                "GET", "/test/endpoint", params={"state": "open", "sort": "created"}
            )

        # Verify custom params are included
        call_args = mock_request.call_args
        assert call_args[1]["params"]["state"] == "open"
        assert call_args[1]["params"]["sort"] == "created"

        api.close()

    def test_params_not_mutated(self) -> None:
        """Test that original params dict is not mutated during pagination."""
        api = GitHubAPI(token="test-token")

        # Create params dict to pass to the method
        original_params = {"state": "open", "sort": "created"}
        params_copy = original_params.copy()

        mock_response = Mock()
        mock_response.status_code = 200
        mock_response.json.return_value = [{"id": 1}]
        mock_response.headers.get.return_value = ""
        mock_response.raise_for_status = Mock()

        with patch.object(api.client, "request", return_value=mock_response):
            api._request_paginated("GET", "/test/endpoint", params=original_params)

        # Verify original params dict was not mutated
        assert original_params == params_copy
        assert "per_page" not in original_params
        assert "page" not in original_params

        api.close()

    def test_non_list_response_stops_pagination(self) -> None:
        """Test that non-list response stops pagination."""
        api = GitHubAPI(token="test-token")

        # Mock response with dict instead of list
        mock_response = Mock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"error": "test"}
        mock_response.headers.get.return_value = ""
        mock_response.raise_for_status = Mock()

        with patch.object(api.client, "request", return_value=mock_response):
            results = api._request_paginated("GET", "/test/endpoint")

        assert len(results) == 0

        api.close()


def test_list_all_repos_no_default_branch() -> None:
    """Test listing repos when a repository has no default branch configured.

    Repos without a default branch should still appear in results (with
    ``None`` values) and a single summary warning should be emitted
    instead of one warning per repo.
    """
    api = GitHubAPI(token="test-token")

    mock_response = Mock()
    mock_response.json.return_value = {
        "data": {
            "organization": {
                "repositories": {
                    "nodes": [
                        {
                            "name": "repo-with-branch",
                            "nameWithOwner": "test-org/repo-with-branch",
                            "url": "https://github.com/test-org/repo-with-branch",
                            "sshUrl": "git@github.com:test-org/repo-with-branch.git",
                            "isPrivate": False,
                            "description": "Repo with default branch",
                            "defaultBranchRef": {
                                "name": "main",
                                "target": {
                                    "oid": "abc123def456",
                                    "committedDate": "2025-01-18T12:34:56Z",
                                },
                            },
                        },
                        {
                            "name": "repo-no-branch",
                            "nameWithOwner": "test-org/repo-no-branch",
                            "url": "https://github.com/test-org/repo-no-branch",
                            "sshUrl": "git@github.com:test-org/repo-no-branch.git",
                            "isPrivate": False,
                            "description": "Repo without default branch",
                            "defaultBranchRef": None,  # No default branch
                        },
                    ],
                    "pageInfo": {
                        "hasNextPage": False,
                        "endCursor": None,
                    },
                },
            },
        },
    }

    with (
        patch.object(api.client, "post", return_value=mock_response),
        patch("source_clone.github_repo_listing.logger") as mock_logger,
    ):
        result = api.list_all_repos_graphql("test-org")

        # Should have both repos
        assert len(result) == 2
        assert "repo-with-branch" in result
        assert "repo-no-branch" in result

        # Repo with branch should have commit SHA and date
        assert result["repo-with-branch"]["default_branch"] == "main"
        assert result["repo-with-branch"]["latest_commit_sha"] == "abc123def456"
        assert result["repo-with-branch"]["last_commit_date"] == "2025-01-18T12:34:56Z"

        # Repo without branch should have None values
        assert result["repo-no-branch"]["default_branch"] is None
        assert result["repo-no-branch"]["latest_commit_sha"] is None
        assert result["repo-no-branch"]["last_commit_date"] is None

        # Individual repos should only produce DEBUG-level messages now
        debug_calls = [
            call
            for call in mock_logger.debug.call_args_list
            if len(call[0]) >= 1 and "no default branch configured" in str(call[0][0])
        ]
        assert len(debug_calls) == 1  # one debug entry for repo-no-branch

        # A single summary INFO should be emitted (not WARNING) listing
        # the affected repo names so operators can distinguish Gerrit
        # parent projects from genuinely broken repos.
        info_calls = [
            call
            for call in mock_logger.info.call_args_list
            if len(call[0]) >= 1 and "no default branch configured" in str(call[0][0])
        ]
        assert len(info_calls) == 1
        # Logger receives (format_string, arg1, arg2, ...) — check the
        # positional args that will be interpolated via %-formatting.
        summary_fmt = info_calls[0][0][0]
        summary_positional = info_calls[0][0][1:]
        # First two positional args are the counts: (1, 2)
        assert summary_positional[0] == 1  # 1 repo without default branch
        assert summary_positional[1] == 2  # 2 total repos
        # Third positional arg is the comma-separated repo names
        assert summary_positional[2] == "repo-no-branch"
        # Message should mention the condition and reference Gerrit parent projects
        assert "no default branch configured" in summary_fmt
        assert "Gerrit parent project" in summary_fmt

        # No WARNING should be emitted for this condition
        warning_calls = [
            call
            for call in mock_logger.warning.call_args_list
            if len(call[0]) >= 1 and "no default branch configured" in str(call[0][0])
        ]
        assert len(warning_calls) == 0

    api.close()


def test_list_all_repos_committed_date_absent() -> None:
    """Test that last_commit_date is None when committedDate is absent from GraphQL response.

    Older mock data or repos whose target fragment lacks committedDate should
    still produce a valid entry with last_commit_date set to None rather than
    raising an error.
    """
    api = GitHubAPI(token="test-token")

    mock_response = Mock()
    mock_response.json.return_value = {
        "data": {
            "organization": {
                "repositories": {
                    "nodes": [
                        {
                            "name": "repo-no-date",
                            "nameWithOwner": "test-org/repo-no-date",
                            "url": "https://github.com/test-org/repo-no-date",
                            "sshUrl": "git@github.com:test-org/repo-no-date.git",
                            "isPrivate": False,
                            "description": "Repo with commit SHA but no date",
                            "defaultBranchRef": {
                                "name": "main",
                                "target": {
                                    "oid": "deadbeef1234",
                                    # committedDate intentionally absent
                                },
                            },
                        },
                    ],
                    "pageInfo": {
                        "hasNextPage": False,
                        "endCursor": None,
                    },
                },
            },
        },
    }

    with patch.object(api.client, "post", return_value=mock_response):
        result = api.list_all_repos_graphql("test-org")

        assert len(result) == 1
        assert result["repo-no-date"]["latest_commit_sha"] == "deadbeef1234"
        assert result["repo-no-date"]["last_commit_date"] is None

    api.close()


def test_list_all_repos_committed_date_present() -> None:
    """Test that last_commit_date is extracted from the GraphQL committedDate field."""
    api = GitHubAPI(token="test-token")

    mock_response = Mock()
    mock_response.json.return_value = {
        "data": {
            "organization": {
                "repositories": {
                    "nodes": [
                        {
                            "name": "repo-with-date",
                            "nameWithOwner": "test-org/repo-with-date",
                            "url": "https://github.com/test-org/repo-with-date",
                            "sshUrl": "git@github.com:test-org/repo-with-date.git",
                            "isPrivate": False,
                            "description": "Repo with full commit metadata",
                            "defaultBranchRef": {
                                "name": "develop",
                                "target": {
                                    "oid": "cafe0123babe",
                                    "committedDate": "2024-12-25T08:00:00Z",
                                },
                            },
                        },
                    ],
                    "pageInfo": {
                        "hasNextPage": False,
                        "endCursor": None,
                    },
                },
            },
        },
    }

    with patch.object(api.client, "post", return_value=mock_response):
        result = api.list_all_repos_graphql("test-org")

        assert len(result) == 1
        repo = result["repo-with-date"]
        assert repo["latest_commit_sha"] == "cafe0123babe"
        assert repo["last_commit_date"] == "2024-12-25T08:00:00Z"
        assert repo["default_branch"] == "develop"

    api.close()


class TestSetDefaultBranch:
    """Test GitHubAPI.set_default_branch method."""

    def test_set_default_branch_success(self) -> None:
        """Test successfully setting the default branch."""
        with GitHubAPI(token="test-token") as api:
            mock_response = Mock()
            mock_response.status_code = 200
            mock_response.json.return_value = {
                "default_branch": "main",
                "name": "test-repo",
            }
            mock_response.content = b'{"default_branch": "main"}'
            mock_response.headers = {}

            with patch.object(api.client, "request", return_value=mock_response):
                result = api.set_default_branch("test-org", "test-repo", "main")

            assert result is True

    def test_set_default_branch_not_found(self) -> None:
        """Test setting default branch on a non-existent repository returns False."""
        with GitHubAPI(token="test-token") as api:
            mock_response = Mock()
            mock_response.status_code = 404
            mock_response.text = "Not Found"
            mock_response.headers = {}

            with patch.object(api.client, "request", return_value=mock_response):
                result = api.set_default_branch("test-org", "missing-repo", "main")

            assert result is False

    def test_set_default_branch_api_error(self) -> None:
        """Test handling of a generic API error when setting default branch."""
        with GitHubAPI(token="test-token") as api:
            mock_response = Mock()
            mock_response.status_code = 422
            mock_response.text = "Validation Failed"
            mock_response.headers = {}

            with patch.object(api.client, "request", return_value=mock_response):
                result = api.set_default_branch(
                    "test-org", "test-repo", "nonexistent-branch"
                )

            assert result is False

    def test_set_default_branch_sends_correct_request(self) -> None:
        """Test that set_default_branch sends the right HTTP method and payload."""
        with GitHubAPI(token="test-token") as api:
            mock_response = Mock()
            mock_response.status_code = 200
            mock_response.json.return_value = {
                "default_branch": "develop",
                "name": "test-repo",
            }
            mock_response.content = b'{"default_branch": "develop"}'
            mock_response.headers = {}

            with patch.object(
                api.client, "request", return_value=mock_response
            ) as mock_request:
                api.set_default_branch("test-org", "test-repo", "develop")

                mock_request.assert_called_once_with(
                    "PATCH",
                    "https://api.github.com/repos/test-org/test-repo",
                    json={"default_branch": "develop"},
                )


class TestTokenBucketLimiterFromGitHubAPI:
    """Test TokenBucketLimiter rate reduction and recovery.

    These tests replace the old _AsyncRateLimiter tests and verify the
    new token-bucket behaviour: rate halving on 403, time-based
    recovery, cascading rate reductions, and min-rate clamping.
    """

    @pytest.mark.asyncio
    async def test_rate_limit_halves_rate(self) -> None:
        """Recording a rate limit should halve the refill rate."""
        limiter = TokenBucketLimiter(rate=1.0, burst=10, min_rate=0.01)
        assert limiter.rate == 1.0

        await limiter.record_rate_limit()
        assert limiter.rate == 0.5

    @pytest.mark.asyncio
    async def test_rate_never_below_min(self) -> None:
        """Rate must never drop below min_rate."""
        limiter = TokenBucketLimiter(rate=1.0, burst=10, min_rate=0.5)
        await limiter.record_rate_limit()
        assert limiter.rate == 0.5  # Halved to exactly min

        await limiter.record_rate_limit()
        assert limiter.rate == 0.5  # Clamped at min

    @pytest.mark.asyncio
    async def test_cascading_rate_reductions(self) -> None:
        """Multiple rate-limit hits should keep halving the rate."""
        limiter = TokenBucketLimiter(rate=2.0, burst=10, min_rate=0.1)

        await limiter.record_rate_limit()
        assert limiter.rate == 1.0

        await limiter.record_rate_limit()
        assert limiter.rate == 0.5

        await limiter.record_rate_limit()
        assert limiter.rate == 0.25

        await limiter.record_rate_limit()
        assert limiter.rate == 0.125

        # Next halving would be 0.0625, but min_rate is 0.1
        await limiter.record_rate_limit()
        assert limiter.rate >= 0.1

    @pytest.mark.asyncio
    async def test_rate_limit_drains_bucket(self) -> None:
        """Recording a rate limit should empty the token bucket."""
        limiter = TokenBucketLimiter(rate=1.0, burst=10)
        assert limiter.tokens == 10.0

        await limiter.record_rate_limit()
        assert limiter.tokens == 0.0

    @pytest.mark.asyncio
    async def test_time_based_recovery_full(self) -> None:
        """Rate should fully recover after recovery_seconds elapse."""
        limiter = TokenBucketLimiter(
            rate=1.0, burst=5, min_rate=0.1, recovery_seconds=0.2
        )
        await limiter.record_rate_limit()
        assert limiter.rate < 1.0

        # Simulate time passing beyond recovery_seconds
        async with limiter._lock:
            limiter._last_rate_limit_time = _time.monotonic() - 0.3
            limiter._tokens = 5.0  # Refill manually for acquire

        # Trigger _refill which checks recovery
        await limiter.acquire(tokens=1.0)
        assert limiter.rate == 1.0

    @pytest.mark.asyncio
    async def test_no_recovery_before_threshold(self) -> None:
        """Rate should not recover before 50% of recovery_seconds."""
        limiter = TokenBucketLimiter(
            rate=1.0, burst=5, min_rate=0.1, recovery_seconds=10.0
        )
        await limiter.record_rate_limit()
        reduced = limiter.rate

        # Simulate only 30% of recovery elapsed — no recovery yet
        async with limiter._lock:
            limiter._last_rate_limit_time = _time.monotonic() - 3.0
            limiter._tokens = 5.0

        await limiter.acquire(tokens=1.0)
        assert limiter.rate == reduced

    @pytest.mark.asyncio
    async def test_record_success_is_noop(self) -> None:
        """record_success should not change the rate (recovery is time-based)."""
        limiter = TokenBucketLimiter(rate=1.0, burst=5)
        original = limiter.rate

        for _ in range(20):
            await limiter.record_success()
        assert limiter.rate == original

    @pytest.mark.asyncio
    async def test_retry_after_sets_global_pause(self) -> None:
        """Retry-After should set a global pause for all tasks."""
        limiter = TokenBucketLimiter(rate=10.0, burst=10)
        await limiter.record_rate_limit(retry_after=30.0)

        # The global retry deadline should be set
        assert limiter._global_retry_until > 0
        assert limiter.tokens == 0.0
