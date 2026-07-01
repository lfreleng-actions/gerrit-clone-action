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

import hashlib
import re
import shutil
import subprocess
import tempfile
import threading
import time
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
    # npm tokens
    "npm_token": re.compile(r"npm_[A-Za-z0-9]{36}"),
    # PyPI API tokens
    "pypi_token": re.compile(r"pypi-[A-Za-z0-9_\-]{50,}"),
    # Mailchimp API keys
    "mailchimp_api_key": re.compile(
        r"[0-9a-f]{32}-us[0-9]{1,2}"
    ),
}


def is_shallow_repository(repo_path: Path, *, timeout: int = 30) -> bool:
    """Return ``True`` if the git repo at *repo_path* is a shallow clone.

    Used to fail closed before running history-dependent filters
    (``--git-filter`` / ``--redact-secrets``): a shallow repository has a
    truncated history, so secret scanning / history rewriting could miss
    older leaked secrets (and a later unshallow fetch might reintroduce
    blocked content), giving a false sense of safety.

    Fails closed: if shallowness cannot be determined (``git`` missing,
    not a repository, or a timeout) the repo is treated as shallow so the
    caller refuses to run history-dependent filters against a repo whose
    full history could not be verified.
    """
    try:
        result = subprocess.run(
            [
                "git",
                "-C",
                str(repo_path),
                "rev-parse",
                "--is-shallow-repository",
            ],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return True
    if result.returncode != 0:
        return True
    return result.stdout.strip() == "true"


def scan_repo_for_secrets(
    repo_path: Path,
    *,
    timeout: int = 300,
) -> list[str]:
    """Scan repository content for well-known credential patterns.

    Iterates over all blob content in the repository using
    ``git log --all -p`` and matches each line against the
    built-in :data:`SECRET_PATTERNS` dictionary.

    The git output is streamed line-by-line rather than buffered
    in full, so repositories with very large histories do not
    require the entire diff to be held in memory at once.

    Args:
        repo_path: Path to the git repository (bare or regular).
        timeout: Timeout in seconds for the git log operation.

    Returns:
        Deduplicated list of discovered credential strings,
        in the order they were first encountered.

    Raises:
        RuntimeError: If the scan cannot complete (git log times
            out or exits non-zero).  Failing closed ensures callers
            never mistake an incomplete scan for a clean repository.
    """
    if not repo_path.exists():
        return []

    cmd = [
        "git",
        "-C",
        str(repo_path),
        "log",
        "--all",
        "--diff-filter=ACMRD",
        "-p",
        # By default ``git log -p`` emits no patch for merge commits,
        # so a secret introduced (or removed) only in a merge's
        # conflict-resolution would be invisible to the scan.
        # ``--diff-merges=first-parent`` makes each merge show its
        # diff against the first parent, surfacing content that the
        # merge brought onto the mainline so it can be redacted.
        "--diff-merges=first-parent",
        "--no-color",
    ]

    seen: set[str] = set()
    discovered: list[str] = []

    try:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
        )
    except OSError as exc:
        # subprocess.Popen raises OSError (e.g. FileNotFoundError when
        # the git binary is missing) before the process even starts.
        # Re-raise as RuntimeError so the function honours its
        # documented fail-closed contract: callers (apply_content_
        # filters) treat RuntimeError as a filtering failure rather
        # than mistaking an unstarted scan for a clean repository.
        raise RuntimeError(
            f"Failed to start git log for secret scan in {repo_path.name}: {exc}"
        ) from exc
    deadline = time.monotonic() + timeout
    timed_out = threading.Event()

    # Drain stderr concurrently in a background thread.  ``git log``
    # can write to stderr (e.g. warnings) while we are still reading
    # stdout; if stderr were left unread until after the stdout loop
    # finished, a child that filled the OS stderr pipe buffer would
    # block on its write, stop producing stdout, and deadlock the
    # scan until the watchdog killed it.  Reading both pipes in
    # parallel keeps the child unblocked.
    stderr_chunks: list[str] = []

    def _drain_stderr() -> None:
        if proc.stderr is not None:
            for err_line in proc.stderr:
                stderr_chunks.append(err_line)

    stderr_thread = threading.Thread(target=_drain_stderr, daemon=True)
    stderr_thread.start()

    def _on_timeout() -> None:
        # Fires unconditionally once ``timeout`` seconds have elapsed
        # since the watchdog started — the Timer is not reset by
        # output activity.  Killing the process unblocks the ``for
        # line in stdout`` iterator, which otherwise only re-checks
        # the deadline when a new line arrives and so could block
        # indefinitely if git stalls without producing output.  A
        # threading.Timer is used instead of select() so the timeout
        # is enforced portably, including on Windows where select()
        # does not support pipe handles.
        timed_out.set()
        proc.kill()

    watchdog = threading.Timer(timeout, _on_timeout)
    watchdog.start()

    # Track position within the ``git log -p`` stream so file-header
    # markers are distinguished structurally rather than by a fragile
    # textual heuristic.  A unified diff file header ("--- a/..." /
    # "+++ b/...") only appears after a "diff --git" line and before
    # the first "@@" hunk of that file; once inside a hunk every
    # "+"/"-"/" " line is content.  This avoids skipping a genuine
    # added/removed line whose own text begins with "++"/"--" (which
    # renders as "+++ "/"--- " once the diff marker is prepended) and
    # also keeps commit metadata / message bodies out of the scan.
    in_hunk = False

    try:
        if proc.stdout is not None:
            for line in proc.stdout:
                # Also re-check the deadline on each line so a scan
                # that keeps producing output but runs long still
                # stops promptly.
                if time.monotonic() > deadline:
                    timed_out.set()
                    proc.kill()
                    break

                # A new commit or a new file resets hunk state; the
                # intervening lines (commit/Author/Date, index/mode
                # lines and the ---/+++ file headers) are never
                # scannable content.
                if line.startswith("commit ") or line.startswith("diff --"):
                    in_hunk = False
                    continue
                if line.startswith("@@"):
                    in_hunk = True
                    continue
                if not in_hunk:
                    continue
                # Inside a hunk: added ("+"), removed ("-") or context
                # (" ").  Anything else (e.g. "\ No newline at end of
                # file") is not content.  The single leading diff
                # marker is stripped before matching.
                if not line or line[0] not in ("+", "-", " "):
                    continue
                stripped = line[1:].rstrip("\n")
                if not stripped:
                    continue

                for pattern_name, pattern in SECRET_PATTERNS.items():
                    for match in pattern.finditer(stripped):
                        matched = match.group(0)
                        if matched not in seen:
                            seen.add(matched)
                            discovered.append(matched)
                            # Log only a truncated SHA-256 digest of
                            # the matched text, never the raw value, to
                            # avoid recording the credential itself and
                            # reduce the leakage risk in the audit trail.
                            digest = hashlib.sha256(
                                matched.encode()
                            ).hexdigest()[:12]
                            logger.info(
                                "Secret scan: found %s pattern "
                                "(sha256:%s) in %s",
                                pattern_name,
                                digest,
                                repo_path.name,
                            )
    finally:
        watchdog.cancel()
        returncode = proc.wait()
        # The stderr drain thread exits once the pipe reaches EOF
        # (which happens when the process terminates).
        stderr_thread.join()
        stderr_output = "".join(stderr_chunks)

    if timed_out.is_set():
        msg = (
            f"Secret scan timed out for "
            f"{repo_path.name} after {timeout}s"
        )
        logger.error(msg)
        raise RuntimeError(msg)

    if returncode != 0:
        msg = (
            f"Secret scan git log failed for "
            f"{repo_path.name}: {stderr_output.strip()}"
        )
        logger.error(msg)
        raise RuntimeError(msg)

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
        regex = pattern[len("regex:") :]
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
            if not regex:
                # A bare ``regex:`` is an empty regex, which matches
                # every path. Combined with ``--invert-paths`` that
                # would wipe the entire repository history. Reject it
                # explicitly, mirroring ``match_file_pattern``.
                logger.warning(
                    "Empty regex pattern (bare 'regex:') ignored"
                )
                continue
            cmd.extend(["--path-regex", regex, "--invert-paths"])
            applied.append(pattern)
        elif any(c in pattern for c in ("*", "?", "[", "]")):
            cmd.extend(["--path-glob", pattern, "--invert-paths"])
            applied.append(pattern)
        elif not pattern:
            # An empty exact path is meaningless; skip it rather than
            # passing an empty ``--path`` to git filter-repo.
            logger.warning("Empty file pattern ignored")
            continue
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


