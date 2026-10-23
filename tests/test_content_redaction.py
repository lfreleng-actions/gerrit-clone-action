# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""Tests for the ``git filter-repo --replace-text`` mapping file.

The mapping file drives a history rewrite, so a token must never be
able to change how its own line is parsed.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from source_clone.content_redaction import (
    _generate_replacement_string,
    _write_replacements_file,
)


def _lines_for(tokens: list[str]) -> list[str]:
    """Write *tokens* to a mapping file and return its lines."""
    path, count = _write_replacements_file(tokens)
    try:
        content = Path(path).read_text(encoding="utf-8")
    finally:
        Path(path).unlink(missing_ok=True)
    lines = content.splitlines()
    assert count == len(lines)
    return lines


class TestReplacementsFile:
    """Every accepted token is written as an explicit literal."""

    def test_plain_token_is_written_as_a_literal(self) -> None:
        token = "ghp_averyrealsecret"

        assert _lines_for([token]) == [
            f"literal:{token}==>{_generate_replacement_string(token)}"
        ]

    @pytest.mark.parametrize("prefix", ["regex:", "glob:", "literal:"])
    def test_selector_prefixes_in_a_token_are_not_interpreted(
        self, prefix: str
    ) -> None:
        """A token may not smuggle a git filter-repo selector.

        Without the explicit ``literal:``, a token of ``regex:.*``
        would be parsed as the regex ``.*`` and rewrite the entire
        repository rather than redacting that one string.
        """
        token = f"{prefix}.*"

        lines = _lines_for([token])

        assert lines == [f"literal:{token}==>{_generate_replacement_string(token)}"]
        assert lines[0].startswith("literal:")

    def test_tokens_breaking_the_format_are_skipped(self) -> None:
        """Newlines, NULs and the delimiter cannot reach the file."""
        good = "keepme"
        tokens = ["bad\nvalue", "bad\rvalue", "bad\0value", "bad==>value", good]

        lines = _lines_for(tokens)

        assert lines == [f"literal:{good}==>{_generate_replacement_string(good)}"]

    def test_replacement_does_not_leak_the_token(self) -> None:
        token = "ghp_averyrealsecret"

        assert token not in _generate_replacement_string(token)

    def test_no_tokens_yields_an_empty_mapping(self) -> None:
        assert _lines_for([]) == []
