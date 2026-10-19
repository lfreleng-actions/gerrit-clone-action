# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2025 The Linux Foundation

"""
Netrc file parsing for Gerrit authentication credentials.

This module provides functionality to parse .netrc files and retrieve
credentials for authenticating with Gerrit servers. It follows the
standard netrc format as documented at:
https://everything.curl.dev/usingcurl/netrc.html

The module supports:
- Standard netrc tokens: machine, login, password, default
- Quoted strings (curl 7.84.0+) with escape sequences
- Multiple search locations (local directory, home directory)
- Windows compatibility (_netrc fallback)
"""

from __future__ import annotations

import logging
import os
from typing import TYPE_CHECKING

from gerrit_clone.netrc_discovery import check_netrc_permissions, find_netrc_file
from gerrit_clone.netrc_models import (
    CredentialSource,
    GerritCredentials,
    NetrcCredentials,
    NetrcParseError,
)
from gerrit_clone.netrc_parser import NetrcParser

if TYPE_CHECKING:
    from pathlib import Path

log = logging.getLogger(__name__)


def _normalize_host_for_netrc_lookup(host: str) -> str:
    """Normalize a host string for .netrc lookup.

    Strips scheme (http://, https://), path components, and port numbers
    to produce a clean hostname for credential lookup.

    Args:
        host: Raw host string, may include scheme, port, or path.

    Returns:
        Normalized hostname in lowercase.

    Examples:
        >>> _normalize_host_for_netrc_lookup("https://gerrit.example.org/r")
        'gerrit.example.org'
        >>> _normalize_host_for_netrc_lookup("gerrit.example.org:8080")
        'gerrit.example.org'
        >>> _normalize_host_for_netrc_lookup("GERRIT.EXAMPLE.ORG")
        'gerrit.example.org'
    """
    normalized = host.lower().strip()
    # Remove scheme (http://, https://, etc.)
    if "://" in normalized:
        normalized = normalized.split("://", 1)[1]
    if "/" in normalized:
        normalized = normalized.split("/", 1)[0]
    if ":" in normalized:
        normalized = normalized.rsplit(":", 1)[0]
    return normalized


def load_netrc(
    path: Path | None = None,
    search_local: bool = True,
) -> NetrcParser | None:
    """
    Load and parse a netrc file.

    Args:
        path: Explicit path to netrc file (optional).
        search_local: Search current directory for .netrc.

    Returns:
        NetrcParser instance, or None if no file found.

    Raises:
        NetrcParseError: If the file exists but cannot be parsed.
    """
    netrc_path = find_netrc_file(
        search_local=search_local,
        explicit_path=path,
    )

    if netrc_path is None:
        return None

    check_netrc_permissions(netrc_path)

    try:
        content = netrc_path.read_text(encoding="utf-8")
    except OSError:
        log.exception("Could not read netrc file %s", netrc_path)
        return None

    try:
        return NetrcParser(content)
    except NetrcParseError:
        log.exception("Could not parse netrc file %s", netrc_path)
        raise


def get_credentials_for_host(
    host: str,
    netrc_file: Path | None = None,
    search_local: bool = True,
    use_netrc: bool = True,
    netrc_optional: bool = True,
) -> NetrcCredentials | None:
    """
    Get credentials for a Gerrit host from .netrc file only.

    This is a lower-level function for direct .netrc file lookup.
    For full credential resolution with priority order (CLI args,
    .netrc, environment variables), use `resolve_gerrit_credentials`
    instead.

    Args:
        host: Gerrit server hostname (e.g., 'gerrit.onap.org').
        netrc_file: Explicit path to netrc file (optional).
        search_local: Search current directory for .netrc.
        use_netrc: Whether to use netrc at all (--no-netrc sets False).
        netrc_optional: If True, don't fail if netrc not found.

    Returns:
        NetrcCredentials if found, None otherwise.

    Raises:
        NetrcParseError: If netrc file exists but cannot be parsed.
        FileNotFoundError: If netrc_optional=False and no file found.

    See Also:
        resolve_gerrit_credentials: The recommended function for full
            credential resolution with CLI, .netrc, and env var support.
    """
    if not use_netrc:
        log.debug("Netrc lookup disabled")
        return None

    # Normalize host - remove scheme, path, and port if present
    normalized_host = _normalize_host_for_netrc_lookup(host)

    # Find the netrc file path first so we can include it in log messages
    netrc_path = find_netrc_file(
        search_local=search_local,
        explicit_path=netrc_file,
    )

    if netrc_path is None:
        if not netrc_optional:
            msg = "No .netrc file found and netrc is required"
            raise FileNotFoundError(msg)
        return None

    netrc = load_netrc(
        path=netrc_path,
        search_local=False,  # Already found the path
    )

    if netrc is None:
        # load_netrc returns None if file couldn't be read
        return None

    credentials = netrc.get_credentials(normalized_host)
    if credentials:
        log.debug(
            "Found netrc credentials for %s (login: %s) in %s",
            normalized_host,
            credentials.login,
            netrc_path,
        )
    else:
        log.warning(
            "No netrc credentials found for %s in %s",
            normalized_host,
            netrc_path,
        )

    return credentials


