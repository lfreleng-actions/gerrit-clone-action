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
import re
import stat
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

log = logging.getLogger(__name__)

# Token constants for netrc parsing
_TOKEN_MACHINE = "machine"
_TOKEN_LOGIN = "login"
_TOKEN_PASSWORD = "password"
_TOKEN_DEFAULT = "default"
_TOKEN_MACDEF = "macdef"


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
    # Remove path components
    if "/" in normalized:
        normalized = normalized.split("/", 1)[0]
    # Remove port number
    if ":" in normalized:
        normalized = normalized.rsplit(":", 1)[0]
    return normalized


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


class NetrcParser:
    """
    Parser for .netrc files.

    Supports the standard netrc format with machine, login, password,
    and default tokens. Also supports quoted strings with escape
    sequences as introduced in curl 7.84.0.
    """

    # Regex for quoted strings with escape sequences
    _QUOTED_STRING_PATTERN = re.compile(r'"(?:[^"\\]|\\.)*"')

    def __init__(self, content: str) -> None:
        """
        Initialize parser with file content.

        Args:
            content: The raw content of a .netrc file.
        """
        self._content = content
        self._entries: dict[str, NetrcCredentials] = {}
        self._default: NetrcCredentials | None = None
        self._parse()

    def _unescape_quoted_string(self, s: str) -> str:
        """
        Unescape a quoted string from netrc format.

        Handles escape sequences: \\", \\n, \\r, \\t

        Args:
            s: Quoted string including surrounding quotes.

        Returns:
            Unescaped string content without quotes.
        """
        # Remove surrounding quotes
        inner = s[1:-1]
        # Process escape sequences
        result: list[str] = []
        i = 0
        while i < len(inner):
            if inner[i] == "\\" and i + 1 < len(inner):
                next_char = inner[i + 1]
                if next_char == '"':
                    result.append('"')
                elif next_char == "n":
                    result.append("\n")
                elif next_char == "r":
                    result.append("\r")
                elif next_char == "t":
                    result.append("\t")
                elif next_char == "\\":
                    result.append("\\")
                else:
                    # Unknown escape, keep as-is
                    result.append(inner[i : i + 2])
                i += 2
            else:
                result.append(inner[i])
                i += 1
        return "".join(result)

    def _strip_inline_comment(self, text: str) -> str:
        """Strip inline comment from a line, respecting quotes."""
        if "#" not in text:
            return text
        in_quotes = False
        for i, char in enumerate(text):
            if char == '"' and (i == 0 or text[i - 1] != "\\"):
                in_quotes = not in_quotes
            elif char == "#" and not in_quotes:
                return text[:i]
        return text

    def _tokenize(self, content: str) -> list[str]:
        """
        Tokenize netrc content, handling quoted strings.

        Preserves newline tokens ("\n") to support proper macdef parsing.
        Per netrc spec, macdef sections end at a blank line (two consecutive
        newlines), so we need to preserve newline information.

        Args:
            content: Raw netrc file content.

        Returns:
            List of tokens, including "\n" tokens for line boundaries.
        """
        tokens: list[str] = []
        # Process line by line to preserve newline information
        lines: list[str] = []
        for raw_line in content.splitlines():
            # Strip leading whitespace to check for comment
            stripped = raw_line.lstrip()
            if stripped.startswith("#"):
                # Preserve blank line marker for macdef parsing
                lines.append("")
                continue
            # Handle inline comments
            processed_line = self._strip_inline_comment(raw_line)
            lines.append(processed_line)

        # Find all quoted strings and replace with placeholders
        placeholders: dict[str, str] = {}
        placeholder_idx = 0

        def replace_quoted(match: re.Match[str]) -> str:
            nonlocal placeholder_idx
            placeholder = f"\x00QUOTED{placeholder_idx}\x00"
            placeholders[placeholder] = match.group(0)
            placeholder_idx += 1
            return placeholder

        # Process each line, preserving newline tokens
        for line in lines:
            # Replace quoted strings with placeholders
            processed_line = self._QUOTED_STRING_PATTERN.sub(
                replace_quoted, line
            )

            # Split on whitespace
            raw_tokens = processed_line.split()

            # Restore quoted strings and unescape
            for raw_token in raw_tokens:
                if raw_token in placeholders:
                    tokens.append(
                        self._unescape_quoted_string(placeholders[raw_token])
                    )
                elif "\x00QUOTED" in raw_token:
                    # Handle case where placeholder is part of larger token
                    processed_token = raw_token
                    for placeholder, quoted in placeholders.items():
                        if placeholder in processed_token:
                            processed_token = processed_token.replace(
                                placeholder, self._unescape_quoted_string(quoted)
                            )
                    tokens.append(processed_token)
                else:
                    tokens.append(raw_token)

            # Add newline token to mark end of line
            tokens.append("\n")

        return tokens

    def _parse_machine_entry(
        self, tokens: list[str], start_idx: int
    ) -> tuple[int, NetrcCredentials | None]:
        """Parse a machine entry starting at start_idx."""
        # Skip any newlines after 'machine' keyword
        i = start_idx + 1
        while i < len(tokens) and tokens[i] == "\n":
            i += 1
        if i >= len(tokens):
            msg = "Expected machine name after 'machine'"
            raise NetrcParseError(msg)

        machine = tokens[i]
        i += 1
        login: str | None = None
        password: str | None = None

        while i < len(tokens):
            token = tokens[i]
            # Skip newline tokens in normal parsing
            if token == "\n":
                i += 1
                continue
            next_token = token.lower()
            if next_token == _TOKEN_LOGIN:
                if i + 1 >= len(tokens):
                    msg = "Expected login value after 'login'"
                    raise NetrcParseError(msg)
                # Skip any newlines before the value
                i += 1
                while i < len(tokens) and tokens[i] == "\n":
                    i += 1
                if i >= len(tokens):
                    msg = "Expected login value after 'login'"
                    raise NetrcParseError(msg)
                login = tokens[i]
                i += 1
            elif next_token == _TOKEN_PASSWORD:
                if i + 1 >= len(tokens):
                    msg = "Expected password value after 'password'"
                    raise NetrcParseError(msg)
                # Skip any newlines before the value
                i += 1
                while i < len(tokens) and tokens[i] == "\n":
                    i += 1
                if i >= len(tokens):
                    msg = "Expected password value after 'password'"
                    raise NetrcParseError(msg)
                password = tokens[i]
                i += 1
            elif next_token in (_TOKEN_MACHINE, _TOKEN_DEFAULT):
                break
            elif next_token == _TOKEN_MACDEF:
                # Skip over the 'macdef' token itself
                i += 1
                # Skip over the macro name, if present
                if i < len(tokens) and tokens[i] != "\n":
                    i += 1
                # Per netrc spec, the macro body continues until a blank line.
                # A blank line is detected as two consecutive newline tokens.
                consecutive_newlines = 0
                while i < len(tokens):
                    token = tokens[i]
                    if token == "\n":
                        consecutive_newlines += 1
                        if consecutive_newlines >= 2:
                            # Found blank line - end of macdef
                            i += 1
                            break
                    else:
                        # Any non-newline token resets the blank-line check
                        consecutive_newlines = 0
                    i += 1
            else:
                i += 1

        creds = None
        if login and password:
            creds = NetrcCredentials(
                machine=machine,
                login=login,
                password=password,
            )
        return i, creds

    def _parse_default_entry(
        self, tokens: list[str], start_idx: int
    ) -> tuple[int, NetrcCredentials | None]:
        """Parse a default entry starting at start_idx."""
        i = start_idx + 1
        login: str | None = None
        password: str | None = None

        while i < len(tokens):
            token = tokens[i]
            # Skip newline tokens in normal parsing
            if token == "\n":
                i += 1
                continue
            next_token = token.lower()
            if next_token == _TOKEN_LOGIN:
                if i + 1 >= len(tokens):
                    msg = "Expected login value after 'login'"
                    raise NetrcParseError(msg)
                # Skip any newlines before the value
                i += 1
                while i < len(tokens) and tokens[i] == "\n":
                    i += 1
                if i >= len(tokens):
                    msg = "Expected login value after 'login'"
                    raise NetrcParseError(msg)
                login = tokens[i]
                i += 1
            elif next_token == _TOKEN_PASSWORD:
                if i + 1 >= len(tokens):
                    msg = "Expected password value after 'password'"
                    raise NetrcParseError(msg)
                # Skip any newlines before the value
                i += 1
                while i < len(tokens) and tokens[i] == "\n":
                    i += 1
                if i >= len(tokens):
                    msg = "Expected password value after 'password'"
                    raise NetrcParseError(msg)
                password = tokens[i]
                i += 1
            elif next_token in (_TOKEN_MACHINE, _TOKEN_DEFAULT):
                break
            else:
                i += 1

        creds = None
        if login and password:
            creds = NetrcCredentials(
                machine=_TOKEN_DEFAULT,
                login=login,
                password=password,
            )
        return i, creds

    def _parse(self) -> None:
        """Parse the netrc content into entries."""
        tokens = self._tokenize(self._content)

        i = 0
        while i < len(tokens):
            token = tokens[i]
            # Skip newline tokens at top level
            if token == "\n":
                i += 1
                continue
            current_token = token.lower()

            if current_token == _TOKEN_MACHINE:
                i, creds = self._parse_machine_entry(tokens, i)
                if creds:
                    self._entries[creds.machine.lower()] = creds
            elif current_token == _TOKEN_DEFAULT:
                i, creds = self._parse_default_entry(tokens, i)
                if creds:
                    self._default = creds
            else:
                i += 1

    def get_credentials(self, machine: str) -> NetrcCredentials | None:
        """
        Get credentials for a specific machine.

        Args:
            machine: The hostname to look up credentials for.

        Returns:
            NetrcCredentials if found, None otherwise.
            Falls back to default entry if no specific match.
        """
        # Normalize machine name (case-insensitive lookup)
        normalized = machine.lower().strip()

        # Try exact match first
        if normalized in self._entries:
            return self._entries[normalized]

        # Fall back to default
        return self._default

    @property
    def machines(self) -> list[str]:
        """Return list of all machine names with entries."""
        return list(self._entries.keys())

    @property
    def has_default(self) -> bool:
        """Return True if a default entry exists."""
        return self._default is not None


