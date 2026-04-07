# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2025 Matthew Watkins <mwatkins@linuxfoundation.org>

"""Content filtering utilities for repository operations.

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
# Well-known credential patterns for automatic secret detection
# ---------------------------------------------------------------------------

#: Compiled regex patterns for well-known credential formats.
#: Each pattern is designed to match the token value itself (no
#: surrounding context required) so it can be used as a literal
#: replacement target for ``git filter-repo --replace-text``.
SECRET_PATTERNS: dict[str, re.Pattern[str]] = {
    # GitLab Personal Access Tokens (glpat-XXXX...)
    "gitlab_pat": re.compile(r"glpat-[A-Za-z0-9_\-]{20,}"),
    # GitHub classic Personal Access Tokens (ghp_XXXX...)
    "github_pat_classic": re.compile(r"ghp_[A-Za-z0-9]{36,}"),
    # GitHub fine-grained Personal Access Tokens
    "github_pat_fine_grained": re.compile(
        r"github_pat_[A-Za-z0-9_]{22,}"
    ),
    # GitHub OAuth access tokens (gho_XXXX...)
    "github_oauth": re.compile(r"gho_[A-Za-z0-9]{36,}"),
    # GitHub user-to-server tokens (ghu_XXXX...)
    "github_app_user": re.compile(r"ghu_[A-Za-z0-9]{36,}"),
    # GitHub server-to-server tokens (ghs_XXXX...)
    "github_app_server": re.compile(r"ghs_[A-Za-z0-9]{36,}"),
    # GitHub app refresh tokens (ghr_XXXX...)
    "github_app_refresh": re.compile(r"ghr_[A-Za-z0-9]{36,}"),
    # AWS Access Key IDs (AKIA...)
    "aws_access_key_id": re.compile(r"AKIA[0-9A-Z]{16}"),
    # Slack bot/user/workspace tokens (xoxb-, xoxp-, xoxa-, xoxr-, xoxs-)
    "slack_token": re.compile(r"xox[bpars]-[A-Za-z0-9\-]{10,}"),
    # Slack webhook URLs
    "slack_webhook": re.compile(
        r"https://hooks\.slack\.com/services/T[A-Za-z0-9]+/"
        r"B[A-Za-z0-9]+/[A-Za-z0-9]+"
    ),
    # Stripe API keys (sk_live_/sk_test_/pk_live_/pk_test_)
    "stripe_api_key": re.compile(
        r"(?:sk|pk)_(?:live|test)_[A-Za-z0-9]{20,}"
    ),
    # Twilio API keys
    "twilio_api_key": re.compile(r"SK[a-f0-9]{32}"),
    # SendGrid API keys
    "sendgrid_api_key": re.compile(r"SG\.[A-Za-z0-9_\-]{22,}\.[A-Za-z0-9_\-]{22,}"),
    # Google API keys
    "google_api_key": re.compile(r"AIza[A-Za-z0-9_\-]{35}"),
    # Heroku API keys
    "heroku_api_key": re.compile(
        r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}"
        r"-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
    ),
    # npm tokens
    "npm_token": re.compile(r"npm_[A-Za-z0-9]{36}"),
    # PyPI API tokens
    "pypi_token": re.compile(r"pypi-[A-Za-z0-9_\-]{50,}"),
    # Mailchimp API keys
    "mailchimp_api_key": re.compile(
        r"[0-9a-f]{32}-us[0-9]{1,2}"
    ),
}


def scan_repo_for_secrets(
    repo_path: Path,
    *,
    timeout: int = 300,
) -> list[str]:
    """Scan repository content for well-known credential patterns.

    Iterates over all blob content in the repository using
    ``git log --all -p`` and matches each line against the
    built-in :data:`SECRET_PATTERNS` dictionary.

    Args:
        repo_path: Path to the git repository (bare or regular).
        timeout: Timeout in seconds for the git log operation.

    Returns:
        Deduplicated list of discovered credential strings,
        in the order they were first encountered.
    """
    if not repo_path.exists():
        return []

    cmd = [
        "git",
        "-C",
        str(repo_path),
        "log",
        "--all",
        "--diff-filter=ACMR",
        "-p",
        "--no-color",
    ]

    try:
        result = subprocess.run(
            cmd,
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        logger.warning(
            "Secret scan timed out for %s after %ds",
            repo_path.name,
            timeout,
        )
        return []

    if result.returncode != 0:
        logger.warning(
            "Secret scan git log failed for %s: %s",
            repo_path.name,
            result.stderr.strip(),
        )
        return []

    # Scan output line by line for known patterns
    seen: set[str] = set()
    discovered: list[str] = []

    for line in result.stdout.splitlines():
        # Only scan diff addition lines (lines starting with +)
        # and context lines, skip diff headers
        stripped = line.lstrip("+")
        if not stripped or line.startswith("---") or line.startswith("+++"):
            continue

        for pattern_name, pattern in SECRET_PATTERNS.items():
            for match in pattern.finditer(stripped):
                token = match.group(0)
                if token not in seen:
                    seen.add(token)
                    discovered.append(token)
                    logger.info(
                        "Secret scan: found %s pattern "
                        "(sha256:%s) in %s",
                        pattern_name,
                        hashlib.sha256(
                            token.encode()
                        ).hexdigest()[:12],
                        repo_path.name,
                    )

    if discovered:
        logger.info(
            "Secret scan: found %d unique credential(s) in %s",
            len(discovered),
            repo_path.name,
        )
    else:
        logger.debug(
            "Secret scan: no credentials found in %s",
            repo_path.name,
        )

    return discovered


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
        try:
            return bool(re.search(regex, file_path))
        except re.error as exc:
            logger.warning("Invalid regex pattern %r: %s", regex, exc)
            return False

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
    matched = False
    if "/" in pat:
        # Multi-component pattern: check if path ends with pattern
        matched = normalized.endswith("/" + pat) or normalized == pat
    else:
        # Single-component: match against any path segment
        parts = normalized.split("/")
        matched = any(fnmatch.fnmatchcase(part, pat) for part in parts)

    return matched


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
        List of pattern arguments or file paths that were processed.
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
        elif any(c in pattern for c in ("*", "?", "[", "]")):
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
            msg = (
                f"git filter-repo failed for {repo_path.name}: {result.stderr.strip()}"
            )
            logger.error(msg)
            raise RuntimeError(msg)

        logger.info(
            "Successfully filtered files from %s",
            repo_path.name,
        )
        return applied
    except subprocess.TimeoutExpired:
        msg = f"git filter-repo timed out for {repo_path.name} after {timeout}s"
        logger.error(msg)
        raise RuntimeError(msg) from None
    except RuntimeError:
        raise
    except Exception as exc:
        msg = f"git filter-repo error for {repo_path.name}: {exc}"
        logger.error(msg)
        raise RuntimeError(msg) from exc


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
    timeout: int = 300,
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
            timeout=timeout,
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
                timeout=timeout,
                check=True,
            )

            # Remove matching files
            for file_path in files_to_remove:
                full_path = Path(worktree_dir) / file_path
                if full_path.exists():
                    rm_result = subprocess.run(
                        [
                            "git",
                            "-C",
                            worktree_dir,
                            "rm",
                            "-f",
                            "--",
                            file_path,
                        ],
                        check=False,
                        capture_output=True,
                        text=True,
                        timeout=timeout,
                    )
                    if rm_result.returncode != 0:
                        raise RuntimeError(
                            f"git rm failed for '{file_path}' on "
                            f"branch '{branch}' in "
                            f"{repo_path.name}: "
                            f"{rm_result.stderr.strip()}"
                        )

            # Commit the removal
            result = subprocess.run(
                [
                    "git",
                    "-C",
                    worktree_dir,
                    "-c",
                    "user.name=gerrit-clone",
                    "-c",
                    "user.email=gerrit-clone@noreply",
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
                timeout=timeout,
            )

            if result.returncode != 0:
                raise RuntimeError(
                    f"git commit failed on branch '{branch}' in "
                    f"{repo_path.name}: {result.stderr.strip()}"
                )

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
                timeout=timeout,
            )
            if Path(worktree_dir).exists():
                shutil.rmtree(worktree_dir, ignore_errors=True)

    unique_removed = sorted(set(all_removed))
    if unique_removed:
        logger.info(
            "Removed %d unique file(s) from %s across %d branch(es)",
            len(unique_removed),
            repo_path.name,
            len(branches),
        )
    return unique_removed


# ---------------------------------------------------------------------------
# Feature 2: Token/credential replacement in git history
# ---------------------------------------------------------------------------


def _generate_replacement_string(original: str) -> str:
    """Generate a safe replacement for a credential string.

    The replacement is:
    - Deterministic (same input always produces the same output)
    - A different length from typical token lengths (to avoid pattern matching)
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
    # clearly different from typical token lengths.
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
                # git filter-repo format: literal==>replacement
                tmp.write(f"{token}==>{replacement}\n")
                fingerprint = hashlib.sha256(token.encode()).hexdigest()[:12]
                logger.debug(
                    "Token replacement: [sha256:%s] → %s",
                    fingerprint,
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
    redact_secrets: bool = False,
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
        redact_secrets: When ``True``, scan repository content for
            well-known credential patterns and replace any discovered
            tokens with safe placeholder values.  This runs after
            explicit token replacement (Step 2) so that any tokens
            already handled are not double-processed.
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
    # Aggregate tokens from all matching patterns so filter-repo runs once.
    if git_filter_projects:
        aggregated_tokens: list[str] = []
        matched_patterns: list[str] = []
        for pattern, token_list in git_filter_projects.items():
            if match_project_pattern(project_name, pattern):
                matched_patterns.append(pattern)
                aggregated_tokens.extend(token_list)

        if aggregated_tokens:
            # Deduplicate while preserving order
            seen: set[str] = set()
            unique_tokens: list[str] = []
            for t in aggregated_tokens:
                if t not in seen:
                    seen.add(t)
                    unique_tokens.append(t)

            logger.info(
                "Applying token replacement to %s "
                "(matched %d filter pattern(s): %s, %d unique token(s))",
                project_name,
                len(matched_patterns),
                matched_patterns,
                len(unique_tokens),
            )
            try:
                success = replace_tokens_in_history(
                    repo_path,
                    unique_tokens,
                    timeout=timeout,
                )
                if not success:
                    msg = f"Token replacement failed for {project_name}"
                    errors.append(msg)
            except RuntimeError as exc:
                msg = str(exc)
                logger.error(msg)
                errors.append(msg)

    # Step 3: Auto-detect and redact secrets if requested
    if redact_secrets:
        try:
            discovered = scan_repo_for_secrets(
                repo_path, timeout=timeout
            )
            if discovered:
                logger.info(
                    "Redacting %d auto-discovered secret(s) "
                    "from %s",
                    len(discovered),
                    project_name,
                )
                success = replace_tokens_in_history(
                    repo_path,
                    discovered,
                    timeout=timeout,
                )
                if not success:
                    msg = (
                        f"Auto-redaction failed for "
                        f"{project_name}"
                    )
                    errors.append(msg)
            else:
                logger.debug(
                    "No secrets found to redact in %s",
                    project_name,
                )
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
