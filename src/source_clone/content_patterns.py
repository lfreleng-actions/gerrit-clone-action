# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""Pattern primitives shared by the content filtering modules.

Holds the pure, side-effect free pieces of pattern handling:
translating path-segment-aware globs into regex fragments, matching
``regex:``-prefixed patterns, normalizing user-supplied pattern lists
and parsing the ``--git-filter`` specification string.
"""

from __future__ import annotations

import hashlib
import re

from source_clone.logging import get_logger

logger = get_logger(__name__)


def _glob_to_regex(pat: str) -> str:
    """Translate a path-segment-aware glob into a regex fragment.

    The returned fragment is **not** anchored; callers anchor it by
    matching with :func:`re.fullmatch` (which requires the pattern to
    cover the whole string).

    Unlike :func:`fnmatch.translate`, ``*`` and ``?`` do **not** match
    across directory separators.  Recursive matching requires the
    explicit ``**`` token.

    Semantics:
    - ``*``    matches any run of characters except ``/``
    - ``?``    matches a single character except ``/``
    - ``**``   matches any run of characters including ``/``
    - ``**/``  optionally matches a leading directory prefix
    - ``[seq]`` matches one character in the set (``!`` negates)

    Args:
        pat: Glob pattern with ``/`` separators.

    Returns:
        A regex string (not anchored) suitable for :func:`re.fullmatch`.
    """
    i = 0
    n = len(pat)
    out: list[str] = []
    while i < n:
        c = pat[i]
        if c == "*":
            if i + 1 < n and pat[i + 1] == "*":
                i += 2
                if i < n and pat[i] == "/":
                    # ``**/`` matches zero or more leading segments
                    out.append("(?:.*/)?")
                    i += 1
                else:
                    out.append(".*")
            else:
                out.append("[^/]*")
                i += 1
        elif c == "?":
            out.append("[^/]")
            i += 1
        elif c == "[":
            j = i + 1
            if j < n and pat[j] in ("!", "^"):
                j += 1
            if j < n and pat[j] == "]":
                j += 1
            while j < n and pat[j] != "]":
                j += 1
            if j >= n:
                # No closing bracket: treat '[' as a literal.
                out.append(re.escape(c))
                i += 1
            else:
                inner = pat[i + 1 : j]
                if inner.startswith("!"):
                    inner = "^" + inner[1:]
                out.append("[" + inner + "]")
                i = j + 1
        else:
            out.append(re.escape(c))
            i += 1
    return "".join(out)


def _match_regex_pattern(regex: str, normalized: str) -> bool:
    """Match *normalized* against a bare ``regex:`` pattern body.

    Args:
        regex: Regex source with the ``regex:`` prefix already stripped.
        normalized: Forward-slash normalized path.

    Returns:
        ``True`` on a match; ``False`` for empty or invalid patterns.
    """
    if not regex:
        # An empty regex (bare ``regex:``) would match every
        # path via ``re.search("", ...)``, which could silently
        # remove all files.  Reject it explicitly.
        logger.warning("Empty regex pattern (bare 'regex:') ignored")
        return False
    try:
        return bool(re.search(regex, normalized))
    except re.error as exc:
        logger.warning("Invalid regex pattern %r: %s", regex, exc)
        return False


def normalize_file_patterns(raw: list[str]) -> list[str]:
    """Normalize a list of file path patterns.

    Strips whitespace, splits on commas, drops empties,
    de-duplicates while preserving insertion order.

    Args:
        raw: List of raw pattern strings (may contain commas).

    Returns:
        Normalized, de-duplicated list of patterns.
    """
    seen: set[str] = set()
    normalized: list[str] = []
    for entry in raw:
        for comma_part in entry.split(","):
            clean = comma_part.strip()
            if clean and clean not in seen:
                normalized.append(clean)
                seen.add(clean)
    return normalized


def parse_git_filter_spec(raw: str) -> dict[str, list[str]]:
    """Parse a git filter specification string.

    The format is: ``project_pattern:token1,token2;project2:token3``

    Semicolons separate project entries.  Within each entry, a colon
    separates the project name pattern from comma-separated tokens.

    Alternatively, a simpler format for a single project:
    ``project_pattern:token1``

    Args:
        raw: Raw specification string.

    Returns:
        Dictionary mapping project patterns to lists of tokens.
    """
    result: dict[str, list[str]] = {}
    if not raw or not raw.strip():
        return result

    for raw_entry in raw.split(";"):
        entry = raw_entry.strip()
        if not entry:
            continue
        if ":" not in entry:
            entry_fp = hashlib.sha256(entry.encode("utf-8")).hexdigest()
            logger.warning(
                "Invalid git-filter spec entry (no colon). sha256=%s length=%d",
                entry_fp,
                len(entry),
            )
            continue
        # Split on first colon only (tokens might contain colons)
        project_pattern, tokens_str = entry.split(":", 1)
        project_pattern = project_pattern.strip()
        if not project_pattern:
            continue
        token_list = [t.strip() for t in tokens_str.split(",") if t.strip()]
        if token_list:
            result[project_pattern] = token_list

    return result