def find_netrc_file(
    search_local: bool = True,
    explicit_path: Path | None = None,
) -> Path | None:
    """
    Find a .netrc file using standard search order.

    Search order:
    1. Explicit path (if provided)
    2. Local directory .netrc (if search_local=True)
    3. ~/.netrc
    4. ~/_netrc (Windows fallback)

    Args:
        search_local: Whether to search current directory first.
        explicit_path: Explicit path to a netrc file.

    Returns:
        Path to found netrc file, or None if not found.
    """
    if explicit_path is not None:
        if explicit_path.is_file():
            log.debug("Using explicit netrc file: %s", explicit_path)
            return explicit_path
        log.warning("Explicit netrc file not found: %s", explicit_path)
        return None

    candidates: list[Path] = []

    # Local directory
    if search_local:
        candidates.append(Path.cwd() / ".netrc")

    # Home directory
    home = Path.home()
    candidates.append(home / ".netrc")

    # Windows fallback
    if os.name == "nt":
        candidates.append(home / "_netrc")

    for candidate in candidates:
        if candidate.is_file():
            log.debug("Found netrc file: %s", candidate)
            return candidate

    log.debug("No netrc file found in search paths")
    return None


def check_netrc_permissions(path: Path) -> bool:
    """
    Check if netrc file has secure permissions.

    Warns if the file is readable by others (Unix only).

    Args:
        path: Path to the netrc file.

    Returns:
        True if permissions are secure, False otherwise.
    """
    if os.name == "nt":
        # Windows doesn't have the same permission model
        return True

    try:
        mode = path.stat().st_mode
    except OSError as e:
        log.warning("Could not check permissions for %s: %s", path, e)
        return True

    # Check if group or others have read permission
    if mode & (stat.S_IRGRP | stat.S_IROTH):
        log.warning(
            "Netrc file %s has insecure permissions. "
            "Consider running: chmod 600 %s",
            path,
            path,
        )
        return False
    return True


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
        - gerrit-clone clone command
        - gerrit-clone mirror command
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
        log.debug(
            "Using credentials from environment variables %s/%s",
            env_username_var,
            env_password_var,
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
            log.debug(
                "Using credentials from fallback environment variables %s/%s",
                fallback_env_username_var,
                fallback_env_password_var,
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
