# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2025 Matthew Watkins <mwatkins@linuxfoundation.org>

"""Unit tests for Gerrit API discovery mechanism."""

import json
from typing import Any
from unittest.mock import Mock, patch

import httpx
import pytest

from gerrit_clone.discovery import (
    GerritAPIDiscovery,
    GerritDiscoveryError,
    check_gerrit_api_access,
    discover_gerrit_base_url,
)


class TestGerritAPIDiscovery:
    """Test cases for GerritAPIDiscovery class."""

    @pytest.fixture
    def discovery(self):
        """Create GerritAPIDiscovery instance for testing."""
        return GerritAPIDiscovery(timeout=5.0)

    @pytest.fixture
    def mock_response(self):
        """Create a mock HTTP response."""

        def _create_response(status_code=200, text="", headers=None):
            response = Mock(spec=httpx.Response)
            response.status_code = status_code
            response.text = text
            response.headers = headers or {}
            return response

        return _create_response

    def test_initialization(self, discovery):
        """Test GerritAPIDiscovery initialization."""
        assert discovery.timeout.connect == 10.0
        assert discovery.client.timeout.connect == 10.0
        assert "gerrit-clone" in discovery.client.headers["User-Agent"]
        assert discovery.client.headers["Accept"] == "application/json"

    def test_context_manager(self):
        """Test context manager functionality."""
        with GerritAPIDiscovery() as discovery:
            assert isinstance(discovery, GerritAPIDiscovery)
            assert hasattr(discovery, "client")

    @patch("gerrit_clone.discovery.httpx.Client")
    def test_close(self, mock_client_class):
        """Test cleanup on close."""
        mock_client = Mock()
        mock_client_class.return_value = mock_client

        discovery = GerritAPIDiscovery()
        discovery.close()

        mock_client.close.assert_called_once()

    def test_validate_projects_response_valid_gerrit_format(self, discovery):
        """Test validation of valid Gerrit API response."""
        # Standard Gerrit response format with CSRF protection
        response_text = ')]}\'\n{"project1":{"id":"project1","state":"ACTIVE"}}'

        assert discovery._validate_projects_response(response_text) is True

    def test_validate_projects_response_valid_json_without_prefix(self, discovery):
        """Test validation of valid JSON without CSRF prefix."""
        response_text = '{"project1":{"id":"project1","state":"ACTIVE"}}'

        assert discovery._validate_projects_response(response_text) is True

    def test_validate_projects_response_empty_projects(self, discovery):
        """Test validation of empty projects response."""
        response_text = ")]}'\n{}"

        assert discovery._validate_projects_response(response_text) is True

    def test_validate_projects_response_invalid_json(self, discovery):
        """Test validation of invalid JSON."""
        response_text = "invalid json"

        assert discovery._validate_projects_response(response_text) is False

    def test_validate_projects_response_wrong_format(self, discovery):
        """Test validation of wrong response format."""
        response_text = ')]}\'\n["not", "a", "dict"]'

        assert discovery._validate_projects_response(response_text) is False

    def test_discover_via_redirect_success(self, discovery, mock_response):
        """Test successful redirect discovery."""
        # Mock redirect response
        redirect_response = mock_response(
            status_code=302, headers={"location": "https://gerrit.example.org/r/"}
        )

        with patch.object(discovery.client, "get", return_value=redirect_response):
            result = discovery._discover_via_redirect("gerrit.example.org")

        assert result == "/r"

    def test_discover_via_redirect_no_redirect(self, discovery, mock_response):
        """Test redirect discovery when no redirect occurs."""
        # Mock normal response (no redirect)
        normal_response = mock_response(status_code=200)

        with patch.object(discovery.client, "get", return_value=normal_response):
            result = discovery._discover_via_redirect("gerrit.example.org")

        assert result is None

    def test_discover_via_redirect_external_host(self, discovery, mock_response):
        """Test redirect discovery with external host redirect."""
        # Mock redirect to different host (should be ignored)
        redirect_response = mock_response(
            status_code=302, headers={"location": "https://different-host.com/r/"}
        )

        with patch.object(discovery.client, "get", return_value=redirect_response):
            result = discovery._discover_via_redirect("gerrit.example.org")

        assert result is None

    def test_discover_via_redirect_exception(self, discovery):
        """Test redirect discovery with network exception."""
        with patch.object(
            discovery.client, "get", side_effect=httpx.ConnectError("Connection failed")
        ):
            result = discovery._discover_via_redirect("gerrit.example.org")

        assert result is None

    def test_test_projects_api_success(self, discovery, mock_response):
        """Test successful projects API test."""
        # Mock successful API response
        api_response = mock_response(
            status_code=200,
            text=')]}\'\n{"project1":{"id":"project1","state":"ACTIVE"}}',
        )

        with patch.object(discovery.client, "get", return_value=api_response):
            result = discovery._test_projects_api("https://gerrit.example.org/r")

        assert result is True

    def test_test_projects_api_not_found(self, discovery, mock_response):
        """Test projects API test with 404 response."""
        api_response = mock_response(status_code=404)

        with patch.object(discovery.client, "get", return_value=api_response):
            result = discovery._test_projects_api("https://gerrit.example.org")

        assert result is False

    def test_test_projects_api_invalid_response(self, discovery, mock_response):
        """Test projects API test with invalid response format."""
        api_response = mock_response(status_code=200, text="<html>Not JSON</html>")

        with patch.object(discovery.client, "get", return_value=api_response):
            result = discovery._test_projects_api("https://gerrit.example.org/r")

        assert result is False

    def test_test_projects_api_exception(self, discovery):
        """Test projects API test with network exception."""
        with patch.object(
            discovery.client, "get", side_effect=httpx.ConnectError("Connection failed")
        ):
            result = discovery._test_projects_api("https://gerrit.example.org/r")

        assert result is False

    def test_discover_base_url_with_redirect(self, discovery, mock_response):
        """Test base URL discovery using redirect."""
        # Mock redirect response
        redirect_response = mock_response(
            status_code=302, headers={"location": "https://gerrit.example.org/r/"}
        )

        # Mock successful API test for /r path
        api_response = mock_response(
            status_code=200,
            text=')]}\'\n{"project1":{"id":"project1","state":"ACTIVE"}}',
        )

        with patch.object(discovery.client, "get") as mock_get:
            # First call is for redirect discovery, second is for API test
            mock_get.side_effect = [redirect_response, api_response]

            result = discovery.discover_base_url("gerrit.example.org")

        assert result == "https://gerrit.example.org/r"
        assert mock_get.call_count == 2

    def test_discover_base_url_direct_path(self, discovery, mock_response):
        """Test base URL discovery with direct path (no redirect)."""
        # Mock no redirect
        normal_response = mock_response(status_code=200)

        # Mock API responses - first path fails, second succeeds
        api_not_found = mock_response(status_code=404)
        api_success = mock_response(
            status_code=200,
            text=')]}\'\n{"project1":{"id":"project1","state":"ACTIVE"}}',
        )

        with patch.object(discovery.client, "get") as mock_get:
            # Redirect check, then API tests
            mock_get.side_effect = [normal_response, api_not_found, api_success]

            result = discovery.discover_base_url("gerrit.example.org")

        assert result == "https://gerrit.example.org/r"

    def test_discover_base_url_no_working_endpoint(self, discovery, mock_response):
        """Test base URL discovery when no endpoint works."""
        # Mock no redirect
        normal_response = mock_response(status_code=200)

        # Mock all API tests failing
        api_not_found = mock_response(status_code=404)

        with patch.object(discovery.client, "get") as mock_get:
            mock_get.return_value = normal_response
            # First call for redirect, then multiple API test failures
            mock_get.side_effect = [normal_response] + [api_not_found] * 5

            with pytest.raises(GerritDiscoveryError) as exc_info:
                discovery.discover_base_url("gerrit.example.org")

        assert "Could not discover Gerrit API endpoint" in str(exc_info.value)
        assert "gerrit.example.org" in str(exc_info.value)

    def test_discover_multiple_hosts_success(self, discovery):
        """Test discovering multiple hosts successfully."""
        hosts = ["host1.example.org", "host2.example.org"]

        with patch.object(discovery, "discover_base_url") as mock_discover:
            mock_discover.side_effect = [
                "https://host1.example.org/r",
                "https://host2.example.org/gerrit",
            ]

            result = discovery.discover_multiple_hosts(hosts)

        expected = {
            "host1.example.org": "https://host1.example.org/r",
            "host2.example.org": "https://host2.example.org/gerrit",
        }
        assert result == expected

    def test_discover_multiple_hosts_partial_failure(self, discovery):
        """Test discovering multiple hosts with some failures."""
        hosts = ["host1.example.org", "host2.example.org"]

        with patch.object(discovery, "discover_base_url") as mock_discover:
            mock_discover.side_effect = [
                "https://host1.example.org/r",
                GerritDiscoveryError("Discovery failed for host2"),
            ]

            with pytest.raises(GerritDiscoveryError) as exc_info:
                discovery.discover_multiple_hosts(hosts)

        assert "Failed to discover API for some hosts" in str(exc_info.value)
        assert "host2.example.org" in str(exc_info.value)


