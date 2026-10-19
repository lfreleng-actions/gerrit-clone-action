# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2025 Matthew Watkins <mwatkins@linuxfoundation.org>

"""Unit tests for unified Gerrit project discovery."""

from unittest.mock import MagicMock, patch

import pytest

from source_clone.error_codes import DiscoveryError
from source_clone.gerrit_api import GerritAPIError, GerritConnectionError
from source_clone.models import Config, DiscoveryMethod, Project, ProjectState
from source_clone.unified_discovery import UnifiedDiscovery


class TestUnifiedDiscoveryErrorHandling:
    """Test error handling in unified discovery."""

    @pytest.fixture
    def mock_config(self):
        """Create a mock config for testing."""
        config = MagicMock(spec=Config)
        config.host = "gerrit.test.org"
        config.port = 443
        config.use_https = True
        config.discovery_method = DiscoveryMethod.HTTP
        config.skip_archived = True
        return config

    @pytest.fixture
    def sample_projects(self):
        """Create sample projects for testing."""
        return [
            Project(
                name="project1",
                state=ProjectState.ACTIVE,
                description="Test project 1",
            ),
            Project(
                name="project2",
                state=ProjectState.ACTIVE,
                description="Test project 2",
            ),
        ]

    def test_http_discovery_with_404_error(self, mock_config):
        """Test that HTTP discovery with 404 error raises DiscoveryError."""
        mock_config.discovery_method = DiscoveryMethod.HTTP

        with patch(
            "source_clone.unified_discovery.fetch_gerrit_projects"
        ) as mock_fetch:
            # Simulate a 404 error from Gerrit API
            mock_fetch.side_effect = GerritAPIError(
                "Projects API not found (HTTP 404). Check server URL: https://gerrit.test.org"
            )

            discovery = UnifiedDiscovery(mock_config)

            with pytest.raises(DiscoveryError) as exc_info:
                discovery.discover_projects()

            assert "HTTP discovery failed" in str(exc_info.value)
            assert "404" in str(exc_info.value.details)

    def test_http_discovery_with_connection_error(self, mock_config):
        """Test that HTTP discovery with connection error raises DiscoveryError."""
        mock_config.discovery_method = DiscoveryMethod.HTTP

        with patch(
            "source_clone.unified_discovery.fetch_gerrit_projects"
        ) as mock_fetch:
            # Simulate a connection error
            mock_fetch.side_effect = GerritConnectionError("Connection failed")

            discovery = UnifiedDiscovery(mock_config)

            with pytest.raises(DiscoveryError) as exc_info:
                discovery.discover_projects()

            assert "HTTP discovery failed" in str(exc_info.value)
            assert "Connection failed" in str(exc_info.value.details)

    def test_ssh_discovery_with_error(self, mock_config):
        """Test that SSH discovery with error raises DiscoveryError."""
        mock_config.discovery_method = DiscoveryMethod.SSH

        with patch(
            "source_clone.unified_discovery.fetch_gerrit_projects_ssh"
        ) as mock_fetch:
            # Simulate an SSH error
            mock_fetch.side_effect = Exception("SSH connection failed")

            discovery = UnifiedDiscovery(mock_config)

            with pytest.raises(DiscoveryError) as exc_info:
                discovery.discover_projects()

            assert "SSH discovery failed" in str(exc_info.value)
            assert "SSH connection failed" in str(exc_info.value.details)

    def test_http_discovery_success(self, mock_config, sample_projects):
        """Test successful HTTP discovery."""
        mock_config.discovery_method = DiscoveryMethod.HTTP

        with patch(
            "source_clone.unified_discovery.fetch_gerrit_projects"
        ) as mock_fetch:
            mock_fetch.return_value = (
                sample_projects,
                {"total": 2, "filtered": 2, "skipped": 0},
            )

            discovery = UnifiedDiscovery(mock_config)
            projects, stats = discovery.discover_projects()

            assert len(projects) == 2
            assert stats["discovery_method"] == "http"
            assert "warnings" in stats
            assert len(stats["warnings"]) == 0

    def test_ssh_discovery_success(self, mock_config, sample_projects):
        """Test successful SSH discovery."""
        mock_config.discovery_method = DiscoveryMethod.SSH

        with patch(
            "source_clone.unified_discovery.fetch_gerrit_projects_ssh"
        ) as mock_fetch:
            mock_fetch.return_value = (
                sample_projects,
                {"total": 2, "filtered": 2, "skipped": 0},
            )

            discovery = UnifiedDiscovery(mock_config)
            projects, stats = discovery.discover_projects()

            assert len(projects) == 2
            assert stats["discovery_method"] == "ssh"
            assert "warnings" in stats
            assert len(stats["warnings"]) == 0

    def test_both_discovery_http_fails_ssh_succeeds(self, mock_config, sample_projects):
        """Test BOTH discovery when HTTP fails but SSH succeeds."""
        mock_config.discovery_method = DiscoveryMethod.BOTH

        with (
            patch("source_clone.unified_discovery.fetch_gerrit_projects") as mock_http,
            patch(
                "source_clone.unified_discovery.fetch_gerrit_projects_ssh"
            ) as mock_ssh,
        ):
            # HTTP fails
            mock_http.side_effect = GerritAPIError("HTTP 404")

            # SSH succeeds
            mock_ssh.return_value = (
                sample_projects,
                {"total": 2, "filtered": 2, "skipped": 0},
            )

            discovery = UnifiedDiscovery(mock_config)
            projects, stats = discovery.discover_projects()

            # Should get SSH results
            assert len(projects) == 2
            assert stats["discovery_method"] == "ssh_only_fallback"
            assert len(discovery.warnings) == 1
            assert "HTTP discovery failed" in discovery.warnings[0].message

    def test_both_discovery_ssh_fails_http_succeeds(self, mock_config, sample_projects):
        """Test BOTH discovery when SSH fails but HTTP succeeds."""
        mock_config.discovery_method = DiscoveryMethod.BOTH

        with (
            patch("source_clone.unified_discovery.fetch_gerrit_projects") as mock_http,
            patch(
                "source_clone.unified_discovery.fetch_gerrit_projects_ssh"
            ) as mock_ssh,
        ):
            # HTTP succeeds
            mock_http.return_value = (
                sample_projects,
                {"total": 2, "filtered": 2, "skipped": 0},
            )

            # SSH fails
            mock_ssh.side_effect = Exception("SSH connection failed")

            discovery = UnifiedDiscovery(mock_config)
            projects, stats = discovery.discover_projects()

            # Should get HTTP results
            assert len(projects) == 2
            assert stats["discovery_method"] == "http_only_fallback"
            assert len(discovery.warnings) == 1
            assert "SSH discovery failed" in discovery.warnings[0].message

    def test_both_discovery_both_fail(self, mock_config):
        """Test BOTH discovery when both methods fail."""
        mock_config.discovery_method = DiscoveryMethod.BOTH

        with (
            patch("source_clone.unified_discovery.fetch_gerrit_projects") as mock_http,
            patch(
                "source_clone.unified_discovery.fetch_gerrit_projects_ssh"
            ) as mock_ssh,
        ):
            # Both fail
            mock_http.side_effect = GerritAPIError("HTTP 404")
            mock_ssh.side_effect = Exception("SSH connection failed")

            discovery = UnifiedDiscovery(mock_config)

            with pytest.raises(DiscoveryError) as exc_info:
                discovery.discover_projects()

            assert "Both HTTP and SSH discovery methods failed" in str(exc_info.value)
            assert "HTTP: " in str(exc_info.value.details)
            assert "SSH: " in str(exc_info.value.details)

    def test_both_discovery_both_succeed(self, mock_config, sample_projects):
        """Test BOTH discovery when both methods succeed."""
        mock_config.discovery_method = DiscoveryMethod.BOTH

        with (
            patch("source_clone.unified_discovery.fetch_gerrit_projects") as mock_http,
            patch(
                "source_clone.unified_discovery.fetch_gerrit_projects_ssh"
            ) as mock_ssh,
        ):
            # Both succeed
            mock_http.return_value = (
                sample_projects,
                {"total": 2, "filtered": 2, "skipped": 0},
            )
            mock_ssh.return_value = (
                sample_projects,
                {"total": 2, "filtered": 2, "skipped": 0},
            )

            discovery = UnifiedDiscovery(mock_config)
            projects, stats = discovery.discover_projects()

            # Should merge results
            assert len(projects) >= 2
            assert "discovery_method" in stats

    def test_discovery_error_preserves_exception_chain(self, mock_config):
        """Test that DiscoveryError preserves the original exception."""
        mock_config.discovery_method = DiscoveryMethod.HTTP

        with patch(
            "source_clone.unified_discovery.fetch_gerrit_projects"
        ) as mock_fetch:
            original_error = ValueError("Original error")
            mock_fetch.side_effect = original_error

            discovery = UnifiedDiscovery(mock_config)

            with pytest.raises(DiscoveryError) as exc_info:
                discovery.discover_projects()

            assert exc_info.value.original_exception is not None
            # The original exception should be preserved
            assert "Original error" in str(exc_info.value.details)


class TestUnifiedDiscoveryIntegration:
    """Integration tests for unified discovery."""

    def test_discovery_with_real_config(self):
        """Test discovery with a realistic config object."""
        # This test uses a mock but with a more realistic setup
        config = MagicMock(spec=Config)
        config.host = "gerrit.example.org"
        config.port = 443
        config.use_https = True
        config.discovery_method = DiscoveryMethod.HTTP
        config.skip_archived = True

        with patch(
            "source_clone.unified_discovery.fetch_gerrit_projects"
        ) as mock_fetch:
            mock_fetch.side_effect = GerritAPIError("Server unavailable")

            discovery = UnifiedDiscovery(config)

            # Should raise DiscoveryError, not the underlying GerritAPIError
            with pytest.raises(DiscoveryError):
                discovery.discover_projects()
