# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2025 Matthew Watkins <mwatkins@linuxfoundation.org>

"""Unified project discovery coordinator.

This module coordinates project discovery for both Gerrit and GitHub sources,
using HTTP, SSH, or both methods for Gerrit, and GitHub API for GitHub.
Provides warnings when the methods return different results.
"""

from __future__ import annotations

from typing import Any

from gerrit_clone.error_codes import (
    DiscoveryError,
)
from gerrit_clone.gerrit_api import fetch_gerrit_projects
from gerrit_clone.logging import get_logger
from gerrit_clone.models import Config, DiscoveryMethod, Project, SourceType
from gerrit_clone.ssh_discovery import fetch_gerrit_projects_ssh

logger = get_logger(__name__)


class DiscoveryWarning:
    """Warning about discovery method discrepancies."""

    def __init__(
        self,
        message: str,
        http_count: int | None = None,
        ssh_count: int | None = None,
        missing_in_http: list[str] | None = None,
        missing_in_ssh: list[str] | None = None,
    ) -> None:
        """Initialize discovery warning.

        Args:
            message: Warning message
            http_count: Number of projects found via HTTP
            ssh_count: Number of projects found via SSH
            missing_in_http: Projects found in SSH but not HTTP
            missing_in_ssh: Projects found in HTTP but not SSH
        """
        self.message = message
        self.http_count = http_count
        self.ssh_count = ssh_count
        self.missing_in_http = missing_in_http or []
        self.missing_in_ssh = missing_in_ssh or []

    def __str__(self) -> str:
        """Get string representation of warning."""
        parts = [self.message]

        if self.http_count is not None and self.ssh_count is not None:
            parts.append(
                f"HTTP: {self.http_count} projects, SSH: {self.ssh_count} projects"
            )

        if self.missing_in_http:
            parts.append(
                f"Missing in HTTP (visible only via SSH): {self._format_project_list(self.missing_in_http)}"
            )

        if self.missing_in_ssh:
            parts.append(
                f"Missing in SSH (visible only via HTTP): {self._format_project_list(self.missing_in_ssh)}"
            )

        return " | ".join(parts)

    @staticmethod
    def _format_project_list(projects: list[str], max_items: int = 10) -> str:
        """Format a project list for logging, truncating if too long."""
        if len(projects) <= max_items:
            return ", ".join(projects)
        else:
            shown = ", ".join(projects[:max_items])
            return f"{shown}, ... +{len(projects) - max_items} more"


