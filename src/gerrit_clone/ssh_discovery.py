# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2025 Matthew Watkins <mwatkins@linuxfoundation.org>

"""SSH-based Gerrit project discovery module.

This module provides functionality to discover Gerrit projects using the SSH API
(gerrit ls-projects command), which is more authoritative than the HTTP REST API
and may expose projects that are hidden from unauthenticated HTTP access.
"""

from __future__ import annotations

import json
import subprocess
from typing import Any

from gerrit_clone.logging import get_logger
from gerrit_clone.models import Config, Project, ProjectState
from gerrit_clone.retry import RetryableError, execute_with_retry
from gerrit_clone.rich_status import discovering_projects, projects_found

logger = get_logger(__name__)


class SSHDiscoveryError(Exception):
    """Base exception for SSH discovery errors."""


class SSHConnectionError(RetryableError):
    """Raised when SSH connection to Gerrit server fails."""


class SSHCommandError(SSHDiscoveryError):
    """Raised when SSH command execution fails."""


class SSHParseError(SSHDiscoveryError):
    """Raised when SSH command output cannot be parsed."""


class GerritSSHClient:
    """Client for discovering Gerrit projects via SSH."""

    def __init__(self, config: Config) -> None:
        """Initialize SSH client.

        Args:
            config: Configuration containing connection details
        """
        self.config = config

    def fetch_projects(self) -> list[Project]:
        """Fetch all projects from Gerrit server via SSH.

        Returns:
            List of Project objects

        Raises:
            SSHDiscoveryError: If SSH request fails or response is invalid
        """
        logger.debug("Discovering projects via SSH on %s", self.config.host)
        discovering_projects(self.config.host, method="ssh")

        try:
            response_data = execute_with_retry(
                self._fetch_projects_ssh,
                self.config.retry_policy,
                f"fetch projects via SSH from {self.config.host}",
            )

            projects = self._parse_projects_response(response_data)

            logger.debug("Found %d projects via SSH", len(projects))
            projects_found(len(projects), method="ssh")
            return projects

        except Exception as e:
            logger.error(f"Failed to fetch projects via SSH: {e}")
            raise

    def _fetch_projects_ssh(self) -> dict[str, Any]:
        """Execute SSH command to fetch projects.

        Returns:
            Parsed JSON response from gerrit ls-projects

        Raises:
            SSHConnectionError: For connection/network issues
            SSHCommandError: For command execution failures
            SSHParseError: For parsing errors
        """
        cmd = self._build_ssh_command()

        logger.debug(f"Executing SSH command: {' '.join(cmd)}")

        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=90,  # Match HTTP timeout
                check=False,  # Don't raise on non-zero exit
            )

            if result.returncode != 0:
                stderr = result.stderr.strip()

                # Check for common SSH errors that should be retried
                if any(
                    msg in stderr.lower()
                    for msg in [
                        "connection timed out",
                        "connection refused",
                        "connection reset",
                        "network is unreachable",
                        "temporary failure",
                    ]
                ):
                    raise SSHConnectionError(
                        f"SSH connection failed (exit {result.returncode}): {stderr}"
                    )

                if any(
                    msg in stderr.lower()
                    for msg in [
                        "permission denied",
                        "authentication failed",
                        "publickey",
                        "no such identity",
                    ]
                ):
                    raise SSHCommandError(
                        f"SSH authentication failed: {stderr}. "
                        "Check your SSH keys and permissions."
                    )

                # Other errors
                raise SSHCommandError(
                    f"SSH command failed (exit {result.returncode}): {stderr}"
                )

            if not result.stdout.strip():
                raise SSHParseError("Empty output from gerrit ls-projects")

            try:
                data = json.loads(result.stdout)
                if not isinstance(data, dict):
                    raise SSHParseError(
                        f"Expected JSON object, got {type(data).__name__}"
                    )
                return data
            except json.JSONDecodeError as e:
                raise SSHParseError(f"Invalid JSON from gerrit ls-projects: {e}") from e

        except subprocess.TimeoutExpired as e:
            raise SSHConnectionError(
                f"SSH command timed out after {e.timeout} seconds"
            ) from e
        except FileNotFoundError as e:
            raise SSHCommandError(
                "SSH client not found. Please ensure 'ssh' is installed and in PATH."
            ) from e
        except Exception as e:
            if isinstance(e, (SSHConnectionError, SSHCommandError, SSHParseError)):
                raise
            raise SSHCommandError(f"Unexpected error executing SSH command: {e}") from e

    def _build_ssh_command(self) -> list[str]:
        """Build SSH command for listing projects.

        Returns:
            List of command arguments
        """
        cmd = ["ssh"]

        # Add port
        cmd.extend(["-p", str(self.config.port)])

        # Add identity file if specified
        if self.config.ssh_identity_file:
            cmd.extend(["-i", str(self.config.ssh_identity_file)])

        # Add strict host key checking setting
        if not self.config.strict_host_checking:
            cmd.extend(
                [
                    "-o",
                    "StrictHostKeyChecking=no",
                    "-o",
                    "UserKnownHostsFile=/dev/null",
                ]
            )

        # Add SSH debug if requested
        if self.config.ssh_debug:
            cmd.append("-vvv")

        # Build host string (user@host or just host)
        if self.config.ssh_user:
            host_str = f"{self.config.ssh_user}@{self.config.host}"
        else:
            host_str = self.config.host

        cmd.append(host_str)

        # Add gerrit command
        cmd.extend(
            [
                "gerrit",
                "ls-projects",
                "--format",
                "JSON",
                "-d",  # Include project description
            ]
        )

        return cmd

    def _parse_project_data(
        self, project_name: str, project_info: dict[str, Any]
    ) -> Project:
        """Parse individual project data from SSH response.

        Args:
            project_name: The name of the project
            project_info: Project information dictionary

        Returns:
            Project object

        Raises:
            SSHParseError: If project data is malformed
        """
        try:
            # Extract project state (default to ACTIVE if not specified)
            state_str = "ACTIVE"
            if isinstance(project_info, dict):
                state_str = project_info.get("state", "ACTIVE")

            try:
                state = ProjectState(state_str)
            except ValueError:
                logger.warning(
                    f"Unknown project state '{state_str}' for {project_name}, "
                    "treating as ACTIVE"
                )
                state = ProjectState.ACTIVE

            description = None
            if isinstance(project_info, dict):
                description = project_info.get("description")

            web_links = None
            if isinstance(project_info, dict) and "web_links" in project_info:
                web_links = project_info["web_links"]
                if not isinstance(web_links, list):
                    web_links = None

            return Project(
                name=project_name,
                state=state,
                description=description,
                web_links=web_links,
            )

        except Exception as e:
            raise SSHParseError(f"Failed to parse project '{project_name}': {e}") from e

    def _parse_projects_response(self, data: dict[str, Any]) -> list[Project]:
        """Parse projects from SSH response.

        Args:
            data: Parsed JSON response from gerrit ls-projects

        Returns:
            List of Project objects

        Raises:
            SSHParseError: If response format is unexpected
        """
        if not isinstance(data, dict):
            raise SSHParseError("Expected object/dict response from gerrit ls-projects")

        projects = []
        system_projects = {"All-Projects", "All-Users"}

        for project_name, project_info in data.items():
            try:
                # Skip system meta-projects
                if project_name in system_projects:
                    logger.debug(f"Skipping system project: {project_name}")
                    continue

                project = self._parse_project_data(project_name, project_info)
                projects.append(project)

            except SSHParseError as e:
                logger.warning(f"Skipping malformed project '{project_name}': {e}")
                continue

        return projects

    def filter_projects(
        self, projects: list[Project]
    ) -> tuple[list[Project], dict[str, int]]:
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
            "skipped": skipped_count,
        }

        return filtered, stats


def fetch_gerrit_projects_ssh(config: Config) -> tuple[list[Project], dict[str, int]]:
    """Convenience function to fetch and filter projects via SSH.

    Args:
        config: Configuration with connection details

    Returns:
        Tuple of (filtered Project objects, filtering stats)
    """
    client = GerritSSHClient(config)
    projects = client.fetch_projects()
    return client.filter_projects(projects)
