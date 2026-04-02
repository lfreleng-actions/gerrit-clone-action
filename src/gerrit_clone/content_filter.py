# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2025 Matthew Watkins <mwatkins@linuxfoundation.org>

"""Content filtering utilities for repository operations.

Provides two main capabilities:

1. **File removal** — Remove files/folders matching glob patterns from
   bare git repositories before pushing to a target platform.  This
   prevents unwanted files (e.g. ``.github/dependabot.yml``) from
   triggering platform-specific side effects in the target.

2. **Token replacement** — Rewrite git history to replace credential
   strings with safe placeholder values, allowing repositories that
   contain accidentally committed secrets to be mirrored without
   triggering secret-scanning blocks.
"""

from __future__ import annotations

import fnmatch
import hashlib
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

from gerrit_clone.logging import get_logger
from gerrit_clone.models import match_project_pattern

logger = get_logger(__name__)


# ---------------------------------------------------------------------------
# Pattern matching helpers
# ---------------------------------------------------------------------------


def match_file_pattern(file_path: str, pattern: str) -> bool:
    """Match a file path against a glob or regex pattern.

    Supports:
    - Shell-style globs: ``*``, ``?``, ``[seq]``, ``**`` (recursive)
    - Regex patterns: prefixed with ``regex:`` (e.g. ``regex:\\.pyc$``)

    Args:
        file_path: Relative file path within the repository.
        pattern: Glob or ``regex:``-prefixed regex pattern.

    Returns:
        ``True`` if *file_path* matches *pattern*.
    """
    if pattern.startswith("regex:"):
        regex = pattern[len("regex:") :]
        return bool(re.search(regex, file_path))

    # Normalize separators for matching
    normalized = file_path.replace("\\", "/")
    pat = pattern.replace("\\", "/")

    # Support ** for recursive matching
    if "**" in pat:
        # Convert ** glob to regex
        regex_pat = fnmatch.translate(pat).replace(r"(?s:.*)", ".*")
        return bool(re.match(regex_pat, normalized))

    # Try matching against full path and basename
    if fnmatch.fnmatchcase(normalized, pat):
        return True

    # Also try matching just the filename or relative segments
    # e.g., pattern ".github/dependabot.yml" should match
    # "some/prefix/.github/dependabot.yml"
    if "/" in pat:
        # Multi-component pattern: check if path ends with pattern
        if normalized.endswith("/" + pat) or normalized == pat:
            return True
    else:
        # Single-component: match against any path segment
        parts = normalized.split("/")
        if any(fnmatch.fnmatchcase(part, pat) for part in parts):
            return True

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


# ---------------------------------------------------------------------------
# Feature 1: File removal from bare repositories
# ---------------------------------------------------------------------------


