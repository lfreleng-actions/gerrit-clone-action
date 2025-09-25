# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2025 Matthew Watkins <mwatkins@linuxfoundation.org>

"""Unit tests for gerrit_api module."""

from __future__ import annotations

import json
from unittest.mock import Mock, patch

import httpx
import pytest

from gerrit_clone.gerrit_api import (
    GerritAPIClient,
    GerritAPIError,
    GerritAuthenticationError,
    GerritConnectionError,
    GerritParseError,
)
from gerrit_clone.models import Config, ProjectState


class TestGerritAPIError:
    """Test GerritAPIError exception."""

    def test_gerrit_api_error_creation(self) -> None:
        """Test GerritAPIError creation."""
        error = GerritAPIError("API failed")
        assert str(error) == "API failed"
        assert isinstance(error, Exception)

    def test_gerrit_connection_error_creation(self) -> None:
        """Test GerritConnectionError creation."""
        error = GerritConnectionError("Connection failed")
        assert str(error) == "Connection failed"
        assert isinstance(error, Exception)

    def test_gerrit_authentication_error_creation(self) -> None:
        """Test GerritAuthenticationError creation."""
        error = GerritAuthenticationError("Auth failed")
        assert str(error) == "Auth failed"
        assert isinstance(error, GerritAPIError)


