# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""Validation and normalization rules for :class:`~source_clone.models.Config`.

Holds the logic invoked from ``Config.__post_init__``: field validation,
discovery-method resolution, mirror-mode option overrides, project filter
normalization and base URL derivation. Imports the configuration type only
for type checking so it remains a leaf module.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from source_clone.model_enums import DiscoveryMethod, SourceType
from source_clone.model_project_filters import normalize_project_list

if TYPE_CHECKING:
    from source_clone.models import Config


def validate_and_normalize(config: Config) -> None:
    """Validate and normalize a freshly constructed configuration.

    Applied in a fixed order: connection settings are checked before the
    discovery method is resolved, and project filters are normalized before
    the base URL is derived, so later steps can rely on earlier ones.

    Args:
        config: Configuration instance to validate in place.

    Raises:
        ValueError: If any field or combination of fields is invalid.
    """
    if not config.host:
        raise ValueError("host is required")

    _apply_port_defaults(config)

    # Resolve discovery method as a single source of truth for protocol
    # selection. ``port`` is the SSH port only; HTTPS discovery/cloning use
    # ``base_url``. This tool treats SSH-based discovery as incompatible
    # with HTTPS clone mode (regardless of whether SSH credentials exist).
    resolve_discovery_method(config)

    _validate_clone_limits(config)
    _apply_mirror_overrides(config)

    # Normalize include/exclude project lists (strip whitespace, split
    # on commas/spaces, drop empties, de-dup while preserving order)
    if config.include_projects:
        object.__setattr__(
            config, "include_projects", normalize_project_list(config.include_projects)
        )
    if config.exclude_projects:
        object.__setattr__(
            config, "exclude_projects", normalize_project_list(config.exclude_projects)
        )

    # Ensure path is absolute
    config.path = config.path.resolve()

    # Generate base_url if not provided
    if config.base_url is None:
        _derive_base_url(config)


def _apply_port_defaults(config: Config) -> None:
    """Apply the default Gerrit SSH port and validate the port range."""
    # Set default port based on source type if not explicitly provided
    if config.port is None and config.source_type == SourceType.GERRIT:
        # Default Gerrit SSH port
        config.port = 29418
        # For GitHub, leave port as None (not used)

    # Validate port range for Gerrit sources
    # After applying defaults above, port is guaranteed non-None for Gerrit
    # Port is only meaningful for Gerrit (SSH/HTTP endpoint configuration)
    # For GitHub sources, port should be None and is not validated
    if config.source_type == SourceType.GERRIT:
        # Type assertion for mypy - port is guaranteed non-None after defaults
        assert config.port is not None
        if config.port <= 0 or config.port > 65535:
            raise ValueError("port must be between 1 and 65535")


def resolve_discovery_method(config: Config) -> None:
    """Resolve and validate the project discovery method.

    ``discovery_method`` may be ``None`` to request derivation from the
    source type and clone protocol. This keeps discovery consistent with
    cloning and fast-fails on contradictory combinations instead of
    producing an obscure runtime connection error (for example, attempting
    SSH discovery against the HTTPS port).
    """
    if config.source_type == SourceType.GITHUB:
        # GitHub discovery always uses the GitHub API path, regardless of
        # the requested method. Normalize to GITHUB_API so the resolved
        # value is unambiguous (HTTP would be misleading here).
        config.discovery_method = DiscoveryMethod.GITHUB_API
        return

    # Gerrit source: GitHub API discovery is meaningless and would route
    # to the GitHub backend, failing in confusing ways. Reject it early.
    if config.discovery_method == DiscoveryMethod.GITHUB_API:
        raise ValueError(
            "discovery_method='github_api' is only valid for GitHub "
            "sources, not Gerrit. Use 'ssh', 'http', or 'both'."
        )

    if config.use_https:
        if config.discovery_method is None:
            # HTTPS cloning pairs with HTTP REST discovery.
            config.discovery_method = DiscoveryMethod.HTTP
        elif config.discovery_method in (
            DiscoveryMethod.SSH,
            DiscoveryMethod.BOTH,
        ):
            raise ValueError(
                "use_https requires HTTP-based project discovery, but "
                f"discovery_method='{config.discovery_method.value}' needs "
                "SSH. SSH-based discovery is incompatible with HTTPS clone "
                "mode in this tool. Use discovery_method='http' (the "
                "default with HTTPS) or clone over SSH instead."
            )
    elif config.discovery_method is None:
        # SSH cloning pairs with SSH discovery by default.
        config.discovery_method = DiscoveryMethod.SSH


def _validate_clone_limits(config: Config) -> None:
    """Validate the numeric clone limits (threads, depth, timeout)."""
    if config.threads is not None and config.threads < 1:
        raise ValueError("threads must be at least 1")

    if config.depth is not None and config.depth < 1:
        raise ValueError("depth must be at least 1")

    if config.clone_timeout <= 0:
        raise ValueError("clone_timeout must be positive")


def _apply_mirror_overrides(config: Config) -> None:
    """Drop options that mirror mode cannot honour, warning as they are cleared."""
    if not config.mirror:
        return

    # --mirror is incompatible with --depth and --branch
    # When mirror is enabled, we override these options
    if config.depth is not None:
        logger = __import__("source_clone.logging", fromlist=["get_logger"]).get_logger(
            __name__
        )
        logger.warning(
            "mirror mode is incompatible with --depth option. "
            "Ignoring --depth and using full clone."
        )
        object.__setattr__(config, "depth", None)

    if config.branch is not None:
        logger = __import__("source_clone.logging", fromlist=["get_logger"]).get_logger(
            __name__
        )
        logger.warning(
            "mirror mode is incompatible with --branch option. "
            "Ignoring --branch and cloning all refs."
        )
        object.__setattr__(config, "branch", None)


def _derive_base_url(config: Config) -> None:
    """Derive the API base URL for the configured source when unset."""
    if config.source_type == SourceType.GERRIT:
        from source_clone.discovery import (  # noqa: PLC0415
            discover_gerrit_base_url,
        )

        try:
            config.base_url = discover_gerrit_base_url(config.host)
        except Exception as e:
            # Fall back to basic URL if discovery fails
            logger = __import__(
                "source_clone.logging", fromlist=["get_logger"]
            ).get_logger(__name__)
            logger.debug(
                f"API discovery failed for {config.host}, using basic URL: {e}"
            )
            # aislop-ignore-next-line hardcoded-url -- URL built from configured host, not a hardcoded endpoint
            config.base_url = f"https://{config.host}"
    elif config.source_type == SourceType.GITHUB:
        from source_clone.github_transport import github_api_url  # noqa: PLC0415

        config.base_url = github_api_url(config.host)
