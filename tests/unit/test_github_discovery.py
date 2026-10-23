# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2025 Matthew Watkins <mwatkins@linuxfoundation.org>

"""Unit tests for GitHub repository discovery."""

from __future__ import annotations

from unittest.mock import MagicMock, Mock, patch

import pytest

from source_clone.github_api import GitHubAPI
from source_clone.github_discovery import (
    _apply_filters,
    _convert_to_projects,
    _extract_org_from_host,
    detect_github_source,
    discover_github_repositories,
    parse_github_url,
)
from source_clone.models import Config, Project, ProjectState, SourceType


def _graphql_node(name: str, *, archived: bool) -> dict[str, object]:
    """Build a GraphQL repository node as GitHub returns it."""
    return {
        "name": name,
        "nameWithOwner": f"testorg/{name}",
        "url": f"https://github.com/testorg/{name}",
        "sshUrl": f"git@github.com:testorg/{name}.git",
        "isPrivate": False,
        "isArchived": archived,
        "description": None,
        "defaultBranchRef": {"name": "main"},
    }


class TestDetectGitHubSource:
    """Tests for detect_github_source function."""

    def test_detects_github_com(self) -> None:
        """Test detection of github.com URLs."""
        assert detect_github_source("github.com") is True
        assert detect_github_source("github.com/myorg") is True
        assert detect_github_source("https://github.com/myorg") is True

    def test_detects_github_enterprise(self) -> None:
        """Test detection of GitHub Enterprise URLs."""
        assert detect_github_source("ghe.example.com") is True
        assert detect_github_source("github.company.com") is True

    def test_detects_github_io(self) -> None:
        """Test detection of github.io URLs."""
        assert detect_github_source("myorg.github.io") is True

    def test_rejects_non_github(self) -> None:
        """Test rejection of non-GitHub URLs."""
        assert detect_github_source("gerrit.example.com") is False
        assert detect_github_source("gitlab.com") is False
        assert detect_github_source("bitbucket.org") is False


class TestParseGitHubUrl:
    """Tests for parse_github_url function."""

    def test_parses_full_url(self) -> None:
        """Test parsing full GitHub URL."""
        host, org = parse_github_url("https://github.com/lfreleng-actions")
        assert host == "github.com"
        assert org == "lfreleng-actions"

    def test_parses_url_without_protocol(self) -> None:
        """Test parsing URL without protocol."""
        host, org = parse_github_url("github.com/myorg")
        assert host == "github.com"
        assert org == "myorg"

    def test_parses_hostname_only(self) -> None:
        """Test parsing hostname without org."""
        host, org = parse_github_url("github.com")
        assert host == "github.com"
        assert org is None

    def test_handles_http_protocol(self) -> None:
        """Test handling of http:// protocol."""
        host, org = parse_github_url("http://github.com/testorg")
        assert host == "github.com"
        assert org == "testorg"


class TestExtractOrgFromHost:
    """Tests for _extract_org_from_host function."""

    def test_extracts_org_from_url(self) -> None:
        """Test extraction of org from URL."""
        org = _extract_org_from_host("github.com/lfreleng-actions")
        assert org == "lfreleng-actions"

    def test_extracts_org_with_https(self) -> None:
        """Test extraction with https protocol."""
        org = _extract_org_from_host("https://github.com/myorg")
        assert org == "myorg"

    def test_returns_none_for_hostname_only(self) -> None:
        """Test returns None when only hostname provided."""
        org = _extract_org_from_host("github.com")
        assert org is None

    def test_handles_enterprise_urls(self) -> None:
        """Test extraction from GitHub Enterprise URLs."""
        org = _extract_org_from_host("ghe.company.com/engineering")
        assert org == "engineering"


