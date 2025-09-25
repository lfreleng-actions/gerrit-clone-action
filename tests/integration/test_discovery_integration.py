# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2025 Matthew Watkins <mwatkins@linuxfoundation.org>

"""Integration tests for Gerrit API discovery against real servers.

These tests run against actual Gerrit servers and are marked as integration tests.
They can be skipped in CI environments or when network access is limited.
"""

import os
import time
from typing import TypedDict

import pytest

from gerrit_clone.config import load_config
from gerrit_clone.discovery import (
    GerritAPIDiscovery,
    GerritDiscoveryError,
    check_gerrit_api_access,
    discover_gerrit_base_url,
)
from gerrit_clone.gerrit_api import GerritAPIClient


class GerritServerConfig(TypedDict):
    """Type definition for Gerrit server configuration."""

    expected_base_url: str
    expected_redirect_path: str
    description: str
    min_projects: int
    sample_projects: list[str]


# Real-world Gerrit servers for testing
REAL_GERRIT_SERVERS: dict[str, GerritServerConfig] = {
    "gerrit.linuxfoundation.org": {
        "expected_base_url": "https://gerrit.linuxfoundation.org/infra",
        "expected_redirect_path": "/infra",
        "description": "Linux Foundation Infrastructure",
        "min_projects": 30,  # Expected minimum number of projects
        "sample_projects": ["releng/global-jjb", "releng/lftools"],
    },
    "gerrit.onap.org": {
        "expected_base_url": "https://gerrit.onap.org/r",
        "expected_redirect_path": "/r",
        "description": "Open Network Automation Platform",
        "min_projects": 300,
        "sample_projects": ["aaf", "aaf/authz"],
    },
    "gerrit.o-ran-sc.org": {
        "expected_base_url": "https://gerrit.o-ran-sc.org/r",
        "expected_redirect_path": "/r",
        "description": "O-RAN Software Community",
        "min_projects": 100,
        "sample_projects": [".github", "ric-plt/e2mgr"],
    },
    "git.opendaylight.org": {
        "expected_base_url": "https://git.opendaylight.org/gerrit",
        "expected_redirect_path": "/gerrit",
        "description": "OpenDaylight",
        "min_projects": 80,
        "sample_projects": ["aaa", ".github"],
    },
}

# Type-safe parameter lists for pytest parametrize
SERVER_PARAMS: list[tuple[str, GerritServerConfig]] = list(REAL_GERRIT_SERVERS.items())
HOST_PARAMS: list[str] = list(REAL_GERRIT_SERVERS.keys())


def pytest_configure(config):
    """Register custom pytest markers."""
    config.addinivalue_line(
        "markers",
        "integration: mark test as integration test that requires network access",
    )
    config.addinivalue_line("markers", "slow: mark test as slow running")


def should_skip_integration_tests() -> tuple[bool, str]:
    """Determine if integration tests should be skipped.

    Returns:
        Tuple of (should_skip, reason)
    """
    # Skip if explicitly disabled
    if os.getenv("SKIP_INTEGRATION_TESTS", "").lower() in ("1", "true", "yes"):
        return True, "Integration tests disabled via SKIP_INTEGRATION_TESTS"

    # Skip in CI unless explicitly enabled
    if os.getenv("CI") and not os.getenv("RUN_INTEGRATION_TESTS"):
        return (
            True,
            "Integration tests disabled in CI (set RUN_INTEGRATION_TESTS=1 to enable)",
        )

    # Skip if network tests are disabled
    if os.getenv("PYTEST_DISABLE_NETWORK_TESTS", "").lower() in ("1", "true", "yes"):
        return True, "Network tests disabled via PYTEST_DISABLE_NETWORK_TESTS"

    return False, ""