class UnifiedDiscovery:
    """Unified project discovery coordinator."""

    def __init__(self, config: Config) -> None:
        """Initialize unified discovery.

        Args:
            config: Configuration containing discovery settings
        """
        self.config = config
        self.warnings: list[DiscoveryWarning] = []

    def discover_projects(self) -> tuple[list[Project], dict[str, Any]]:
        """Discover projects using configured method.

        Returns:
            Tuple of (projects list, stats dict with warnings)

        Raises:
            Exception: If discovery fails
        """
        if self.config.source_type == SourceType.GITHUB:
            return self._discover_github()

        # Gerrit discovery methods
        if self.config.discovery_method == DiscoveryMethod.SSH:
            return self._discover_ssh()
        elif self.config.discovery_method == DiscoveryMethod.HTTP:
            return self._discover_http()
        elif self.config.discovery_method == DiscoveryMethod.BOTH:
            return self._discover_both()
        elif self.config.discovery_method == DiscoveryMethod.GITHUB_API:
            return self._discover_github()
        else:
            raise ValueError(
                f"Unknown discovery method: {self.config.discovery_method}"
            )

    def _discover_github(self) -> tuple[list[Project], dict[str, Any]]:
        """Discover GitHub repositories via API.

        Returns:
            Tuple of (projects, stats)

        Raises:
            DiscoveryError: If GitHub discovery fails
        """
        logger.debug("Discovering GitHub repositories via API")
        try:
            from gerrit_clone.github_discovery import (  # noqa: PLC0415
                discover_github_repositories,
            )

            projects, stats = discover_github_repositories(self.config)
            stats["warnings"] = []
            return projects, stats
        except Exception as e:
            logger.error(f"GitHub discovery failed: {e}")
            raise DiscoveryError(
                message="GitHub API discovery failed",
                details=str(e),
                original_exception=e,
            ) from e

    def _discover_ssh(self) -> tuple[list[Project], dict[str, Any]]:
        """Discover projects via SSH only.

        Returns:
            Tuple of (projects, stats)

        Raises:
            DiscoveryError: If SSH discovery fails
        """
        logger.debug("Discovering projects via SSH")
        try:
            projects, stats = fetch_gerrit_projects_ssh(self.config)

            stats["discovery_method"] = "ssh"  # type: ignore[assignment]
            stats["warnings"] = []  # type: ignore[assignment]

            return projects, stats
        except Exception as e:
            logger.error(f"SSH discovery failed: {e}")
            raise DiscoveryError(
                message="SSH discovery failed",
                details=str(e),
                original_exception=e,
            ) from e

    def _discover_http(self) -> tuple[list[Project], dict[str, Any]]:
        """Discover projects via HTTP only.

        Returns:
            Tuple of (projects, stats)

        Raises:
            DiscoveryError: If HTTP discovery fails
        """
        logger.debug("Discovering projects via HTTP")
        try:
            projects, stats = fetch_gerrit_projects(self.config)

            stats["discovery_method"] = "http"  # type: ignore[assignment]
            stats["warnings"] = []  # type: ignore[assignment]

            return projects, stats
        except Exception as e:
            logger.error(f"HTTP discovery failed: {e}")
            raise DiscoveryError(
                message="HTTP discovery failed",
                details=str(e),
                original_exception=e,
            ) from e

    def _discover_both(self) -> tuple[list[Project], dict[str, Any]]:
        """Discover projects via both methods and compare.

        Attempts both HTTP and SSH discovery. If one fails, warns and continues
        with the other. If both fail, exits with fatal error.

        Returns:
            Tuple of (projects, stats with comparison data and warnings)
        """
        logger.debug("Discovering projects via both HTTP and SSH for comparison")

        http_projects = None
        ssh_projects = None
        http_stats = None
        ssh_stats = None
        http_error = None
        ssh_error = None

        # Attempt HTTP discovery
        try:
            logger.debug("Attempting HTTP discovery...")
            http_projects, http_stats = fetch_gerrit_projects(self.config)
            logger.debug(
                f"HTTP discovery succeeded: found {len(http_projects)} projects"
            )
        except Exception as e:
            http_error = e
            logger.warning(f"HTTP discovery failed: {e}")

        # Attempt SSH discovery
        try:
            logger.debug("Attempting SSH discovery...")
            ssh_projects, ssh_stats = fetch_gerrit_projects_ssh(self.config)
            logger.debug(f"SSH discovery succeeded: found {len(ssh_projects)} projects")
        except Exception as e:
            ssh_error = e
            logger.warning(f"SSH discovery failed: {e}")

        # Check if both methods failed
        if http_projects is None and ssh_projects is None:
            # Both methods failed - this is a fatal error
            error_details = []
            if http_error:
                error_details.append(f"HTTP: {http_error}")
            if ssh_error:
                error_details.append(f"SSH: {ssh_error}")

            details = "; ".join(error_details)
            raise DiscoveryError(
                message="Both HTTP and SSH discovery methods failed",
                details=details,
                original_exception=ssh_error or http_error,
            )

        # At least one method succeeded - determine which to use as authoritative
        if ssh_projects is not None and http_projects is not None:
            # Both succeeded - create union of both results
            # Type assertion: we know these are not None due to the check above
            assert http_stats is not None
            assert ssh_stats is not None
            return self._merge_discovery_results(
                http_projects, http_stats, ssh_projects, ssh_stats
            )
        elif ssh_projects is not None:
            # Only SSH succeeded
            # Type assertion: we know ssh_stats is not None if ssh_projects is not None
            assert ssh_stats is not None
            ssh_only_stats: dict[str, Any] = ssh_stats.copy()
            ssh_only_stats["discovery_method"] = "ssh_only_fallback"
            warning = DiscoveryWarning(
                message="HTTP discovery failed, using SSH results only",
            )
            self.warnings.append(warning)
            ssh_only_stats["warnings"] = [str(warning)]
            logger.info("Using SSH discovery results (HTTP failed)")
            return ssh_projects, ssh_only_stats
        else:
            # Only HTTP succeeded
            # Type assertion: we know http_stats and http_projects are not None
            assert http_projects is not None
            assert http_stats is not None
            http_only_stats: dict[str, Any] = http_stats.copy()
            http_only_stats["discovery_method"] = "http_only_fallback"
            warning = DiscoveryWarning(
                message="SSH discovery failed, using HTTP results only",
            )
            self.warnings.append(warning)
            http_only_stats["warnings"] = [str(warning)]
            logger.info("Using HTTP discovery results (SSH failed)")
            return http_projects, http_only_stats

    def _merge_discovery_results(
        self,
        http_projects: list[Project],
        http_stats: dict[str, Any],
        ssh_projects: list[Project],
        ssh_stats: dict[str, Any],
    ) -> tuple[list[Project], dict[str, Any]]:
        """Merge results from both discovery methods into a union.

        Creates a union of projects from both HTTP and SSH discovery methods.
        For duplicate projects (same name), SSH metadata is preferred as it
        typically provides more complete information.

        Args:
            http_projects: Projects discovered via HTTP
            http_stats: HTTP discovery statistics
            ssh_projects: Projects discovered via SSH
            ssh_stats: SSH discovery statistics

        Returns:
            Tuple of (merged projects union, combined stats with warnings)
        """
        http_names = {p.name for p in http_projects}
        ssh_names = {p.name for p in ssh_projects}

        missing_in_http = sorted(ssh_names - http_names)
        missing_in_ssh = sorted(http_names - ssh_names)

        # Create union of projects, preferring SSH metadata for duplicates
        merged_projects = self._create_project_union(http_projects, ssh_projects)

        stats: dict[str, Any] = {
            "total": len(merged_projects),
            "filtered": max(ssh_stats["filtered"], http_stats["filtered"]),
            "skipped": max(ssh_stats["skipped"], http_stats["skipped"]),
            "discovery_method": "both",
            "http_total": http_stats["total"],
            "ssh_total": ssh_stats["total"],
            "warnings": [],
        }

        # Generate warnings if there are differences
        if missing_in_http or missing_in_ssh:
            if missing_in_http:
                warning = DiscoveryWarning(
                    message=f"Found {len(missing_in_http)} project(s) in SSH that are not visible via HTTP",
                    http_count=len(http_projects),
                    ssh_count=len(ssh_projects),
                    missing_in_http=missing_in_http,
                )
                self.warnings.append(warning)
                stats["warnings"].append(str(warning))
                logger.warning(str(warning))

            if missing_in_ssh:
                warning = DiscoveryWarning(
                    message=f"Found {len(missing_in_ssh)} project(s) in HTTP that are not visible via SSH",
                    http_count=len(http_projects),
                    ssh_count=len(ssh_projects),
                    missing_in_ssh=missing_in_ssh,
                )
                self.warnings.append(warning)
                stats["warnings"].append(str(warning))
                logger.warning(str(warning))
        else:
            logger.info(
                f"HTTP and SSH discovery returned identical results ({len(merged_projects)} projects)"
            )

        return merged_projects, stats

    def _create_project_union(
        self, http_projects: list[Project], ssh_projects: list[Project]
    ) -> list[Project]:
        """Create a union of projects from HTTP and SSH discovery.

        For duplicate projects (same name), SSH metadata is preferred as it
        typically provides more complete project information.

        Args:
            http_projects: Projects discovered via HTTP
            ssh_projects: Projects discovered via SSH

        Returns:
            List of unique projects with SSH metadata preferred for duplicates
        """
        # Create mapping of project names to projects, with SSH taking precedence
        project_map: dict[str, Project] = {}

        # First add HTTP projects
        for project in http_projects:
            project_map[project.name] = project

        # Then add SSH projects (overwriting HTTP for duplicates)
        for project in ssh_projects:
            project_map[project.name] = project

        return sorted(project_map.values(), key=lambda p: p.name)


def discover_projects(config: Config) -> tuple[list[Project], dict[str, Any]]:
    """Convenience function for unified project discovery.

    Supports both Gerrit and GitHub sources.

    Args:
        config: Configuration with discovery settings

    Returns:
        Tuple of (projects, stats dict with warnings)
    """
    discovery = UnifiedDiscovery(config)
    return discovery.discover_projects()
