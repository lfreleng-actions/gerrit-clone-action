# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2025 Matthew Watkins <mwatkins@linuxfoundation.org>

"""Standardized error codes and error handling for gerrit-clone-action.

This module provides consistent exit codes and error handling patterns
similar to github2gerrit-action for better automation and debugging.
"""

# mypy: disable-error-code="import-untyped"

from __future__ import annotations

import sys
import urllib.error
from enum import IntEnum
from typing import NoReturn

from rich.console import Console

from gerrit_clone.logging import get_logger

logger = get_logger(__name__)


class ExitCode(IntEnum):
    """Standard exit codes for gerrit-clone operations."""

    SUCCESS = 0
    """Operation completed successfully."""

    GENERAL_ERROR = 1
    """General operational failure."""

    CONFIGURATION_ERROR = 2
    """Configuration validation failed or missing required parameters."""

    DISCOVERY_ERROR = 3
    """Project discovery failed for all configured methods."""

    GERRIT_CONNECTION_ERROR = 4
    """Failed to connect to or authenticate with Gerrit server."""

    NETWORK_ERROR = 5
    """Network connectivity issues."""

    REPOSITORY_ERROR = 6
    """Git repository access or operation failed."""

    CLONE_ERROR = 7
    """Clone operations failed."""

    VALIDATION_ERROR = 8
    """Input validation failed."""

    FILESYSTEM_ERROR = 9
    """Filesystem access or permission issues."""

    INTERRUPT = 130
    """Operation interrupted by user (SIGINT)."""


# Error message templates
ERROR_MESSAGES = {
    ExitCode.DISCOVERY_ERROR: (
        "❌ Project discovery failed; unable to fetch project list from Gerrit"
    ),
    ExitCode.GERRIT_CONNECTION_ERROR: (
        "❌ Gerrit connection failed; check SSH keys and server configuration"
    ),
    ExitCode.CONFIGURATION_ERROR: (
        "❌ Configuration validation failed; check required parameters"
    ),
    ExitCode.NETWORK_ERROR: (
        "❌ Network connectivity failed; check internet connection"
    ),
    ExitCode.REPOSITORY_ERROR: (
        "❌ Git repository access failed; check repository permissions"
    ),
    ExitCode.CLONE_ERROR: ("❌ Clone operations failed; check logs for details"),
    ExitCode.VALIDATION_ERROR: ("❌ Input validation failed; check parameter values"),
    ExitCode.FILESYSTEM_ERROR: (
        "❌ Filesystem access failed; check permissions and disk space"
    ),
    ExitCode.INTERRUPT: "❌ Operation interrupted by user",
    ExitCode.GENERAL_ERROR: "❌ Operation failed; check logs for details",
}


class GerritCloneError(Exception):
    """Base exception class for gerrit-clone errors with exit codes."""

    def __init__(
        self,
        exit_code: ExitCode,
        message: str | None = None,
        details: str | None = None,
        original_exception: Exception | None = None,
    ):
        self.exit_code = exit_code
        self.message = message or ERROR_MESSAGES.get(
            exit_code, ERROR_MESSAGES[ExitCode.GENERAL_ERROR]
        )
        self.details = details
        self.original_exception = original_exception

        # Call parent constructor with the error message
        super().__init__(self.message)

    def display_and_exit(self) -> NoReturn:
        """Display the error message and exit with the appropriate code."""
        console = Console(stderr=True)

        # Log the error with details
        if self.original_exception:
            logger.error(
                "Exit code %d: %s (Exception: %s)",
                self.exit_code,
                self.message,
                self.original_exception,
            )
            if self.details:
                logger.error("Additional details: %s", self.details)
        else:
            logger.error("Exit code %d: %s", self.exit_code, self.message)
            if self.details:
                logger.error("Details: %s", self.details)

        # Display user-friendly error message
        console.print(self.message, style="red")

        if self.details:
            console.print(f"Details: {self.details}", style="dim red")

        sys.exit(int(self.exit_code))