class TestConvertToProjects:
    """Tests for _convert_to_projects function."""

    def test_converts_basic_repo(self) -> None:
        """Test conversion of basic repository."""
        repos = [
            {
                "name": "test-repo",
                "description": "Test repository",
                "clone_url": "https://github.com/org/test-repo.git",
                "ssh_url": "git@github.com:org/test-repo.git",
                "html_url": "https://github.com/org/test-repo",
                "default_branch": "main",
            }
        ]

        projects = _convert_to_projects(repos)

        assert len(projects) == 1
        project = projects[0]
        assert project.name == "test-repo"
        assert project.description == "Test repository"
        assert project.source_type == SourceType.GITHUB
        assert project.clone_url == "https://github.com/org/test-repo.git"
        assert project.ssh_url_override == "git@github.com:org/test-repo.git"
        assert project.default_branch == "main"
        assert project.state == ProjectState.ACTIVE

    def test_converts_archived_repo(self) -> None:
        """Test conversion of archived repository."""
        repos = [
            {
                "name": "old-repo",
                "description": "Archived repo",
                "clone_url": "https://github.com/org/old-repo.git",
                "ssh_url": "git@github.com:org/old-repo.git",
                "html_url": "https://github.com/org/old-repo",
                "archived": True,
            }
        ]

        projects = _convert_to_projects(repos)

        assert len(projects) == 1
        assert projects[0].state == ProjectState.READ_ONLY

    def test_converts_multiple_repos(self) -> None:
        """Test conversion of multiple repositories."""
        repos = [
            {
                "name": "repo1",
                "description": "First repo",
                "clone_url": "https://github.com/org/repo1.git",
                "ssh_url": "git@github.com:org/repo1.git",
                "html_url": "https://github.com/org/repo1",
            },
            {
                "name": "repo2",
                "description": "Second repo",
                "clone_url": "https://github.com/org/repo2.git",
                "ssh_url": "git@github.com:org/repo2.git",
                "html_url": "https://github.com/org/repo2",
            },
        ]

        projects = _convert_to_projects(repos)

        assert len(projects) == 2
        assert projects[0].name == "repo1"
        assert projects[1].name == "repo2"


class TestApplyFilters:
    """Tests for _apply_filters function."""

    def test_filters_by_include_projects(self) -> None:
        """Test filtering by include_projects list."""
        projects = [
            Project(
                name="repo1", state=ProjectState.ACTIVE, source_type=SourceType.GITHUB
            ),
            Project(
                name="repo2", state=ProjectState.ACTIVE, source_type=SourceType.GITHUB
            ),
            Project(
                name="repo3", state=ProjectState.ACTIVE, source_type=SourceType.GITHUB
            ),
        ]

        config = Config(
            host="github.com",
            source_type=SourceType.GITHUB,
            include_projects=["repo1", "repo3"],
        )

        filtered, skipped = _apply_filters(projects, config)

        assert len(filtered) == 2
        assert filtered[0].name == "repo1"
        assert filtered[1].name == "repo3"
        assert skipped == 0

    def test_filters_archived_repos(self) -> None:
        """Test filtering of archived repositories."""
        projects = [
            Project(
                name="active1", state=ProjectState.ACTIVE, source_type=SourceType.GITHUB
            ),
            Project(
                name="archived1",
                state=ProjectState.READ_ONLY,
                source_type=SourceType.GITHUB,
            ),
            Project(
                name="active2", state=ProjectState.ACTIVE, source_type=SourceType.GITHUB
            ),
        ]

        config = Config(
            host="github.com",
            source_type=SourceType.GITHUB,
            skip_archived=True,
        )

        filtered, skipped = _apply_filters(projects, config)

        assert len(filtered) == 2
        assert filtered[0].name == "active1"
        assert filtered[1].name == "active2"
        assert skipped == 1

    def test_includes_archived_when_not_skipped(self) -> None:
        """Test including archived repos when skip_archived=False."""
        projects = [
            Project(
                name="active1", state=ProjectState.ACTIVE, source_type=SourceType.GITHUB
            ),
            Project(
                name="archived1",
                state=ProjectState.READ_ONLY,
                source_type=SourceType.GITHUB,
            ),
        ]

        config = Config(
            host="github.com",
            source_type=SourceType.GITHUB,
            skip_archived=False,
        )

        filtered, skipped = _apply_filters(projects, config)

        assert len(filtered) == 2
        assert skipped == 0

    @pytest.mark.parametrize("skip_archived", [True, False])
    def test_name_filtered_archived_not_counted_as_skipped(
        self, skip_archived: bool
    ) -> None:
        """An archived repo the name filters remove isn't skipped as archived."""
        projects = [
            Project(
                name="active1", state=ProjectState.ACTIVE, source_type=SourceType.GITHUB
            ),
            Project(
                name="archived1",
                state=ProjectState.READ_ONLY,
                source_type=SourceType.GITHUB,
            ),
        ]

        config = Config(
            host="github.com",
            source_type=SourceType.GITHUB,
            exclude_projects=["archived1"],
            skip_archived=skip_archived,
        )

        filtered, skipped = _apply_filters(projects, config)

        assert [p.name for p in filtered] == ["active1"]
        assert skipped == 0

    def test_no_filters_returns_all(self) -> None:
        """Test that no filters returns all projects."""
        projects = [
            Project(
                name="repo1", state=ProjectState.ACTIVE, source_type=SourceType.GITHUB
            ),
            Project(
                name="repo2", state=ProjectState.ACTIVE, source_type=SourceType.GITHUB
            ),
        ]

        config = Config(
            host="github.com",
            source_type=SourceType.GITHUB,
            skip_archived=False,
        )

        filtered, skipped = _apply_filters(projects, config)

        assert len(filtered) == 2
        assert skipped == 0