class TestConvenienceFunctions:
    """Test convenience functions."""

    @patch("gerrit_clone.discovery.GerritAPIDiscovery")
    def test_discover_gerrit_base_url(self, mock_discovery_class):
        """Test discover_gerrit_base_url convenience function."""
        mock_discovery = Mock()
        mock_discovery.discover_base_url.return_value = "https://gerrit.example.org/r"
        mock_discovery_class.return_value.__enter__.return_value = mock_discovery

        result = discover_gerrit_base_url("gerrit.example.org", timeout=15.0)

        assert result == "https://gerrit.example.org/r"
        mock_discovery_class.assert_called_once_with(timeout=15.0)
        mock_discovery.discover_base_url.assert_called_once_with("gerrit.example.org")

    @patch("gerrit_clone.discovery.GerritAPIDiscovery")
    def test_test_gerrit_api_access_success(self, mock_discovery_class):
        """Test check_gerrit_api_access with working API."""
        mock_discovery = Mock()
        mock_discovery._test_projects_api.return_value = True
        mock_discovery_class.return_value.__enter__.return_value = mock_discovery

        is_accessible, error = check_gerrit_api_access("https://gerrit.example.org/r")

        assert is_accessible is True
        assert error is None

    @patch("gerrit_clone.discovery.GerritAPIDiscovery")
    def test_test_gerrit_api_access_failure(self, mock_discovery_class):
        """Test check_gerrit_api_access with non-working API."""
        mock_discovery = Mock()
        mock_discovery._test_projects_api.return_value = False
        mock_discovery_class.return_value.__enter__.return_value = mock_discovery

        is_accessible, error = check_gerrit_api_access("https://gerrit.example.org")

        assert is_accessible is False
        assert error == "Projects API test failed"

    @patch("gerrit_clone.discovery.GerritAPIDiscovery")
    def test_test_gerrit_api_access_exception(self, mock_discovery_class):
        """Test check_gerrit_api_access with exception."""
        mock_discovery = Mock()
        mock_discovery._test_projects_api.side_effect = Exception("Network error")
        mock_discovery_class.return_value.__enter__.return_value = mock_discovery

        is_accessible, error = check_gerrit_api_access("https://gerrit.example.org/r")

        assert is_accessible is False
        assert error == "Network error"