@pytest.mark.integration
@pytest.mark.slow
class TestRealGerritDiscovery:
    """Integration tests against real Gerrit servers."""

    @pytest.fixture(autouse=True)
    def check_integration_tests(self):
        """Auto-skip integration tests when appropriate."""
        should_skip, reason = should_skip_integration_tests()
        if should_skip:
            pytest.skip(reason)

    @pytest.fixture(scope="class")
    def discovery_client(self):
        """Create a discovery client for the test class."""
        with GerritAPIDiscovery(timeout=30.0) as client:
            yield client

    @pytest.mark.parametrize("host,config", SERVER_PARAMS)
    def test_discover_real_gerrit_base_url(
        self,
        host: str,
        config: GerritServerConfig,
        discovery_client: GerritAPIDiscovery,
    ) -> None:
        """Test discovery against real Gerrit servers."""
        print(f"\n🔍 Testing discovery for {host} ({config['description']})")

        try:
            base_url = discovery_client.discover_base_url(host)

            print(f"✅ Discovered base URL: {base_url}")
            assert base_url == config["expected_base_url"], (
                f"Expected {config['expected_base_url']}, got {base_url}"
            )

        except GerritDiscoveryError as e:
            pytest.fail(f"Discovery failed for {host}: {e}")

    @pytest.mark.parametrize("host,config", SERVER_PARAMS)
    def test_redirect_discovery(
        self,
        host: str,
        config: GerritServerConfig,
        discovery_client: GerritAPIDiscovery,
    ) -> None:
        """Test redirect-based discovery for real servers."""
        print(f"\n🔀 Testing redirect discovery for {host}")

        redirect_path = discovery_client._discover_via_redirect(host)

        if redirect_path:
            print(f"✅ Found redirect path: {redirect_path}")
            assert redirect_path == config["expected_redirect_path"], (
                f"Expected redirect to {config['expected_redirect_path']}, got {redirect_path}"
            )
        else:
            pytest.fail(
                f"No redirect found for {host}, expected {config['expected_redirect_path']}"
            )

    @pytest.mark.parametrize("host,config", SERVER_PARAMS)
    def test_api_accessibility(self, host: str, config: GerritServerConfig) -> None:
        """Test that discovered APIs are accessible."""
        print(f"\n🌐 Testing API accessibility for {host}")

        base_url = config["expected_base_url"]
        is_accessible, error = check_gerrit_api_access(base_url, timeout=30.0)

        if is_accessible:
            print(f"✅ API accessible at {base_url}")
        else:
            pytest.fail(f"API not accessible at {base_url}: {error}")

    @pytest.mark.parametrize("host,config", SERVER_PARAMS)
    def test_full_api_client_integration(
        self, host: str, config: GerritServerConfig
    ) -> None:
        """Test full integration with API client."""
        print(f"\n🔧 Testing full API client integration for {host}")

        try:
            # Load configuration (this will trigger discovery)
            gerrit_config = load_config(host=host)

            print(f"✅ Config loaded with base URL: {gerrit_config.base_url}")
            assert gerrit_config.base_url == config["expected_base_url"]

            # Test API client
            with GerritAPIClient(gerrit_config) as client:
                projects = client.fetch_projects()

                print(f"✅ Fetched {len(projects)} projects")
                assert len(projects) >= config["min_projects"], (
                    f"Expected at least {config['min_projects']} projects, got {len(projects)}"
                )

                # Check for sample projects
                project_names = [p.name for p in projects]
                for sample_project in config["sample_projects"]:
                    if sample_project in project_names:
                        print(f"✅ Found expected project: {sample_project}")
                        break
                else:
                    print(
                        f"⚠️  None of the expected sample projects found: {config['sample_projects']}"
                    )
                    print(f"   Available projects (first 5): {project_names[:5]}")
                    # Don't fail the test for this, as project names can change

        except Exception as e:
            pytest.fail(f"Full integration test failed for {host}: {e}")

    def test_convenience_function_integration(self):
        """Test the convenience function with real servers."""
        print("\n🛠️  Testing convenience function with real servers")

        # Test with one known server
        host = "gerrit.o-ran-sc.org"
        config = REAL_GERRIT_SERVERS[host]

        try:
            base_url = discover_gerrit_base_url(host, timeout=30.0)

            print(f"✅ Convenience function returned: {base_url}")
            assert base_url == config["expected_base_url"]

        except GerritDiscoveryError as e:
            pytest.fail(f"Convenience function failed for {host}: {e}")

    def test_multiple_hosts_discovery(self, discovery_client):
        """Test discovering multiple hosts at once."""
        print("\n🌍 Testing multiple hosts discovery")

        hosts = list(REAL_GERRIT_SERVERS.keys())

        try:
            results = discovery_client.discover_multiple_hosts(hosts)

            print(f"✅ Discovered {len(results)} hosts")
            assert len(results) == len(hosts)

            for host, base_url in results.items():
                expected_url = REAL_GERRIT_SERVERS[host]["expected_base_url"]
                print(f"   {host} -> {base_url}")
                assert base_url == expected_url, (
                    f"For {host}: expected {expected_url}, got {base_url}"
                )

        except GerritDiscoveryError as e:
            pytest.fail(f"Multiple hosts discovery failed: {e}")

    def test_performance_benchmarks(self, discovery_client):
        """Test discovery performance benchmarks."""
        print("\n⏱️  Testing discovery performance")

        # Test single host discovery time
        host = "gerrit.o-ran-sc.org"  # Smallest server for faster testing

        start_time = time.time()
        base_url = discovery_client.discover_base_url(host)
        discovery_time = time.time() - start_time

        print(f"✅ Discovery for {host} took {discovery_time:.2f} seconds")
        print(f"   Result: {base_url}")

        # Discovery should be reasonably fast (under 10 seconds)
        assert discovery_time < 10.0, f"Discovery took too long: {discovery_time:.2f}s"

    @pytest.mark.parametrize("host", HOST_PARAMS)
    def test_discovery_idempotency(
        self, host: str, discovery_client: GerritAPIDiscovery
    ) -> None:
        """Test that discovery is idempotent (same result each time)."""
        print(f"\n🔄 Testing discovery idempotency for {host}")

        expected_url = REAL_GERRIT_SERVERS[host]["expected_base_url"]

        # Run discovery multiple times
        results = []
        for i in range(3):
            base_url = discovery_client.discover_base_url(host)
            results.append(base_url)
            print(f"   Run {i + 1}: {base_url}")

        # All results should be identical
        assert all(url == expected_url for url in results), (
            f"Discovery results not consistent: {results}"
        )
        assert all(url == results[0] for url in results), (
            f"Discovery results varied across runs: {results}"
        )


