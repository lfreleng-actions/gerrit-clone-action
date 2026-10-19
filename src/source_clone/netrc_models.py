# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""
Credential data structures for Gerrit authentication.

Defines the canonical credential types shared by the netrc parsing and
credential resolution code, along with the parse error raised when a
.netrc file cannot be read.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class NetrcParseError(Exception):
    """Raised when a .netrc file cannot be parsed."""


class CredentialSource(Enum):
    """Enum indicating the source of resolved credentials."""

    NETRC = "netrc"
    ENVIRONMENT = "environment"
    CLI_ARGUMENT = "cli_argument"
    NONE = "none"


@dataclass(frozen=True)
class GerritCredentials:
    """Resolved Gerrit credentials with source metadata.

    This is the canonical data structure for Gerrit authentication
    credentials. All credential resolution should produce this type,
    and all consumers should accept this type.
    """

    username: str
    password: str
    source: CredentialSource
    source_detail: str  # e.g., "/path/to/.netrc" or "GERRIT_HTTP_USER"

    def __repr__(self) -> str:
        """Mask password in repr for security."""
        return (
            f"GerritCredentials(username={self.username!r}, "
            f"password='****', source={self.source.value!r}, "
            f"source_detail={self.source_detail!r})"
        )

    @property
    def is_valid(self) -> bool:
        """Return True if credentials are present and non-empty."""
        return bool(self.username and self.password)

    def auth_method_display(self) -> str:
        """Return a human-readable description of the auth method for display."""
        if self.source == CredentialSource.NETRC:
            return f".netrc file ({self.source_detail})"
        elif self.source == CredentialSource.ENVIRONMENT:
            return f"Environment variables ({self.source_detail})"
        elif self.source == CredentialSource.CLI_ARGUMENT:
            return "CLI arguments"
        else:
            return "None"


@dataclass(frozen=True)
class NetrcCredentials:
    """Credentials retrieved from a .netrc file entry."""

    machine: str
    login: str
    password: str

    def __repr__(self) -> str:
        """Mask password in repr for security."""
        return (
            f"NetrcCredentials(machine={self.machine!r}, "
            f"login={self.login!r}, password='****')"
        )


__all__ = [
    "CredentialSource",
    "GerritCredentials",
    "NetrcCredentials",
    "NetrcParseError",
]
