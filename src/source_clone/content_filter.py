# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2025 Matthew Watkins <mwatkins@linuxfoundation.org>

"""Content filtering entry points for repository operations.

Provides three main capabilities:

1. **File removal** — Remove files/folders matching glob patterns from
   bare git repositories before pushing to a target platform.  This
   prevents unwanted files (e.g. ``.github/dependabot.yml``) from
   triggering platform-specific side effects in the target.

2. **Token replacement** — Rewrite git history to replace credential
   strings with safe placeholder values, allowing repositories that
   contain accidentally committed secrets to be mirrored without
   triggering secret-scanning blocks.

3. **Secret scanning** — Automatically detect well-known credential
   patterns (e.g. GitLab PATs, GitHub PATs, AWS keys) in repository
   content and replace them with safe placeholder values.

The supporting implementations live in sibling modules
(:mod:`~source_clone.content_patterns`,
:mod:`~source_clone.content_scan`,
:mod:`~source_clone.content_removal`,
:mod:`~source_clone.content_worktree` and
:mod:`~source_clone.content_redaction`) and are re-exported here so this
module remains the single public surface for content filtering.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

from source_clone.content_patterns import (
    _glob_to_regex,
    _match_regex_pattern,
    normalize_file_patterns,
    parse_git_filter_spec,
)
from source_clone.content_policy import (
    FilterPolicy,
    PolicyRecordError,
    collect_filter_tokens,
    content_filtering,
)
from source_clone.content_redaction import (
    _generate_replacement_string,
    _run_replace_text,
    _write_replacements_file,
)
from source_clone.content_removal import (
    _check_git_filter_repo,
    _remove_files_filter_repo,
)
from source_clone.content_scan import (
    SCAN_PATTERNS,
    is_shallow_repository,
    scan_repo_for_secrets,
)
from source_clone.content_worktree import _remove_files_worktree
from source_clone.logging import get_logger
from source_clone.subprocess_tracking import ProcessAbandonedError

logger = get_logger(__name__)

#: Names re-exported so this module stays the single public surface for
#: content filtering (callers and tests patch ``source_clone.
#: content_filter.<name>``).
__all__ = [
    "SCAN_PATTERNS",
    # Private names re-exported from the split-out modules because the
    # test suite imports and patches them through this module.
    "_check_git_filter_repo",
    "_generate_replacement_string",
    "_glob_to_regex",
    "_remove_files_filter_repo",
    "apply_content_filters",
    "is_shallow_repository",
    "match_file_pattern",
    "normalize_file_patterns",
    "parse_git_filter_spec",
    "remove_files_from_bare_repo",
    "replace_tokens_in_history",
    "scan_repo_for_secrets",
]


def _match_glob_pattern(pattern: str, normalized: str) -> bool:
    """Match *normalized* against a shell-style glob pattern.

    Args:
        pattern: Glob pattern (separators not yet normalized).
        normalized: Forward-slash normalized path.

    Returns:
        ``True`` on a match; ``False`` for empty or invalid patterns.
    """
    # Normalize the pattern's separators to match.
    pat = pattern.replace("\\", "/")

    if not normalized or not pat:
        return False

    regex = _glob_to_regex(pat)

    try:
        # Anchored full-path match.
        if re.fullmatch(regex, normalized):
            return True

        if "/" in pat:
            # Multi-component pattern: allow it to match as a path
            # suffix, e.g. ".github/dependabot.yml" matches
            # "some/prefix/.github/dependabot.yml".
            return bool(re.fullmatch(r"(?:.*/)?" + regex, normalized))

        # Single-component pattern: match against any path segment.
        return any(re.fullmatch(regex, part) for part in normalized.split("/"))
    except re.error as exc:
        # A malformed glob (e.g. an unterminated bracket class that
        # ``_glob_to_regex`` turns into an invalid regex) must not
        # crash filtering.  Mirror the guarded ``regex:`` path: warn
        # and treat the pattern as non-matching so --remove-files
        # fails gracefully rather than raising.
        logger.warning("Invalid glob pattern %r: %s", pattern, exc)
        return False


def match_file_pattern(file_path: str, pattern: str) -> bool:
    """Match a file path against a glob or regex pattern.

    Supports:
    - Shell-style globs: ``*``, ``?``, ``[seq]``, ``**`` (recursive)
    - Regex patterns: prefixed with ``regex:`` (e.g. ``regex:\\.pyc$``)

    Glob wildcards are path-segment aware: ``*`` and ``?`` do not
    match across ``/`` separators.  Use ``**`` for recursive matching.

    Args:
        file_path: Relative file path within the repository.
        pattern: Glob or ``regex:``-prefixed regex pattern.

    Returns:
        ``True`` if *file_path* matches *pattern*.
    """
    # Normalize separators up front so both regex and glob matching
    # see a consistent forward-slash path representation regardless
    # of the platform that produced *file_path*.
    normalized = file_path.replace("\\", "/")

    if pattern.startswith("regex:"):
        return _match_regex_pattern(pattern[len("regex:") :], normalized)

    return _match_glob_pattern(pattern, normalized)


def _matches_for_removal(file_path: str, pattern: str) -> bool:
    """Match a file for removal, including directory-prefix matches.

    Extends :func:`match_file_pattern` so that a plain (non-glob,
    non-``regex:``) path pattern that names a directory also matches
    every file nested under it.  This mirrors ``git filter-repo``'s
    ``--path`` prefix semantics (used by the preferred removal path)
    so the worktree fallback removes folders consistently rather than
    only matching a file whose whole path equals the pattern.
    """
    if match_file_pattern(file_path, pattern):
        return True
    # Directory-prefix matching only applies to plain path patterns;
    # ``regex:`` and glob patterns already express their own scope.
    if pattern.startswith("regex:"):
        return False
    if any(c in pattern for c in ("*", "?", "[", "]")):
        return False
    normalized = file_path.replace("\\", "/")
    prefix = pattern.replace("\\", "/").rstrip("/")
    return bool(prefix) and normalized.startswith(prefix + "/")


def remove_files_from_bare_repo(
    repo_path: Path,
    patterns: list[str],
    *,
    timeout: int = 300,
) -> list[str]:
    """Remove files matching patterns from a bare git repository.

    Uses ``git filter-repo`` when available (preferred — removes from
    all history).  Falls back to worktree-based removal that only
    affects branch tips when ``git filter-repo`` is not installed.

    Args:
        repo_path: Path to the bare git repository.
        patterns: List of file path glob/regex patterns to remove.
        timeout: Timeout in seconds for git operations.

    Returns:
        List of pattern arguments or file paths that were processed.
    """
    if not patterns:
        return []

    if not repo_path.exists():
        logger.warning("Repository path does not exist: %s", repo_path)
        return []

    if _check_git_filter_repo():
        return _remove_files_filter_repo(repo_path, patterns, timeout=timeout)
    return _remove_files_worktree(
        repo_path, patterns, _matches_for_removal, timeout=timeout
    )


def replace_tokens_in_history(
    repo_path: Path,
    tokens: list[str],
    *,
    timeout: int = 600,
) -> bool:
    """Replace credential strings in repository history.

    Uses ``git filter-repo --replace-text`` to rewrite all blobs in the
    repository, replacing each token with a safe placeholder value.

    Requires ``git filter-repo`` to be installed.

    Args:
        repo_path: Path to the bare or regular git repository.
        tokens: List of credential strings to replace.
        timeout: Timeout in seconds for the operation.

    Returns:
        ``True`` if replacement was successful, ``False`` otherwise.

    Raises:
        RuntimeError: If ``git filter-repo`` is not available.
    """
    if not tokens:
        return True

    if not _check_git_filter_repo():
        raise RuntimeError(
            "git filter-repo is required for token replacement "
            "but is not installed. Install it with: "
            "pip install git-filter-repo"
        )

    # Build the replacement expressions file
    # Format: LITERAL_STRING==>REPLACEMENT
    replacements_file = None
    try:
        replacements_file, valid_count = _write_replacements_file(tokens)

        if valid_count == 0:
            logger.warning(
                "No valid tokens to replace in %s "
                "(all %d were skipped during validation)",
                repo_path.name,
                len(tokens),
            )
            return True

        return _run_replace_text(repo_path, replacements_file, valid_count, timeout)

    except ProcessAbandonedError:
        raise
    except subprocess.TimeoutExpired:
        logger.error(
            "Token replacement timed out for %s after %ds",
            repo_path.name,
            timeout,
        )
        return False
    except Exception as exc:
        logger.error(
            "Token replacement error for %s: %s",
            repo_path.name,
            exc,
        )
        return False
    finally:
        if replacements_file and Path(replacements_file).exists():
            Path(replacements_file).unlink()


def apply_content_filters(
    repo_path: Path,
    project_name: str,
    remove_patterns: list[str] | None = None,
    git_filter_projects: dict[str, list[str]] | None = None,
    *,
    redact_secrets: bool = False,
    timeout: int = 600,
) -> tuple[bool, str | None]:
    """Apply content filters to a cloned repository before push.

    This is the main entry point for content filtering, called by
    the mirror manager after cloning from Gerrit and before pushing
    to GitHub.

    The policy filtered under is recorded first, and filtering refused
    if it cannot be; afterwards ``origin`` is put back for fetching and
    pushing is blocked.  See :mod:`source_clone.content_policy`.

    Args:
        repo_path: Path to the cloned (bare) repository.
        project_name: Gerrit project name (for matching against
            git_filter_projects keys).
        remove_patterns: File path patterns to remove from the repo.
        git_filter_projects: Mapping of project name patterns to lists
            of token strings to replace.  Project names support the
            same wildcard/hierarchical matching as
            ``--include-projects``.
        redact_secrets: When ``True``, scan repository content for
            well-known credential patterns and replace any discovered
            tokens with safe placeholder values.  This runs after
            explicit token replacement (Step 2) so that any tokens
            already handled are not double-processed.
        timeout: Timeout in seconds for filtering operations.

    Returns:
        Tuple of ``(success, error_message)``.
    """
    tokens = (
        collect_filter_tokens(project_name, git_filter_projects)
        if git_filter_projects
        else []
    )
    policy = FilterPolicy.of(remove_patterns, tokens, redact_secrets)
    if policy.empty:
        return True, None
    try:
        with content_filtering(repo_path, policy) as safety_errors:
            errors = _run_filters(
                repo_path,
                project_name,
                remove_patterns,
                tokens,
                redact_secrets,
                timeout,
            )

    except PolicyRecordError as exc:
        logger.error(str(exc))
        return False, str(exc)

    errors.extend(safety_errors)
    if errors:
        return False, "; ".join(errors)
    return True, None


def _run_filters(
    repo_path: Path,
    project_name: str,
    remove_patterns: list[str] | None,
    tokens: list[str],
    redact_secrets: bool,
    timeout: int,
) -> list[str]:
    """Apply each requested filter to *repo_path*, collecting the failures."""
    errors: list[str] = []
    if remove_patterns:
        try:
            removed = remove_files_from_bare_repo(
                repo_path, remove_patterns, timeout=timeout
            )
            if removed:
                logger.info(
                    "Content filter: removed %d path(s) from %s",
                    len(removed),
                    project_name,
                )
        except ProcessAbandonedError:
            raise
        except Exception as exc:
            msg = f"File removal failed for {project_name}: {exc}"
            logger.error(msg)
            errors.append(msg)

    # Tokens from all matching patterns, so filter-repo runs once.
    if tokens:
        logger.info(
            "Applying token replacement to %s (%d unique token(s))",
            project_name,
            len(tokens),
        )
        try:
            success = replace_tokens_in_history(
                repo_path,
                tokens,
                timeout=timeout,
            )
            if not success:
                msg = f"Token replacement failed for {project_name}"
                errors.append(msg)
        except ProcessAbandonedError:
            raise
        except RuntimeError as exc:
            msg = str(exc)
            logger.error(msg)
            errors.append(msg)

    if redact_secrets:
        try:
            discovered = scan_repo_for_secrets(repo_path, timeout=timeout)
            if discovered:
                logger.info(
                    "Redacting %d auto-discovered secret(s) from %s",
                    len(discovered),
                    project_name,
                )
                success = replace_tokens_in_history(
                    repo_path,
                    discovered,
                    timeout=timeout,
                )
                if not success:
                    msg = f"Auto-redaction failed for {project_name}"
                    errors.append(msg)
            else:
                logger.debug(
                    "No secrets found to redact in %s",
                    project_name,
                )
        except ProcessAbandonedError:
            raise
        except (RuntimeError, OSError) as exc:
            # RuntimeError covers the scan/redaction fail-closed
            # paths; OSError (e.g. FileNotFoundError when git is
            # missing) can surface from subprocess.Popen.  Both are
            # reported as filter failures so the (success, error)
            # contract always holds.
            msg = str(exc)
            logger.error(msg)
            errors.append(msg)
    return errors