@pytest.mark.integration
class TestDiscoveryErrorHandling:
    """Test error handling with real network conditions."""

    @pytest.fixture(autouse=True)
    def check_integration_tests(self):
        """Auto-skip integration tests when appropriate."""
        should_skip, reason = should_skip_integration_tests()
        if should_skip:
            pytest.skip(reason)

    def test_nonexistent_host(self):
        """Test discovery with non-existent host."""
        print("\n❌ Testing discovery with non-existent host")

        with pytest.raises(GerritDiscoveryError) as exc_info:
            discover_gerrit_base_url("nonexistent.gerrit.invalid", timeout=5.0)

        print(f"✅ Correctly failed with: {exc_info.value}")
        assert "Could not discover Gerrit API endpoint" in str(exc_info.value)

    def test_non_gerrit_server(self):
        """Test discovery with a real server that's not Gerrit."""
        print("\n🌐 Testing discovery with non-Gerrit server")

        with pytest.raises(GerritDiscoveryError) as exc_info:
            discover_gerrit_base_url("httpbin.org", timeout=10.0)

        print(f"✅ Correctly failed with: {exc_info.value}")
        assert "Could not discover Gerrit API endpoint" in str(exc_info.value)

    def test_timeout_handling(self):
        """Test discovery with very short timeout."""
        print("\n⏰ Testing discovery with short timeout")

        # Use a real Gerrit server but with very short timeout
        with pytest.raises(GerritDiscoveryError) as exc_info:
            discover_gerrit_base_url("gerrit.linuxfoundation.org", timeout=0.1)

        print(f"✅ Timeout handled correctly: {exc_info.value}")