def _list_tree_files(
    repo_path: Path,
    ref: str,
    *,
    timeout: int = 300,
) -> list[str]:
    """List all files in a bare repo at a given ref.

    Args:
        repo_path: Path to the bare git repository.
        ref: Git ref to list files from.
        timeout: Timeout in seconds for the ls-tree operation.

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
            timeout=timeout,
        )
        if result.returncode != 0:
            return []
        return [line for line in result.stdout.strip().splitlines() if line]
    except subprocess.TimeoutExpired:
        logger.warning(
            "git ls-tree timed out for %s (ref %s) after %ds",
            repo_path.name,
            ref,
            timeout,
        )
        return []
    except OSError as exc:
        logger.warning(
            "git ls-tree failed for %s (ref %s): %s",
            repo_path.name,
            ref,
            exc,
        )
        return []


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
        files = _list_tree_files(repo_path, branch, timeout=timeout)
        if not files:
            continue

        # Find files matching any pattern.  ``_matches_for_removal``
        # adds directory-prefix matching for plain path patterns so a
        # pattern like ``.github/workflows`` removes everything under
        # that directory — matching ``git filter-repo``'s ``--path``
        # prefix semantics used by the preferred code path.
        files_to_remove = [
            f
            for f in files
            if any(_matches_for_removal(f, pat) for pat in patterns)
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
                    # ``--force`` is required for non-bare repos: git
                    # otherwise refuses to add a worktree for a branch
                    # that is already checked out in the repo's main
                    # working tree (the common case for a normal
                    # clone), which would make --remove-files a silent
                    # no-op on that branch.  The branch ref is updated
                    # by the commit below regardless of the now-stale
                    # primary checkout.
                    "--force",
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
            # Surface the failure instead of silently skipping the
            # branch: a swallowed worktree error would make
            # --remove-files a no-op for this branch while still
            # reporting overall success.  apply_content_filters
            # treats RuntimeError as a filtering failure.
            raise RuntimeError(
                f"Failed to create worktree for branch '{branch}' in "
                f"{repo_path.name}: {exc.stderr}"
            ) from exc
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
                # git filter-repo format: literal==>replacement
                tmp.write(f"{token}==>{replacement}\n")
                valid_count += 1
                fingerprint = hashlib.sha256(token.encode()).hexdigest()[:12]
                logger.debug(
                    "Token replacement: [sha256:%s] → %s",
                    fingerprint,
                    replacement,
                )
            replacements_file = tmp.name

        if valid_count == 0:
            logger.warning(
                "No valid tokens to replace in %s "
                "(all %d were skipped during validation)",
                repo_path.name,
                len(tokens),
            )
            return True

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
            valid_count,
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
        except (RuntimeError, OSError) as exc:
            # RuntimeError covers the scan/redaction fail-closed
            # paths; OSError (e.g. FileNotFoundError when git is
            # missing) can surface from subprocess.Popen.  Both are
            # reported as filter failures so the (success, error)
            # contract always holds.
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
            entry_fp = hashlib.sha256(
                entry.encode("utf-8")
            ).hexdigest()
            logger.warning(
                "Invalid git-filter spec entry "
                "(no colon). sha256=%s length=%d",
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
