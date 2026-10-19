# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""
Tokenizer and parser for .netrc file content.

Implements the standard netrc grammar (machine/login/password/default)
including quoted strings with escape sequences as introduced in
curl 7.84.0, and macdef block skipping.
"""

from __future__ import annotations

import re
from typing import ClassVar

from source_clone.netrc_models import NetrcCredentials, NetrcParseError

# Token constants for netrc parsing
_TOKEN_MACHINE = "machine"
_TOKEN_LOGIN = "login"
_TOKEN_PASSWORD = "password"
_TOKEN_DEFAULT = "default"
_TOKEN_MACDEF = "macdef"


class NetrcParser:
    """
    Parser for .netrc files.

    Supports the standard netrc format with machine, login, password,
    and default tokens. Also supports quoted strings with escape
    sequences as introduced in curl 7.84.0.
    """

    # Regex for quoted strings with escape sequences
    _QUOTED_STRING_PATTERN = re.compile(r'"(?:[^"\\]|\\.)*"')

    # Recognised backslash escape sequences within quoted netrc values.
    _ESCAPE_SEQUENCES: ClassVar[dict[str, str]] = {
        '"': '"',
        "n": "\n",
        "r": "\r",
        "t": "\t",
        "\\": "\\",
    }

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
        inner = s[1:-1]
        result: list[str] = []
        i = 0
        while i < len(inner):
            if inner[i] == "\\" and i + 1 < len(inner):
                next_char = inner[i + 1]
                mapped = self._ESCAPE_SEQUENCES.get(next_char)
                if mapped is not None:
                    result.append(mapped)
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
        lines: list[str] = []
        for raw_line in content.splitlines():
            # Strip leading whitespace to check for comment
            stripped = raw_line.lstrip()
            if stripped.startswith("#"):
                # Preserve blank line marker for macdef parsing
                lines.append("")
                continue
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

        for line in lines:
            # Replace quoted strings with placeholders
            processed_line = self._QUOTED_STRING_PATTERN.sub(replace_quoted, line)

            # Split on whitespace
            raw_tokens = processed_line.split()

            # Restore quoted strings and unescape
            for raw_token in raw_tokens:
                if raw_token in placeholders:
                    tokens.append(self._unescape_quoted_string(placeholders[raw_token]))
                elif "\x00QUOTED" in raw_token:
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


__all__ = [
    "NetrcParser",
]
