# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2025 Matthew Watkins <mwatkins@linuxfoundation.org>

"""Gerrit API base URL discovery module.

This module provides functionality to automatically discover the correct API base URL
for different Gerrit server configurations by testing common patterns and following redirects.
"""

from __future__ import annotations

import json
from typing import Any, ClassVar
from urllib.parse import urljoin, urlparse

import httpx

from gerrit_clone.logging import get_logger

logger = get_logger(__name__)


class GerritDiscoveryError(Exception):
    """Raised when Gerrit API discovery fails."""


class GerritAPIDiscovery:
    """Discovers the correct Gerrit API base URL for a given host."""

    # Common Gerrit API path patterns to test
    COMMON_PATHS: ClassVar[list[str]] = [
        "",  # Direct: https://host/
        "/r",  # Standard: https://host/r/
        "/gerrit",  # OpenDaylight style: https://host/gerrit/
        "/infra",  # Linux Foundation style: https://host/infra/
        "/a",  # Authenticated API: https://host/a/
    ]

    def __init__(self, timeout: float = 30.0) -> None:
        """Initialize discovery client.

        Args:
            timeout: HTTP request timeout in seconds
        """
        self.timeout = httpx.Timeout(timeout, connect=10.0)
        self.client = httpx.Client(
            timeout=self.timeout,
            follow_redirects=True,
            headers={
                "User-Agent": "gerrit-clone/0.1.0",
                "Accept": "application/json",
            },
        )

    def __enter__(self) -> GerritAPIDiscovery:
        """Enter context manager."""
        return self

    def __exit__(self, *args: Any) -> None:
        """Exit context manager and cleanup."""
        self.close()

    def close(self) -> None:
        """Close HTTP client."""
        if hasattr(self, "client"):
            self.client.close()

    def discover_base_url(self, host: str) -> str:
        """Discover the correct API base URL for a Gerrit host.

        Args:
            host: Gerrit server hostname

        Returns:
            The discovered base URL (e.g., "https://host/r")

        Raises:
            GerritDiscoveryError: If no valid API endpoint is found
        """
        logger.debug(f"Starting API discovery for host: {host}")

        # First, try to follow redirects from the base URL
        redirect_path = self._discover_via_redirect(host)
        if redirect_path:
            logger.debug(f"Found redirect path: {redirect_path}")
            test_paths = [redirect_path] + [
                p for p in self.COMMON_PATHS if p != redirect_path
            ]
        else:
            test_paths = self.COMMON_PATHS

        # Test each potential path
        for path in test_paths:
            base_url = f"https://{host}{path}"
            logger.debug(f"Testing API endpoint: {base_url}")

            if self._test_projects_api(base_url):
                logger.debug(f"Discovered working API base URL: {base_url}")
                return base_url

        # If all paths fail, raise an error
        raise GerritDiscoveryError(
            f"Could not discover Gerrit API endpoint for {host}. "
            f"Tested paths: {test_paths}. "
            "Please check if the server is accessible and has the projects API enabled."
        )

    def _discover_via_redirect(self, host: str) -> str | None:
        """Attempt to discover the API path by following redirects.

        Args:
            host: Gerrit server hostname

        Returns:
            The discovered path from redirect, or None if no useful redirect found
        """
        try:
            logger.debug(f"Checking for redirects from https://{host}")
            response = self.client.get(f"https://{host}", follow_redirects=False)

            if response.status_code in (301, 302, 303, 307, 308):
                location = response.headers.get("location")
                if location:
                    logger.debug(f"Found redirect to: {location}")

                    # Parse the redirect URL to extract the path
                    parsed = urlparse(location)
                    if parsed.netloc == host or not parsed.netloc:
                        # Same host redirect or relative redirect
                        path: str = parsed.path.rstrip("/")
                        if path and path != "/":
                            logger.debug(f"Extracted path from redirect: {path}")
                            return path

        except Exception as e:
            logger.debug(f"Error checking redirects for {host}: {e}")

        return None

    def _test_projects_api(self, base_url: str) -> bool:
        """Test if the projects API is available at the given base URL.

        Args:
            base_url: Base URL to test (e.g., "https://host/r")

        Returns:
            True if the API is working, False otherwise
        """
        try:
            projects_url = urljoin(base_url.rstrip("/") + "/", "projects/?d")
            logger.debug(f"Testing projects API at: {projects_url}")

            response = self.client.get(projects_url)

            if response.status_code == 200:
                # Try to parse the response to ensure it's valid Gerrit API
                if self._validate_projects_response(response.text):
                    logger.debug(f"Valid projects API response from: {projects_url}")
                    return True
                else:
                    logger.debug(
                        f"Invalid projects API response format from: {projects_url}"
                    )
                    return False
            else:
                logger.debug(
                    f"Projects API returned HTTP {response.status_code} from: {projects_url}"
                )
                return False

        except Exception as e:
            logger.debug(f"Error testing projects API at {base_url}: {e}")
            return False

    def _validate_projects_response(self, response_text: str) -> bool:
        """Validate that the response looks like a valid Gerrit projects API response.

        Args:
            response_text: Raw response text from the API

        Returns:
            True if the response appears to be valid Gerrit API output
        """
        try:
            # Gerrit API responses typically start with ")]}'" to prevent CSRF
            if response_text.startswith(")]}'"):
                json_text = response_text[4:]  # Remove the prefix
            else:
                json_text = response_text

            # Try to parse as JSON
            data = json.loads(json_text)

            # Should be a dictionary with project names as keys
            if isinstance(data, dict):
                # Check if it looks like projects data
                for _key, value in data.items():
                    if isinstance(value, dict) and "id" in value:
                        # This looks like a project entry
                        logger.debug(
                            "Response appears to be valid Gerrit projects API output"
                        )
                        return True

                # Empty projects list is also valid
                if len(data) == 0:
                    logger.debug("Response is empty projects list (valid)")
                    return True

            logger.debug("Response does not match expected Gerrit projects API format")
            return False

        except json.JSONDecodeError:
            logger.debug("Response is not valid JSON")
            return False
        except Exception as e:
            logger.debug(f"Error validating response: {e}")
            return False

    def discover_multiple_hosts(self, hosts: list[str]) -> dict[str, str]:
        """Discover API base URLs for multiple hosts.

        Args:
            hosts: List of Gerrit hostnames

        Returns:
            Dictionary mapping hostnames to their discovered base URLs

        Raises:
            GerritDiscoveryError: If any host fails discovery
        """
        results = {}
        failures = []

        for host in hosts:
            try:
                base_url = self.discover_base_url(host)
                results[host] = base_url
            except GerritDiscoveryError as e:
                failures.append((host, str(e)))

        if failures:
            failure_details = "; ".join(
                [f"{host}: {error}" for host, error in failures]
            )
            raise GerritDiscoveryError(
                f"Failed to discover API for some hosts: {failure_details}"
            )

        return results


def discover_gerrit_base_url(host: str, timeout: float = 30.0) -> str:
    """Convenience function to discover Gerrit API base URL for a single host.

    Args:
        host: Gerrit server hostname
        timeout: HTTP request timeout in seconds

    Returns:
        The discovered base URL

    Raises:
        GerritDiscoveryError: If discovery fails
    """
    with GerritAPIDiscovery(timeout=timeout) as discovery:
        return discovery.discover_base_url(host)


def check_gerrit_api_access(
    base_url: str, timeout: float = 30.0
) -> tuple[bool, str | None]:
    """Test if Gerrit API is accessible at the given base URL.

    Args:
        base_url: Base URL to test
        timeout: HTTP request timeout in seconds

    Returns:
        Tuple of (is_accessible, error_message)
    """
    with GerritAPIDiscovery(timeout=timeout) as discovery:
        try:
            if discovery._test_projects_api(base_url):
                return True, None
            else:
                return False, "Projects API test failed"
        except Exception as e:
            return False, str(e)