class TestGerritAPIClient:
    """Test GerritAPIClient class."""

    def test_client_initialization(self) -> None:
        """Test client initialization."""
        config = Config(host="gerrit.example.org")
        client = GerritAPIClient(config)

        assert client.config is config
        assert client.base_url == "https://gerrit.example.org"
        assert isinstance(client.client, httpx.Client)

    def test_client_initialization_custom_base_url(self) -> None:
        """Test client with custom base URL."""
        config = Config(
            host="gerrit.example.org", base_url="https://custom.example.org"
        )
        client = GerritAPIClient(config)

        assert client.base_url == "https://custom.example.org"

    @patch("httpx.Client.get")
    def test_fetch_projects_success(self, mock_get: Mock) -> None:
        """Test successful project fetching."""
        # Mock response with typical Gerrit format
        mock_response = Mock()
        mock_response.status_code = 200
        mock_response.text = """)]}'
{
  "project1": {
    "id": "project1",
    "state": "ACTIVE",
    "description": "Test project 1"
  },
  "group/project2": {
    "id": "group/project2",
    "state": "READ_ONLY",
    "description": "Test project 2"
  }
}"""
        mock_get.return_value = mock_response

        config = Config(
            host="gerrit.example.org", base_url="https://gerrit.example.org"
        )
        client = GerritAPIClient(config)

        projects = client.fetch_projects()

        assert len(projects) == 2
        assert any(
            p.name == "project1" and p.state == ProjectState.ACTIVE for p in projects
        )
        assert any(
            p.name == "group/project2" and p.state == ProjectState.READ_ONLY
            for p in projects
        )
        mock_get.assert_called_once_with("/projects/?d")

    @patch("httpx.Client.get")
    def test_fetch_projects_empty_response(self, mock_get: Mock) -> None:
        """Test handling of empty project response."""
        mock_response = Mock()
        mock_response.status_code = 200
        mock_response.text = """)]}'
{}"""
        mock_get.return_value = mock_response

        config = Config(
            host="gerrit.example.org", base_url="https://gerrit.example.org"
        )
        client = GerritAPIClient(config)

        projects = client.fetch_projects()

        assert projects == []

    @patch("httpx.Client.get")
    def test_fetch_projects_http_error(self, mock_get: Mock) -> None:
        """Test handling of HTTP errors."""
        mock_response = Mock()
        mock_response.status_code = 500
        mock_response.text = "Internal Server Error"
        mock_get.return_value = mock_response

        config = Config(host="gerrit.example.org")
        client = GerritAPIClient(config)

        with pytest.raises(GerritConnectionError, match="Server error"):
            client.fetch_projects()

    @patch("httpx.Client.get")
    def test_fetch_projects_connection_error(self, mock_get: Mock) -> None:
        """Test handling of connection errors."""
        mock_get.side_effect = httpx.ConnectError("Connection failed")

        config = Config(
            host="gerrit.example.org", base_url="https://gerrit.example.org"
        )
        client = GerritAPIClient(config)

        with pytest.raises(GerritConnectionError, match="Connection failed"):
            client.fetch_projects()

    @patch("httpx.Client.get")
    def test_fetch_projects_timeout_error(self, mock_get: Mock) -> None:
        """Test handling of timeout errors."""
        mock_get.side_effect = httpx.TimeoutException("Request timeout")

        config = Config(host="gerrit.example.org")
        client = GerritAPIClient(config)

        with pytest.raises(GerritConnectionError, match="Request timeout"):
            client.fetch_projects()

    @patch("httpx.Client.get")
    def test_fetch_projects_invalid_json(self, mock_get: Mock) -> None:
        """Test handling of invalid JSON response."""
        mock_response = Mock()
        mock_response.status_code = 200
        mock_response.text = "invalid json"
        mock_get.return_value = mock_response

        config = Config(
            host="gerrit.example.org", base_url="https://gerrit.example.org"
        )
        client = GerritAPIClient(config)

        with pytest.raises(GerritParseError, match="Invalid JSON response"):
            client.fetch_projects()

    @patch("httpx.Client.get")
    def test_fetch_projects_missing_gerrit_prefix(self, mock_get: Mock) -> None:
        """Test handling response without Gerrit security prefix."""
        mock_response = Mock()
        mock_response.status_code = 200
        mock_response.text = '{"project1": {"id": "project1", "state": "ACTIVE"}}'
        mock_get.return_value = mock_response

        config = Config(
            host="gerrit.example.org", base_url="https://gerrit.example.org"
        )
        client = GerritAPIClient(config)

        projects = client.fetch_projects()

        assert len(projects) == 1
        assert projects[0].name == "project1"

    def test_parse_project_data_minimal(self) -> None:
        """Test parsing minimal project data."""
        config = Config(host="gerrit.example.org")
        client = GerritAPIClient(config)

        project_data = {"id": "test-project"}
        project = client._parse_project_data("test-project", project_data)

        assert project.name == "test-project"
        assert project.state == ProjectState.ACTIVE  # Default state
        assert project.description is None

    def test_parse_project_data_complete(self) -> None:
        """Test parsing complete project data."""
        config = Config(
            host="gerrit.example.org", base_url="https://gerrit.example.org"
        )
        client = GerritAPIClient(config)

        project_data = {
            "id": "test-project",
            "state": "READ_ONLY",
            "description": "Test description",
            "web_links": [{"name": "browse", "url": "https://example.org/browse"}],
        }
        project = client._parse_project_data("test-project", project_data)

        assert project.name == "test-project"
        assert project.state == ProjectState.READ_ONLY
        assert project.description == "Test description"
        assert project.web_links == [
            {"name": "browse", "url": "https://example.org/browse"}
        ]

    def test_parse_project_data_unknown_state(self) -> None:
        """Test parsing project with unknown state."""
        config = Config(host="gerrit.example.org")
        client = GerritAPIClient(config)

        project_data = {"id": "test-project", "state": "UNKNOWN_STATE"}
        project = client._parse_project_data("test-project", project_data)

        assert project.name == "test-project"
        assert project.state == ProjectState.ACTIVE  # Default for unknown states

    @patch("httpx.Client.get")
    def test_fetch_projects_with_retry(self, mock_get: Mock) -> None:
        """Test project fetching with retry logic."""

        # First call fails, second succeeds
        mock_response_fail = Mock()
        mock_response_fail.status_code = 503
        mock_response_fail.text = "Service Unavailable"

        mock_response_success = Mock()
        mock_response_success.status_code = 200
        mock_response_success.text = """)]}'
{"project1": {"id": "project1", "state": "ACTIVE"}}"""

        mock_get.side_effect = [mock_response_fail, mock_response_success]

        config = Config(
            host="gerrit.example.org", base_url="https://gerrit.example.org"
        )
        client = GerritAPIClient(config)

        # Should succeed on retry for 5xx errors
        with patch("time.sleep"):  # Speed up test
            projects = client.fetch_projects()

        assert len(projects) == 1
        assert projects[0].name == "project1"
        assert mock_get.call_count == 2

    def test_client_context_manager(self) -> None:
        """Test client as context manager."""
        config = Config(host="gerrit.example.org")

        with GerritAPIClient(config) as client:
            assert isinstance(client, GerritAPIClient)
            assert client.client is not None

        # Client should be closed after context

    def test_client_close(self) -> None:
        """Test client close method."""
        config = Config(
            host="gerrit.example.org", base_url="https://gerrit.example.org"
        )
        client = GerritAPIClient(config)

        # Should not raise exception
        client.close()

    @patch("httpx.Client.get")
    def test_fetch_projects_filters_archived(self, mock_get: Mock) -> None:
        """Test that archived projects can be filtered."""
        mock_response = Mock()
        mock_response.status_code = 200
        mock_response.text = """)]}'
{
  "active-project": {
    "id": "active-project",
    "state": "ACTIVE"
  },
  "archived-project": {
    "id": "archived-project",
    "state": "READ_ONLY"
  },
  "hidden-project": {
    "id": "hidden-project",
    "state": "HIDDEN"
  }
}"""
        mock_get.return_value = mock_response

        config = Config(host="gerrit.example.org", skip_archived=True)
        client = GerritAPIClient(config)

        projects = client.fetch_projects()

        # Should include all projects - filtering happens at higher level
        assert len(projects) == 3
        project_names = [p.name for p in projects]
        assert "active-project" in project_names
        assert "archived-project" in project_names
        assert "hidden-project" in project_names

    @patch("httpx.Client.get")
    def test_fetch_projects_large_response(self, mock_get: Mock) -> None:
        """Test handling of large project response."""
        # Generate large response
        projects_data = {}
        for i in range(100):
            projects_data[f"project-{i:03d}"] = {
                "id": f"project-{i:03d}",
                "state": "ACTIVE" if i % 2 == 0 else "READ_ONLY",
            }

        mock_response = Mock()
        mock_response.status_code = 200
        mock_response.text = f")]}}'\n{json.dumps(projects_data)}"
        mock_get.return_value = mock_response

        config = Config(
            host="gerrit.example.org", base_url="https://gerrit.example.org"
        )
        client = GerritAPIClient(config)

        projects = client.fetch_projects()

        assert len(projects) == 100
        # Verify states are parsed correctly
        active_count = sum(1 for p in projects if p.state == ProjectState.ACTIVE)
        readonly_count = sum(1 for p in projects if p.state == ProjectState.READ_ONLY)
        assert active_count == 50
        assert readonly_count == 50

    def test_strip_gerrit_prefix(self) -> None:
        """Test Gerrit security prefix stripping."""
        config = Config(
            host="gerrit.example.org", base_url="https://gerrit.example.org"
        )
        client = GerritAPIClient(config)

        # Test with prefix
        with_prefix = ')]}\'\n{"test": "data"}'
        result = client._strip_gerrit_prefix(with_prefix)
        assert result == '{"test": "data"}'

        # Test without prefix
        without_prefix = '{"test": "data"}'
        result = client._strip_gerrit_prefix(without_prefix)
        assert result == '{"test": "data"}'

        # Test empty string
        result = client._strip_gerrit_prefix("")
        assert result == ""

    @patch("httpx.Client.get")
    def test_fetch_projects_authentication_required(self, mock_get: Mock) -> None:
        """Test handling of authentication errors."""
        mock_response = Mock()
        mock_response.status_code = 401
        mock_response.text = "Authentication required"
        mock_get.return_value = mock_response

        config = Config(host="gerrit.example.org")
        client = GerritAPIClient(config)

        with pytest.raises(GerritAuthenticationError, match="Authentication failed"):
            client.fetch_projects()