def resolve_gerrit_credentials(
    host: str,
    *,
    explicit_username: str | None = None,
    explicit_password: str | None = None,
    use_netrc: bool = True,
    netrc_file: Path | None = None,
    env_username_var: str = "GERRIT_HTTP_USER",
    env_password_var: str = "GERRIT_HTTP_PASSWORD",
    fallback_env_username_var: str | None = None,
    fallback_env_password_var: str | None = None,
) -> GerritCredentials | None:
    """
    Resolve Gerrit credentials from multiple sources with defined priority.

    This is the **canonical function** for resolving Gerrit credentials
    and should be used by CLI commands and other high-level code.
    It returns a GerritCredentials object that contains both the
    credentials and metadata about their source.

    Priority order:
        1. Explicit CLI arguments (--http-user/--http-password)
        2. .netrc file (if use_netrc=True)
        3. Primary environment variables (GERRIT_HTTP_USER/GERRIT_HTTP_PASSWORD)
        4. Fallback environment variables (GERRIT_USERNAME/GERRIT_PASSWORD)

    This function is used by:
        - source-clone clone command
        - source-clone mirror command
        - GerritAPIClient for HTTP Basic Auth

    Args:
        host: Gerrit server hostname for netrc lookup.
        explicit_username: Username from CLI argument (highest priority).
        explicit_password: Password from CLI argument (highest priority).
        use_netrc: Whether to try .netrc for credentials.
        netrc_file: Explicit path to a .netrc file.
        env_username_var: Primary environment variable for username.
        env_password_var: Primary environment variable for password.
        fallback_env_username_var: Fallback environment variable for username.
        fallback_env_password_var: Fallback environment variable for password.

    Returns:
        GerritCredentials with resolved credentials and source info,
        or None if no credentials found.

    Example:
        >>> creds = resolve_gerrit_credentials(
        ...     host="gerrit.example.org",
        ...     explicit_username="cli_user",
        ...     explicit_password="cli_pass",
        ... )
        >>> if creds:
        ...     print(f"Using {creds.source.value} credentials")

    See Also:
        get_credentials_for_host: Lower-level function for .netrc-only lookup.
    """
    # 1. Check explicit CLI arguments first (highest priority)
    if explicit_username and explicit_password:
        log.debug("Using credentials from CLI arguments")
        return GerritCredentials(
            username=explicit_username.strip(),
            password=explicit_password.strip(),
            source=CredentialSource.CLI_ARGUMENT,
            source_detail="--http-user/--http-password",
        )

    # 2. Try .netrc file using the lower-level function
    if use_netrc:
        # Find the netrc file path for source tracking
        netrc_path = find_netrc_file(
            search_local=True,
            explicit_path=netrc_file,
        )

        if netrc_path is not None:
            # Use get_credentials_for_host for the actual lookup
            netrc_creds = get_credentials_for_host(
                host=host,
                netrc_file=netrc_path,
                search_local=False,  # Already found the path
                use_netrc=True,
                netrc_optional=True,  # Don't raise, we handle missing creds
            )

            if netrc_creds:
                log.debug(
                    "Using credentials from .netrc for %s (login: %s) in %s",
                    host,
                    netrc_creds.login,
                    netrc_path,
                )
                return GerritCredentials(
                    username=netrc_creds.login,
                    password=netrc_creds.password,
                    source=CredentialSource.NETRC,
                    source_detail=str(netrc_path),
                )

    # 3. Try primary environment variables
    env_user = os.getenv(env_username_var, "").strip()
    env_pass = os.getenv(env_password_var, "").strip()

    if env_user and env_pass:
        # Log only the username variable name; naming the password
        # variable risks leaking sensitive identifiers into logs.
        log.debug(
            "Using credentials from environment variables (username var: %s)",
            env_username_var,
        )
        return GerritCredentials(
            username=env_user,
            password=env_pass,
            source=CredentialSource.ENVIRONMENT,
            source_detail=f"{env_username_var}/{env_password_var}",
        )

    # 4. Try fallback environment variables
    if fallback_env_username_var and fallback_env_password_var:
        fallback_user = os.getenv(fallback_env_username_var, "").strip()
        fallback_pass = os.getenv(fallback_env_password_var, "").strip()

        if fallback_user and fallback_pass:
            # Log only the username variable name; naming the password
            # variable risks leaking sensitive identifiers into logs.
            log.debug(
                "Using credentials from fallback environment variables "
                "(username var: %s)",
                fallback_env_username_var,
            )
            return GerritCredentials(
                username=fallback_user,
                password=fallback_pass,
                source=CredentialSource.ENVIRONMENT,
                source_detail=f"{fallback_env_username_var}/{fallback_env_password_var}",
            )

    log.debug("No Gerrit credentials found from any source")
    return None


__all__ = [
    "CredentialSource",
    "GerritCredentials",
    "NetrcCredentials",
    "NetrcParseError",
    "NetrcParser",
    "check_netrc_permissions",
    "find_netrc_file",
    "get_credentials_for_host",
    "load_netrc",
    "resolve_gerrit_credentials",
]
