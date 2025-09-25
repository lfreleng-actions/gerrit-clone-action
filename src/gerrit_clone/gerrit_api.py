# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2025 Matthew Watkins <mwatkins@linuxfoundation.org>

"""Gerrit API client for fetching project information."""

from __future__ import annotations

import json
from typing import Any

import httpx

from gerrit_clone.logging import get_logger
from gerrit_clone.models import Config, Project, ProjectState
from gerrit_clone.retry import RetryableError, execute_with_retry
from gerrit_clone.rich_status import discovering_projects, projects_found

logger = get_logger(__name__)


class GerritAPIError(Exception):
    """Base exception for Gerrit API errors."""


class GerritConnectionError(RetryableError):
    """Raised when connection to Gerrit server fails."""


class GerritAuthenticationError(GerritAPIError):
    """Raised when authentication with Gerrit server fails."""


class GerritParseError(GerritAPIError):
    """Raised when Gerrit API response cannot be parsed."""


class GerritAPIClient:
    """Client for interacting with Gerrit REST API."""

    def __init__(self, config: Config) -> None:
        """Initialize Gerrit API client.

        Args:
            config: Configuration containing connection details
        """
        self.config = config
        self.base_url = config.base_url
        self.timeout = httpx.Timeout(30.0, connect=10.0)

        # Create HTTP client with reasonable defaults
        # base_url is guaranteed to be set by Config.__post_init__
        assert self.base_url is not None
        self.client = httpx.Client(
            base_url=self.base_url,
            timeout=self.timeout,
            follow_redirects=True,
            headers={
                "User-Agent": "gerrit-clone/0.1.0",
                "Accept": "application/json",
            },
        )

    def __enter__(self) -> GerritAPIClient:
        """Enter context manager."""
        return self

    def __exit__(self, *args: Any) -> None:
        """Exit context manager and cleanup."""
        self.close()

    def close(self) -> None:
        """Close the HTTP client and cleanup resources."""
        if hasattr(self, "client"):
            self.client.close()

    def fetch_projects(self) -> list[Project]:
        """Fetch all projects from Gerrit server.

        Returns:
            List of Project objects

        Raises:
            GerritAPIError: If API request fails or response is invalid
        """
        logger.debug("Discovering projects on %s", self.config.host)
        discovering_projects(self.config.host)

        try:
            # Execute with retry for transient failures
            response_data = execute_with_retry(
                self._fetch_projects_request,
                self.config.retry_policy,
                f"fetch projects from {self.config.host}",
            )

            # Parse projects from response
            projects = self._parse_projects_response(response_data)

            logger.debug("Found %d projects to process", len(projects))
            projects_found(len(projects))
            return projects

        except Exception as e:
            logger.error(f"Failed to fetch projects: {e}")
            raise

    def _fetch_projects_request(self) -> dict[str, Any]:
        """Make HTTP request to fetch projects.

        Returns:
            Parsed JSON response

        Raises:
            GerritConnectionError: For connection/network issues
            GerritAuthenticationError: For auth failures
            GerritAPIError: For other API errors
        """
        try:
            # Make request to projects API
            response = self.client.get("/projects/?d")

            # Handle different HTTP status codes
            if response.status_code == 200:
                return self._parse_json_response(response.text)
            elif response.status_code == 401:
                raise GerritAuthenticationError(
                    f"Authentication failed (HTTP {response.status_code}). "
                    "Check credentials or server configuration."
                )
            elif response.status_code == 403:
                raise GerritAuthenticationError(
                    f"Access forbidden (HTTP {response.status_code}). "
                    "Check permissions or server configuration."
                )
            elif response.status_code == 404:
                raise GerritAPIError(
                    f"Projects API not found (HTTP {response.status_code}). "
                    f"Check server URL: {self.base_url}"
                )
            elif response.status_code >= 500:
                raise GerritConnectionError(
                    f"Server error (HTTP {response.status_code}): {response.text}"
                )
            elif response.status_code == 429:
                raise GerritConnectionError(
                    f"Rate limited (HTTP {response.status_code}). "
                    "Too many requests to server."
                )
            else:
                raise GerritAPIError(
                    f"Unexpected HTTP status {response.status_code}: {response.text}"
                )

        except httpx.ConnectError as e:
            raise GerritConnectionError(f"Connection failed: {e}") from e
        except httpx.TimeoutException as e:
            raise GerritConnectionError(f"Request timeout: {e}") from e
        except httpx.NetworkError as e:
            raise GerritConnectionError(f"Network error: {e}") from e
        except httpx.HTTPError as e:
            raise GerritConnectionError(f"HTTP error: {e}") from e

    def _strip_gerrit_prefix(self, response_text: str) -> str:
        """Strip Gerrit's security prefix from response text.

        Gerrit API responses often start with ")]}'" to prevent XSS.

        Args:
            response_text: Raw response text

        Returns:
            Response text with prefix removed
        """
        if response_text.startswith(")]}'"):
            return response_text[4:].lstrip()
        return response_text

    def _parse_json_response(self, response_text: str) -> dict[str, Any]:
        """Parse Gerrit JSON response, handling magic prefix.

        Args:
            response_text: Raw response text

        Returns:
            Parsed JSON data

        Raises:
            GerritParseError: If response cannot be parsed
        """
        try:
            # Remove Gerrit's magic prefix if present
            clean_text = self._strip_gerrit_prefix(response_text)

            # Parse JSON
            result = json.loads(clean_text)
            return result if isinstance(result, dict) else {}

        except json.JSONDecodeError as e:
            raise GerritParseError(f"Invalid JSON response: {e}") from e
        except Exception as e:
            raise GerritParseError(f"Failed to parse response: {e}") from e

    def _parse_project_data(
        self, project_name: str, project_info: dict[str, Any]
    ) -> Project:
        """Parse individual project data from Gerrit API response.

        Args:
            project_name: The name of the project
            project_info: Project information dictionary

        Returns:
            Project object

        Raises:
            GerritParseError: If project data is malformed
        """
        try:
            # Extract project state (default to ACTIVE if not specified)
            state_str = "ACTIVE"
            if isinstance(project_info, dict):
                state_str = project_info.get("state", "ACTIVE")

            # Parse state enum
            try:
                state = ProjectState(state_str)
            except ValueError:
                logger.warning(
                    f"Unknown project state '{state_str}' for {project_name}, "
                    "treating as ACTIVE"
                )
                state = ProjectState.ACTIVE

            # Extract description
            description = None
            if isinstance(project_info, dict):
                description = project_info.get("description")

            # Extract web links
            web_links = None
            if isinstance(project_info, dict) and "web_links" in project_info:
                web_links = project_info["web_links"]
                if not isinstance(web_links, list):
                    web_links = None

            # Create project object
            return Project(
                name=project_name,
                state=state,
                description=description,
                web_links=web_links,
            )

        except Exception as e:
            raise GerritParseError(
                f"Failed to parse project '{project_name}': {e}"
            ) from e

    def _parse_projects_response(self, data: dict[str, Any]) -> list[Project]:
        """Parse projects from Gerrit API response.

        Args:
            data: Parsed JSON response from /projects/ endpoint

        Returns:
            List of Project objects

        Raises:
            GerritParseError: If response format is unexpected
        """
        if not isinstance(data, dict):
            raise GerritParseError("Expected object/dict response from projects API")

        projects = []
        system_projects = {"All-Projects", "All-Users"}

        for project_name, project_info in data.items():
            try:
                # Skip system meta-projects
                if project_name in system_projects:
                    logger.debug(f"Skipping system project: {project_name}")
                    continue

                # Parse individual project
                project = self._parse_project_data(project_name, project_info)
                projects.append(project)

            except GerritParseError as e:
                logger.warning(f"Skipping malformed project '{project_name}': {e}")
                continue

        return projects

    def filter_projects(self, projects: list[Project]) -> tuple[list[Project], dict[str, int]]:
        """Filter projects based on configuration.

        Args:
            projects: List of all projects

        Returns:
            Tuple of (filtered list of projects, filtering stats dict)
        """
        if not self.config.skip_archived:
            # Include all projects
            filtered = projects
        else:
            # Only include ACTIVE projects
            filtered = [p for p in projects if p.is_active]

        skipped_count = len(projects) - len(filtered)

        stats = {
            "total": len(projects),
            "filtered": len(filtered),
            "skipped": skipped_count
        }

        return filtered, stats


def fetch_gerrit_projects(config: Config) -> tuple[list[Project], dict[str, int]]:
    """Convenience function to fetch and filter projects.

    Args:
        config: Configuration with connection details

    Returns:
        Tuple of (filtered Project objects, filtering stats)
    """
    with GerritAPIClient(config) as client:
        projects = client.fetch_projects()
        return client.filter_projects(projects)