class TestRealWorldScenarios:
    """Test real-world Gerrit server scenarios."""

    @pytest.fixture
    def discovery(self):
        """Create GerritAPIDiscovery instance for real-world tests."""
        return GerritAPIDiscovery(timeout=5.0)

    @pytest.fixture
    def mock_response(self):
        """Create a mock HTTP response."""

        def _create_response(status_code=200, text="", headers=None):
            response = Mock(spec=httpx.Response)
            response.status_code = status_code
            response.text = text
            response.headers = headers or {}
            return response

        return _create_response

    def create_gerrit_response(self, projects_data: dict[str, Any]) -> str:
        """Create a realistic Gerrit API response."""
        return ")]}'\n" + json.dumps(projects_data)

    def test_linux_foundation_pattern(self, discovery, mock_response):
        """Test Linux Foundation Gerrit pattern (redirect to /infra)."""
        # Mock redirect to /infra/
        redirect_response = mock_response(
            status_code=302,
            headers={"location": "https://gerrit.linuxfoundation.org/infra/"},
        )

        # Mock successful API response from /infra path
        projects_data = {
            "releng/global-jjb": {
                "id": "releng%2Fglobal-jjb",
                "description": "Global Jenkins Job Builder templates",
                "state": "ACTIVE",
            }
        }
        api_response = mock_response(
            status_code=200, text=self.create_gerrit_response(projects_data)
        )

        with patch.object(discovery.client, "get") as mock_get:
            mock_get.side_effect = [redirect_response, api_response]

            result = discovery.discover_base_url("gerrit.linuxfoundation.org")

        assert result == "https://gerrit.linuxfoundation.org/infra"

    def test_onap_pattern(self, discovery, mock_response):
        """Test ONAP Gerrit pattern (redirect to /r)."""
        # Mock redirect to /r/
        redirect_response = mock_response(
            status_code=302, headers={"location": "https://gerrit.onap.org/r/"}
        )

        # Mock successful API response from /r path
        projects_data = {
            "aaf": {
                "id": "aaf",
                "description": "Application Authorization Framework",
                "state": "ACTIVE",
            }
        }
        api_response = mock_response(
            status_code=200, text=self.create_gerrit_response(projects_data)
        )

        with patch.object(discovery.client, "get") as mock_get:
            mock_get.side_effect = [redirect_response, api_response]

            result = discovery.discover_base_url("gerrit.onap.org")

        assert result == "https://gerrit.onap.org/r"

    def test_oran_pattern(self, discovery, mock_response):
        """Test O-RAN-SC Gerrit pattern (redirect to /r)."""
        # Mock redirect to /r/
        redirect_response = mock_response(
            status_code=302, headers={"location": "https://gerrit.o-ran-sc.org/r/"}
        )

        # Mock successful API response from /r path
        projects_data = {
            ".github": {"id": ".github", "state": "ACTIVE"},
            "ric-plt/e2mgr": {
                "id": "ric-plt%2Fe2mgr",
                "description": "E2 Manager",
                "state": "ACTIVE",
            },
        }
        api_response = mock_response(
            status_code=200, text=self.create_gerrit_response(projects_data)
        )

        with patch.object(discovery.client, "get") as mock_get:
            mock_get.side_effect = [redirect_response, api_response]

            result = discovery.discover_base_url("gerrit.o-ran-sc.org")

        assert result == "https://gerrit.o-ran-sc.org/r"

    def test_opendaylight_pattern(self, discovery, mock_response):
        """Test OpenDaylight Gerrit pattern (redirect to /gerrit)."""
        # Mock redirect to /gerrit/
        redirect_response = mock_response(
            status_code=302,
            headers={"location": "https://git.opendaylight.org/gerrit/"},
        )

        # Mock successful API response from /gerrit path
        projects_data = {
            "aaa": {
                "id": "aaa",
                "description": "Authentication, Authorization and Accounting",
                "state": "ACTIVE",
            }
        }
        api_response = mock_response(
            status_code=200, text=self.create_gerrit_response(projects_data)
        )

        with patch.object(discovery.client, "get") as mock_get:
            mock_get.side_effect = [redirect_response, api_response]

            result = discovery.discover_base_url("git.opendaylight.org")

        assert result == "https://git.opendaylight.org/gerrit"

    def test_standard_gerrit_no_redirect(self, discovery, mock_response):
        """Test standard Gerrit installation without redirect."""
        # Mock no redirect (200 response)
        normal_response = mock_response(status_code=200)

        # Mock API failures for root and /r, success for /gerrit
        api_404 = mock_response(status_code=404)
        projects_data = {"project1": {"id": "project1", "state": "ACTIVE"}}
        api_success = mock_response(
            status_code=200, text=self.create_gerrit_response(projects_data)
        )

        with patch.object(discovery.client, "get") as mock_get:
            # Redirect check (no redirect), then API tests
            mock_get.side_effect = [normal_response, api_404, api_404, api_success]

            result = discovery.discover_base_url("gerrit.example.org")

        assert result == "https://gerrit.example.org/gerrit"

    def test_authenticated_api_path(self, discovery, mock_response):
        """Test Gerrit with authenticated API path (/a)."""
        # Mock no redirect
        normal_response = mock_response(status_code=200)

        # Mock API failures for standard paths, success for /a
        api_404 = mock_response(status_code=404)
        projects_data = {"project1": {"id": "project1", "state": "ACTIVE"}}
        api_success = mock_response(
            status_code=200, text=self.create_gerrit_response(projects_data)
        )

        with patch.object(discovery.client, "get") as mock_get:
            # Redirect check, then API test failures, then success on /a
            responses = [normal_response] + [api_404] * 4 + [api_success]
            mock_get.side_effect = responses

            result = discovery.discover_base_url("secure.gerrit.example.org")

        assert result == "https://secure.gerrit.example.org/a"

    def test_multiple_real_world_hosts(self, discovery):
        """Test discovery for multiple real-world Gerrit hosts."""
        hosts = [
            "gerrit.linuxfoundation.org",
            "gerrit.onap.org",
            "gerrit.o-ran-sc.org",
            "git.opendaylight.org",
        ]

        expected_urls = [
            "https://gerrit.linuxfoundation.org/infra",
            "https://gerrit.onap.org/r",
            "https://gerrit.o-ran-sc.org/r",
            "https://git.opendaylight.org/gerrit",
        ]

        with patch.object(discovery, "discover_base_url") as mock_discover:
            mock_discover.side_effect = expected_urls

            result = discovery.discover_multiple_hosts(hosts)

        expected_result = dict(zip(hosts, expected_urls, strict=False))
        assert result == expected_result
        assert mock_discover.call_count == len(hosts)