class TestDiscoverGitHubRepositories:
    """Tests for discover_github_repositories function."""

    @patch("source_clone.github_discovery.GitHubAPI")
    def test_discovers_org_repos(self, mock_api_class: MagicMock) -> None:
        """Test discovery of organization repositories."""
        # Setup mock
        mock_api = MagicMock()
        mock_api_class.return_value.__enter__.return_value = mock_api

        mock_api.list_owner_repos_graphql.return_value = {
            "repo1": {
                "name": "repo1",
                "full_name": "testorg/repo1",
                "html_url": "https://github.com/testorg/repo1",
                "ssh_url": "git@github.com:testorg/repo1.git",
                "clone_url": "https://github.com/testorg/repo1.git",
                "private": False,
                "description": "Test repo 1",
                "default_branch": "main",
            },
            "repo2": {
                "name": "repo2",
                "full_name": "testorg/repo2",
                "html_url": "https://github.com/testorg/repo2",
                "ssh_url": "git@github.com:testorg/repo2.git",
                "clone_url": "https://github.com/testorg/repo2.git",
                "private": False,
                "description": "Test repo 2",
                "default_branch": "main",
            },
        }

        config = Config(
            host="github.com/testorg",
            source_type=SourceType.GITHUB,
            github_org="testorg",
            github_token="fake-token",
        )

        projects, stats = discover_github_repositories(config)

        assert len(projects) == 2
        assert stats["total"] == 2
        assert stats["discovery_method"] == "github_api"
        assert stats["org_or_user"] == "testorg"

        # Verify projects
        assert projects[0].name == "repo1"
        assert projects[0].source_type == SourceType.GITHUB
        assert projects[1].name == "repo2"

    @patch("source_clone.github_discovery.GitHubAPI")
    def test_applies_include_filter(self, mock_api_class: MagicMock) -> None:
        """Test that include_projects filter is applied."""
        # Setup mock
        mock_api = MagicMock()
        mock_api_class.return_value.__enter__.return_value = mock_api

        mock_api.list_owner_repos_graphql.return_value = {
            "repo1": {
                "name": "repo1",
                "full_name": "testorg/repo1",
                "html_url": "https://github.com/testorg/repo1",
                "ssh_url": "git@github.com:testorg/repo1.git",
                "clone_url": "https://github.com/testorg/repo1.git",
                "private": False,
                "description": "Test repo 1",
            },
            "repo2": {
                "name": "repo2",
                "full_name": "testorg/repo2",
                "html_url": "https://github.com/testorg/repo2",
                "ssh_url": "git@github.com:testorg/repo2.git",
                "clone_url": "https://github.com/testorg/repo2.git",
                "private": False,
                "description": "Test repo 2",
            },
        }

        config = Config(
            host="github.com/testorg",
            source_type=SourceType.GITHUB,
            github_org="testorg",
            github_token="fake-token",
            include_projects=["repo1"],
        )

        projects, _stats = discover_github_repositories(config)

        assert len(projects) == 1
        assert projects[0].name == "repo1"

    @patch("source_clone.github_discovery.GitHubAPI")
    def test_lists_the_owner_named_in_the_host(self, mock_api_class: MagicMock) -> None:
        """Discovery lists the repositories of the login it was given."""
        mock_api = MagicMock()
        mock_api_class.return_value.__enter__.return_value = mock_api
        mock_api.list_owner_repos_graphql.return_value = {
            "user-repo1": {
                "name": "user-repo1",
                "full_name": "testuser/user-repo1",
                "html_url": "https://github.com/testuser/user-repo1",
                "ssh_url": "git@github.com:testuser/user-repo1.git",
                "clone_url": "https://github.com/testuser/user-repo1",
                "private": True,
                "description": "User repo 1",
            },
        }

        config = Config(
            host="github.com/testuser",
            source_type=SourceType.GITHUB,
            github_org="testuser",
            github_token="fake-token",
        )

        projects, stats = discover_github_repositories(config)

        mock_api.list_owner_repos_graphql.assert_called_once_with("testuser")
        mock_api.list_all_repos_graphql.assert_not_called()
        mock_api.list_repos.assert_not_called()
        assert [p.name for p in projects] == ["user-repo1"]
        assert stats["total"] == 1
        assert stats["discovery_method"] == "github_api"
        assert stats["org_or_user"] == "testuser"

    @pytest.mark.parametrize("owner_type", ["Organization", "User"])
    @pytest.mark.parametrize(
        ("skip_archived", "expected_names", "expected_skipped"),
        [
            pytest.param(True, ["live"], 1, id="skip-archived"),
            pytest.param(False, ["live", "old"], 0, id="include-archived"),
        ],
    )
    @patch("source_clone.github_discovery.GitHubAPI")
    def test_archived_flag_reaches_filter(
        self,
        mock_api_class: MagicMock,
        skip_archived: bool,
        expected_names: list[str],
        expected_skipped: int,
        owner_type: str,
    ) -> None:
        """A repo archived on GitHub honours --skip-archived.

        Only the HTTP call is stubbed, so the GraphQL query, the node
        mapping and the filter all run as they do against GitHub, for
        organisations and users alike.
        """
        api = GitHubAPI(token="fake-token")
        mock_api_class.return_value = api

        response = Mock()
        response.json.return_value = {
            "data": {
                "repositoryOwner": {
                    "__typename": owner_type,
                    "repositories": {
                        "nodes": [
                            _graphql_node("live", archived=False),
                            _graphql_node("old", archived=True),
                        ],
                        "pageInfo": {"hasNextPage": False, "endCursor": None},
                    },
                },
            },
        }

        config = Config(
            host="github.com/testorg",
            source_type=SourceType.GITHUB,
            github_org="testorg",
            github_token="fake-token",
            skip_archived=skip_archived,
        )

        with patch.object(api.client, "post", return_value=response):
            projects, stats = discover_github_repositories(config)

        assert sorted(p.name for p in projects) == expected_names
        assert stats["skipped"] == expected_skipped

    def test_raises_on_invalid_source_type(self) -> None:
        """Test that error is raised for non-GitHub source type."""
        config = Config(
            host="gerrit.example.com",
            source_type=SourceType.GERRIT,
        )

        with pytest.raises(ValueError, match="Expected source_type GITHUB"):
            discover_github_repositories(config)

    def test_raises_on_missing_org(self) -> None:
        """Test that error is raised when org cannot be determined."""
        config = Config(
            host="github.com",
            source_type=SourceType.GITHUB,
        )

        with pytest.raises(ValueError, match="Could not determine GitHub organization"):
            discover_github_repositories(config)

    @patch("source_clone.github_discovery.GitHubAPI")
    def test_uses_env_token_when_not_provided(self, mock_api_class: MagicMock) -> None:
        """Test that GITHUB_TOKEN env var is used when token not provided."""
        # Setup mock
        mock_api = MagicMock()
        mock_api_class.return_value.__enter__.return_value = mock_api

        mock_api.list_owner_repos_graphql.return_value = {}

        config = Config(
            host="github.com/testorg",
            source_type=SourceType.GITHUB,
            github_org="testorg",
            github_token=None,
        )

        with patch("os.getenv", return_value="env-token"):
            discover_github_repositories(config)

        # Verify API was created (token handling is in GitHubAPI)
        mock_api_class.assert_called_once()

    @patch("source_clone.github_discovery.GitHubAPI")
    def test_gerrit_clone_token_precedence(self, mock_api_class: MagicMock) -> None:
        """Test that GERRIT_CLONE_TOKEN takes precedence over GITHUB_TOKEN."""
        # Setup mock
        mock_api = MagicMock()
        mock_api_class.return_value.__enter__.return_value = mock_api

        mock_api.list_owner_repos_graphql.return_value = {}

        config = Config(
            host="github.com/testorg",
            source_type=SourceType.GITHUB,
            github_org="testorg",
            github_token=None,
        )

        # Mock os.getenv to return different values for different env vars
        def mock_getenv(key: str) -> str | None:
            if key == "GERRIT_CLONE_TOKEN":
                return "gerrit-clone-token"
            elif key == "GITHUB_TOKEN":
                return "github-token"
            return None

        with patch("os.getenv", side_effect=mock_getenv):
            discover_github_repositories(config)

        # Verify GitHubAPI was called with GERRIT_CLONE_TOKEN (preferred)
        mock_api_class.assert_called_once_with(
            token="gerrit-clone-token", base_url="https://api.github.com"
        )
