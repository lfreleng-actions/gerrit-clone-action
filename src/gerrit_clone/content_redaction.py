# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""Credential redaction primitives for history rewriting.

Generates safe, deterministic placeholder values for credential
strings, writes the ``git filter-repo --replace-text`` mapping file and
runs the rewrite itself.  The orchestration (and the ``git
filter-repo`` availability check) lives in
:mod:`gerrit_clone.content_filter`.
"""

from __future__ import annotations

import hashlib
import tempfile
from typing import TYPE_CHECKING

from gerrit_clone.content_git import run_content_git
from gerrit_clone.logging import get_logger

if TYPE_CHECKING:
    from pathlib import Path

logger = get_logger(__name__)


def _generate_replacement_string(original: str) -> str:
    """Generate a safe replacement for a credential string.

    The replacement is:
    - Deterministic (same input always produces the same output)
    - A different length from typical token lengths (to avoid pattern matching)
    - Prefixed with ``REDACTED_`` for clarity
    - NOT decodable back to the original value

    Uses a SHA-256 hash with a fixed namespace prefix to produce
    a fixed-length hex string.

    Args:
        original: The original credential string to replace.

    Returns:
        A safe replacement string like ``REDACTED_a1b2c3d4e5f6``.
    """
    # Use SHA-256 with a salt to generate a deterministic but
    # non-reversible replacement.  Truncate to 12 hex chars (48 bits)
    # which is enough to be unique within a repo while being a
    # clearly different from typical token lengths.
    # The salt keeps the old tool name deliberately: renaming it would
    # change every replacement, so a tree an earlier release redacted
    # would no longer match a fresh rewrite.
    digest = hashlib.sha256(f"gerrit-clone-redact:{original}".encode()).hexdigest()[:12]
    return f"REDACTED_{digest}"


def _write_replacements_file(tokens: list[str]) -> tuple[str, int]:
    """Write the ``git filter-repo --replace-text`` mapping file.

    Each accepted token becomes one ``literal:TOKEN==>REPLACEMENT`` line.
    The selector prefix is written explicitly so the parse cannot be
    steered by the token text.  Tokens that would corrupt the file
    format are skipped.

    Args:
        tokens: Candidate credential strings to redact.

    Returns:
        Tuple of ``(path_to_mapping_file, accepted_token_count)``.  The
        path is only returned once the file has been written in full,
        so a caller that never receives it has nothing to clean up.
    """
    with tempfile.NamedTemporaryFile(
        mode="w",
        prefix="source-clone-replacements-",
        suffix=".txt",
        delete=False,
        # Pin the encoding and newline so the mapping file is
        # written identically regardless of the ambient locale:
        # git filter-repo reads --replace-text as UTF-8, and a
        # deterministic "\n" avoids platform newline translation
        # that could corrupt an entry.
        encoding="utf-8",
        newline="\n",
    ) as tmp:
        valid_count = 0
        for token in tokens:
            # Validate token: reject values that would corrupt
            # the replacement file format or produce malformed
            # lines.
            if "\n" in token or "\r" in token or "\0" in token:
                logger.warning(
                    "Skipping token containing newline/NUL (sha256:%s)",
                    hashlib.sha256(token.encode()).hexdigest()[:12],
                )
                continue
            if "==>" in token:
                logger.warning(
                    "Skipping token containing '==>' delimiter (sha256:%s)",
                    hashlib.sha256(token.encode()).hexdigest()[:12],
                )
                continue

            replacement = _generate_replacement_string(token)
            # git filter-repo reads each line as [selector:]match==>replacement
            # and honours "literal:", "regex:" and "glob:" prefixes. A bare
            # line defaults to literal, but that default is decided by the
            # token text, so a token beginning with one of those prefixes
            # would be parsed as a selector: a configured token of "regex:.*"
            # would rewrite the whole repository instead of redacting that
            # one string. Prefixing explicitly makes the parse independent
            # of the token, and is a no-op for every other value.
            tmp.write(f"literal:{token}==>{replacement}\n")
            valid_count += 1
            fingerprint = hashlib.sha256(token.encode()).hexdigest()[:12]
            logger.debug(
                "Token replacement: [sha256:%s] → %s",
                fingerprint,
                replacement,
            )
        return tmp.name, valid_count


def _run_replace_text(
    repo_path: Path,
    replacements_file: str,
    valid_count: int,
    timeout: int,
) -> bool:
    """Run ``git filter-repo --replace-text`` against *repo_path*.

    Args:
        repo_path: Repository to rewrite.
        replacements_file: Path to the mapping file.
        valid_count: Number of tokens in the mapping file (for logging).
        timeout: Timeout in seconds for the rewrite.

    Returns:
        ``True`` when the rewrite succeeded.

    Raises:
        ProcessAbandonedError: If the batch was abandoned before or while
            the rewrite ran.
        subprocess.TimeoutExpired: If the rewrite exceeds *timeout*.
    """
    cmd = [
        "git",
        "-C",
        str(repo_path),
        "filter-repo",
        "--replace-text",
        replacements_file,
        "--force",
    ]

    logger.info(
        "Replacing %d token(s) in history of %s",
        valid_count,
        repo_path.name,
    )

    result = run_content_git(cmd, timeout=timeout)

    if result.returncode != 0:
        logger.error(
            "git filter-repo --replace-text failed for %s: %s",
            repo_path.name,
            result.stderr.strip(),
        )
        return False

    logger.info(
        "Successfully replaced %d token(s) in %s",
        valid_count,
        repo_path.name,
    )
    return True