class DiscoveryError(GerritCloneError):
    """Exception for project discovery failures."""

    def __init__(
        self,
        message: str | None = None,
        details: str | None = None,
        original_exception: Exception | None = None,
    ):
        super().__init__(ExitCode.DISCOVERY_ERROR, message, details, original_exception)


class ConfigurationError(GerritCloneError):
    """Exception for configuration validation failures."""

    def __init__(
        self,
        message: str | None = None,
        details: str | None = None,
        original_exception: Exception | None = None,
    ):
        super().__init__(
            ExitCode.CONFIGURATION_ERROR, message, details, original_exception
        )


class GerritConnectionError(GerritCloneError):
    """Exception for Gerrit connection failures."""

    def __init__(
        self,
        message: str | None = None,
        details: str | None = None,
        original_exception: Exception | None = None,
    ):
        super().__init__(
            ExitCode.GERRIT_CONNECTION_ERROR, message, details, original_exception
        )


class NetworkError(GerritCloneError):
    """Exception for network connectivity failures."""

    def __init__(
        self,
        message: str | None = None,
        details: str | None = None,
        original_exception: Exception | None = None,
    ):
        super().__init__(ExitCode.NETWORK_ERROR, message, details, original_exception)


def exit_with_error(
    exit_code: ExitCode,
    message: str | None = None,
    details: str | None = None,
    exception: Exception | None = None,
) -> NoReturn:
    """Exit with standardized error message and code.

    Args:
        exit_code: Standard exit code from ExitCode enum
        message: Override default error message (optional)
        details: Additional error details (optional)
        exception: Original exception for logging (optional)
    """
    error = GerritCloneError(exit_code, message, details, exception)
    error.display_and_exit()


def exit_for_discovery_error(
    message: str | None = None,
    details: str | None = None,
    exception: Exception | None = None,
) -> NoReturn:
    """Exit with discovery error code.

    Args:
        message: Override default error message (optional)
        details: Additional error details (optional)
        exception: Original exception for logging (optional)
    """
    error = DiscoveryError(message, details, exception)
    error.display_and_exit()


def exit_for_gerrit_connection_error(
    message: str | None = None,
    details: str | None = None,
    exception: Exception | None = None,
) -> NoReturn:
    """Exit with Gerrit connection error code.

    Args:
        message: Override default error message (optional)
        details: Additional error details (optional)
        exception: Original exception for logging (optional)
    """
    error = GerritConnectionError(message, details, exception)
    error.display_and_exit()


def exit_for_configuration_error(
    message: str | None = None,
    details: str | None = None,
    exception: Exception | None = None,
) -> NoReturn:
    """Exit with configuration error code.

    Args:
        message: Override default error message (optional)
        details: Additional error details (optional)
        exception: Original exception for logging (optional)
    """
    error = ConfigurationError(message, details, exception)
    error.display_and_exit()


def is_network_error(exception: Exception) -> bool:
    """Determine if an exception represents a network error.

    Args:
        exception: Exception to check

    Returns:
        True if exception indicates network connectivity issues
    """
    if isinstance(exception, urllib.error.URLError):
        return True

    # Fall back to matching known network-error phrases in the message.
    error_str = str(exception).lower()
    network_indicators = [
        "connection refused",
        "connection timeout",
        "network is unreachable",
        "host is unreachable",
        "name resolution failed",
        "dns resolution failed",
        "connection reset",
        "connection aborted",
    ]

    return any(indicator in error_str for indicator in network_indicators)


def is_gerrit_connection_error(exception: Exception) -> bool:
    """Determine if an exception represents a Gerrit connection error.

    Args:
        exception: Exception to check

    Returns:
        True if exception indicates Gerrit server connection issues
    """
    error_str = str(exception).lower()
    gerrit_indicators = [
        "ssh",
        "authentication failed",
        "permission denied",
        "public key",
        "private key",
        "host key verification failed",
        "gerrit",
        "port 29418",  # Common Gerrit SSH port
    ]

    return any(indicator in error_str for indicator in gerrit_indicators)