class TestErrorHandling:
    """Test error handling scenarios."""

    @pytest.fixture
    def discovery(self):
        """Create GerritAPIDiscovery instance for error tests."""
        return GerritAPIDiscovery(timeout=1.0)

    @pytest.fixture
    def mock_response(self):
        """Create a mock HTTP response."""

        def _create_response(status_code=200, text="", headers=None):
            response = Mock(spec=httpx.Response)
            response.status_code = status_code
            response.text = text
            response.headers = headers or {}
            return response

        return _create_response

    def test_network_timeout(self, discovery):
        """Test handling of network timeouts."""
        with (
            patch.object(
                discovery.client, "get", side_effect=httpx.TimeoutException("Timeout")
            ),
            pytest.raises(GerritDiscoveryError) as exc_info,
        ):
            discovery.discover_base_url("slow.gerrit.example.org")

        assert "Could not discover Gerrit API endpoint" in str(exc_info.value)

    def test_connection_error(self, discovery):
        """Test handling of connection errors."""
        with (
            patch.object(
                discovery.client,
                "get",
                side_effect=httpx.ConnectError("Connection refused"),
            ),
            pytest.raises(GerritDiscoveryError) as exc_info,
        ):
            discovery.discover_base_url("unreachable.gerrit.example.org")

        assert "Could not discover Gerrit API endpoint" in str(exc_info.value)

    def test_ssl_error(self, discovery):
        """Test handling of SSL errors."""
        with (
            patch.object(
                discovery.client, "get", side_effect=httpx.ConnectError("SSL Error")
            ),
            pytest.raises(GerritDiscoveryError) as exc_info,
        ):
            discovery.discover_base_url("badssl.gerrit.example.org")

        assert "Could not discover Gerrit API endpoint" in str(exc_info.value)

    def test_server_error_responses(self, discovery, mock_response):
        """Test handling of server error responses."""

        def mock_response_func(
            status_code: int = 200,
            text: str = "",
            headers: dict[str, str] | None = None,
        ) -> Mock:
            response = Mock(spec=httpx.Response)
            response.status_code = status_code
            response.text = text
            response.headers = headers or {}
            return response

        # Mock various server errors
        server_error = mock_response_func(status_code=500, text="Internal Server Error")

        with (
            patch.object(discovery.client, "get", return_value=server_error),
            pytest.raises(GerritDiscoveryError) as exc_info,
        ):
            discovery.discover_base_url("error.gerrit.example.org")

        assert "Could not discover Gerrit API endpoint" in str(exc_info.value)

    def test_malformed_redirect(self, discovery, mock_response):
        """Test handling of malformed redirect responses."""

        def mock_response_func(
            status_code: int = 200,
            text: str = "",
            headers: dict[str, str] | None = None,
        ) -> Mock:
            response = Mock(spec=httpx.Response)
            response.status_code = status_code
            response.text = text
            response.headers = headers or {}
            return response

        # Mock malformed redirect
        malformed_redirect = mock_response_func(
            status_code=302, headers={"location": "not-a-valid-url"}
        )

        # Mock API 404s for all paths
        api_404 = mock_response_func(status_code=404)

        with (
            patch.object(discovery.client, "get") as mock_get,
            pytest.raises(GerritDiscoveryError),
        ):
            mock_get.side_effect = [malformed_redirect] + [api_404] * 5
            discovery.discover_base_url("malformed.gerrit.example.org")