def _check_git_filter_repo() -> bool:
    """Check if git-filter-repo is available.

    Returns:
        ``True`` if ``git filter-repo`` is available on PATH.
    """
    try:
        result = subprocess.run(
            ["git", "filter-repo", "--version"],
            check=False,
            capture_output=True,
            text=True,
            timeout=5,
        )
        return result.returncode == 0
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False


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
        List of file paths that were removed.
    """
    if not patterns:
        return []

    if not repo_path.exists():
        logger.warning("Repository path does not exist: %s", repo_path)
        return []

    if _check_git_filter_repo():
        return _remove_files_filter_repo(repo_path, patterns, timeout=timeout)
    return _remove_files_worktree(repo_path, patterns, timeout=timeout)


def _remove_files_filter_repo(
    repo_path: Path,
    patterns: list[str],
    *,
    timeout: int = 300,
) -> list[str]:
    """Remove files using git filter-repo (all history).

    Args:
        repo_path: Path to the bare git repository.
        patterns: File path patterns to remove.
        timeout: Timeout for the operation.

    Returns:
        List of pattern arguments that were applied.
    """
    cmd: list[str] = [
        "git",
        "-C",
        str(repo_path),
        "filter-repo",
        "--force",
    ]

    applied: list[str] = []
    for pattern in patterns:
        if pattern.startswith("regex:"):
            # Use --path-regex with --invert-paths
            regex = pattern[len("regex:") :]
            cmd.extend(["--path-regex", regex, "--invert-paths"])
            applied.append(pattern)
        elif "**" in pattern or "*" in pattern or "?" in pattern:
            cmd.extend(["--path-glob", pattern, "--invert-paths"])
            applied.append(pattern)
        else:
            # Exact path — use --path with --invert-paths
            cmd.extend(["--path", pattern, "--invert-paths"])
            applied.append(pattern)

    if not applied:
        return []

    logger.info(
        "Removing files from %s using git filter-repo: %s",
        repo_path.name,
        applied,
    )

    try:
        result = subprocess.run(
            cmd,
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        if result.returncode != 0:
            logger.error(
                "git filter-repo failed for %s: %s",
                repo_path.name,
                result.stderr.strip(),
            )
            return []

        logger.info(
            "Successfully filtered files from %s",
            repo_path.name,
        )
        return applied
    except subprocess.TimeoutExpired:
        logger.error(
            "git filter-repo timed out for %s after %ds",
            repo_path.name,
            timeout,
        )
        return []
    except Exception as exc:
        logger.error(
            "git filter-repo error for %s: %s",
            repo_path.name,
            exc,
        )
        return []


def _list_tree_files(repo_path: Path, ref: str) -> list[str]:
    """List all files in a bare repo at a given ref.

    Args:
        repo_path: Path to the bare git repository.
        ref: Git ref to list files from.

    Returns:
        List of file paths relative to the repo root.
    """
    try:
        result = subprocess.run(
            [
                "git",
                "-C",
                str(repo_path),
                "ls-tree",
                "-r",
                "--name-only",
                ref,
            ],
            check=False,
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.returncode != 0:
            return []
        return [line for line in result.stdout.strip().splitlines() if line]
    except (subprocess.TimeoutExpired, Exception):
        return []


def _remove_files_worktree(
    repo_path: Path,
    patterns: list[str],
    *,
    timeout: int = 300,  # noqa: ARG001
) -> list[str]:
    """Remove files from branch tips using a temporary worktree.

    This fallback method creates a temporary worktree for each branch,
    removes matching files, and commits the changes.  Unlike
    ``git filter-repo``, this only affects the branch tips — historical
    commits still contain the removed files.

    Args:
        repo_path: Path to the bare git repository.
        patterns: File path patterns to remove.
        timeout: Timeout for git operations.

    Returns:
        List of files that were removed (across all branches).
    """
    all_removed: list[str] = []

    # Get list of branches
    try:
        result = subprocess.run(
            [
                "git",
                "-C",
                str(repo_path),
                "for-each-ref",
                "--format=%(refname:short)",
                "refs/heads/",
            ],
            check=False,
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.returncode != 0:
            logger.error(
                "Failed to list branches in %s: %s",
                repo_path.name,
                result.stderr.strip(),
            )
            return []
        branches = [b for b in result.stdout.strip().splitlines() if b]
    except (subprocess.TimeoutExpired, Exception) as exc:
        logger.error(
            "Failed to list branches in %s: %s",
            repo_path.name,
            exc,
        )
        return []

    if not branches:
        logger.debug("No branches found in %s", repo_path.name)
        return []

    for branch in branches:
        # List files on this branch
        files = _list_tree_files(repo_path, branch)
        if not files:
            continue

        # Find files matching any pattern
        files_to_remove = [
            f for f in files if any(match_file_pattern(f, pat) for pat in patterns)
        ]

        if not files_to_remove:
            continue

        logger.debug(
            "Removing %d file(s) from branch '%s' in %s: %s",
            len(files_to_remove),
            branch,
            repo_path.name,
            files_to_remove[:5],
        )

        # Create temporary worktree
        worktree_dir = tempfile.mkdtemp(prefix=f"gerrit-clone-filter-{repo_path.name}-")
        try:
            subprocess.run(
                [
                    "git",
                    "-C",
                    str(repo_path),
                    "worktree",
                    "add",
                    worktree_dir,
                    branch,
                ],
                capture_output=True,
                text=True,
                timeout=60,
                check=True,
            )

            # Remove matching files
            for file_path in files_to_remove:
                full_path = Path(worktree_dir) / file_path
                if full_path.exists():
                    subprocess.run(
                        [
                            "git",
                            "-C",
                            worktree_dir,
                            "rm",
                            "-f",
                            file_path,
                        ],
                        check=False,
                        capture_output=True,
                        text=True,
                        timeout=30,
                    )

            # Commit the removal
            result = subprocess.run(
                [
                    "git",
                    "-C",
                    worktree_dir,
                    "commit",
                    "-m",
                    "Remove filtered files for platform sync\n\n"
                    "Files removed by gerrit-clone content "
                    "filter "
                    "to prevent platform-specific side effects.",
                    "--allow-empty",
                ],
                check=False,
                capture_output=True,
                text=True,
                timeout=30,
            )

            if result.returncode == 0:
                all_removed.extend(files_to_remove)
                logger.debug(
                    "Committed removal of %d files on branch '%s'",
                    len(files_to_remove),
                    branch,
                )

        except subprocess.CalledProcessError as exc:
            logger.warning(
                "Failed to create worktree for branch '%s' in %s: %s",
                branch,
                repo_path.name,
                exc.stderr,
            )
        finally:
            # Clean up worktree
            subprocess.run(
                [
                    "git",
                    "-C",
                    str(repo_path),
                    "worktree",
                    "remove",
                    "--force",
                    worktree_dir,
                ],
                check=False,
                capture_output=True,
                text=True,
                timeout=30,
            )
            if Path(worktree_dir).exists():
                shutil.rmtree(worktree_dir, ignore_errors=True)

    if all_removed:
        unique_removed = sorted(set(all_removed))
        logger.info(
            "Removed %d unique file(s) from %s across %d branch(es)",
            len(unique_removed),
            repo_path.name,
            len(branches),
        )
    return all_removed


# ---------------------------------------------------------------------------
# Feature 2: Token/credential replacement in git history
# ---------------------------------------------------------------------------


def _generate_replacement_string(original: str) -> str:
    """Generate a safe replacement for a credential string.

    The replacement is:
    - Deterministic (same input always produces the same output)
    - A different length from the original (to avoid pattern matching)
    - Prefixed with ``REDACTED_`` for clarity
    - NOT decodable back to the original value

    Uses a keyed hash to produce a fixed-length hex string.

    Args:
        original: The original credential string to replace.

    Returns:
        A safe replacement string like ``REDACTED_a1b2c3d4e5f6``.
    """
    # Use SHA-256 with a salt to generate a deterministic but
    # non-reversible replacement.  Truncate to 12 hex chars (48 bits)
    # which is enough to be unique within a repo while being a
    # clearly different length from typical tokens.
    digest = hashlib.sha256(f"gerrit-clone-redact:{original}".encode()).hexdigest()[:12]
    return f"REDACTED_{digest}"


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
        with tempfile.NamedTemporaryFile(
            mode="w",
            prefix="gerrit-clone-replacements-",
            suffix=".txt",
            delete=False,
        ) as tmp:
            for token in tokens:
                replacement = _generate_replacement_string(token)
                # git filter-repo format: literal==>replacement
                tmp.write(f"{token}==>{replacement}\n")
                logger.debug(
                    "Token replacement: %s... → %s",
                    token[:8] + "..." if len(token) > 8 else token,
                    replacement,
                )
            replacements_file = tmp.name

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
            len(tokens),
            repo_path.name,
        )

        result = subprocess.run(
            cmd,
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout,
        )

        if result.returncode != 0:
            logger.error(
                "git filter-repo --replace-text failed for %s: %s",
                repo_path.name,
                result.stderr.strip(),
            )
            return False

        logger.info(
            "Successfully replaced %d token(s) in %s",
            len(tokens),
            repo_path.name,
        )
        return True

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


# ---------------------------------------------------------------------------
# High-level filtering orchestration
# ---------------------------------------------------------------------------


def apply_content_filters(
    repo_path: Path,
    project_name: str,
    remove_patterns: list[str] | None = None,
    git_filter_projects: dict[str, list[str]] | None = None,
    *,
    timeout: int = 600,
) -> tuple[bool, str | None]:
    """Apply content filters to a cloned repository before push.

    This is the main entry point for content filtering, called by
    the mirror manager after cloning from Gerrit and before pushing
    to GitHub.

    Args:
        repo_path: Path to the cloned (bare) repository.
        project_name: Gerrit project name (for matching against
            git_filter_projects keys).
        remove_patterns: File path patterns to remove from the repo.
        git_filter_projects: Mapping of project name patterns to lists
            of token strings to replace.  Project names support the
            same wildcard/hierarchical matching as
            ``--include-projects``.
        timeout: Timeout in seconds for filtering operations.

    Returns:
        Tuple of ``(success, error_message)``.
    """
    errors: list[str] = []

    # Step 1: Remove files matching patterns
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
        except Exception as exc:
            msg = f"File removal failed for {project_name}: {exc}"
            logger.error(msg)
            errors.append(msg)

    # Step 2: Replace tokens if this project matches
    if git_filter_projects:
        for pattern, token_list in git_filter_projects.items():
            if match_project_pattern(project_name, pattern):
                logger.info(
                    "Applying token replacement to %s (matched filter pattern '%s')",
                    project_name,
                    pattern,
                )
                try:
                    success = replace_tokens_in_history(
                        repo_path,
                        token_list,
                        timeout=timeout,
                    )
                    if not success:
                        msg = f"Token replacement failed for {project_name}"
                        errors.append(msg)
                except RuntimeError as exc:
                    msg = str(exc)
                    logger.error(msg)
                    errors.append(msg)

    if errors:
        return False, "; ".join(errors)
    return True, None


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
            logger.warning(
                "Invalid git-filter spec entry (no colon): '%s'",
                entry,
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
