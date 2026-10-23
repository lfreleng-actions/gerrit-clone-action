# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""Secret scanning for repository history.

Owns the registry of well-known credential formats, the shallow-clone
guard used to fail closed before history-dependent filtering, and the
streaming ``git log -p`` scan that discovers credential strings so they
can be redacted by the content filter.
"""

from __future__ import annotations

import contextlib
import hashlib
import re
import subprocess
import time
from typing import TYPE_CHECKING

from source_clone.content_git import raise_if_abandoned, run_content_git
from source_clone.content_scan_process import run_bounded, stop_scan
from source_clone.logging import get_logger
from source_clone.subprocess_streaming import popen_tracked

if TYPE_CHECKING:
    import threading
    from pathlib import Path

logger = get_logger(__name__)


#: Compiled regex patterns for well-known credential formats.
#: Each pattern is designed to match the token value itself (no
#: surrounding context required) so it can be used as a literal
#: replacement target for ``git filter-repo --replace-text``.
#:
#: The registry holds only public, well-known token *format*
#: expressions (never actual credential values), so both the
#: pattern names and the regexes themselves are safe to log.
SCAN_PATTERNS: dict[str, re.Pattern[str]] = {
    # GitLab Personal Access Tokens (glpat-XXXX...)
    "gitlab_pat": re.compile(r"glpat-[A-Za-z0-9_\-]{20,}"),
    # GitHub classic Personal Access Tokens (ghp_XXXX...)
    "github_pat_classic": re.compile(r"ghp_[A-Za-z0-9]{36,}"),
    # GitHub fine-grained Personal Access Tokens
    "github_pat_fine_grained": re.compile(r"github_pat_[A-Za-z0-9_]{22,}"),
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
    "stripe_api_key": re.compile(r"(?:sk|pk)_(?:live|test)_[A-Za-z0-9]{20,}"),
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
    "mailchimp_api_key": re.compile(r"[0-9a-f]{32}-us[0-9]{1,2}"),
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
    full history could not be verified.  An abandoned batch is not an
    undetermined answer, though: it raises
    :class:`~source_clone.subprocess_tracking.ProcessAbandonedError`.
    """
    try:
        result = run_content_git(
            ["git", "-C", str(repo_path), "rev-parse", "--is-shallow-repository"],
            timeout=timeout,
        )
    except (OSError, subprocess.SubprocessError):
        return True
    if result.returncode != 0:
        return True
    return result.stdout.strip() == "true"


def _build_scan_command(repo_path: Path) -> list[str]:
    """Build the ``git log`` command used to stream repository content."""
    return [
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


def _start_scan_process(
    stack: contextlib.ExitStack[bool | None], cmd: list[str], repo_path: Path
) -> subprocess.Popen[str]:
    """Start the streaming ``git log`` process for a secret scan.

    The child stays tracked until *stack* closes, so an abandoned batch
    can stop it mid-scan, and closing *stack* stops anything it left in
    its process group.

    Raises:
        RuntimeError: If the process cannot be started at all.
        ProcessAbandonedError: If the batch was abandoned before it
            started.
    """
    try:
        return stack.enter_context(popen_tracked(cmd))
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


def _collect_line_secrets(
    stripped: str,
    repo_name: str,
    seen: set[str],
    discovered: list[str],
) -> None:
    """Record every new credential match found in *stripped*."""
    for pattern_name, pattern in SCAN_PATTERNS.items():
        for match in pattern.finditer(stripped):
            matched = match.group(0)
            if matched in seen:
                continue
            seen.add(matched)
            discovered.append(matched)
            # Log only a truncated SHA-256 digest of the matched
            # text, never the raw value, to avoid recording the
            # credential itself and reduce the leakage risk in the
            # audit trail.
            digest = hashlib.sha256(matched.encode()).hexdigest()[:12]
            logger.info(
                "Secret scan: found %s pattern (sha256:%s) in %s",
                pattern_name,
                digest,
                repo_name,
            )


def _scannable_content(line: str, in_hunk: bool) -> str | None:
    """Return the scannable payload of a ``git log -p`` *line*.

    Returns ``None`` when the line carries no scannable content.  Only
    called for lines already known to be inside a diff hunk.
    """
    if not in_hunk:
        return None
    # Inside a hunk: added ("+"), removed ("-") or context (" ").
    # Anything else (e.g. "\ No newline at end of file") is not
    # content.  The single leading diff marker is stripped before
    # matching.
    if not line or line[0] not in ("+", "-", " "):
        return None
    stripped = line[1:].rstrip("\n")
    return stripped or None


def _consume_scan_stream(
    proc: subprocess.Popen[str],
    deadline: float,
    timed_out: threading.Event,
    repo_name: str,
) -> list[str]:
    """Stream *proc*'s stdout and collect discovered credentials.

    Returns the deduplicated credentials in first-encounter order.
    """
    seen: set[str] = set()
    discovered: list[str] = []
    if proc.stdout is None:
        return discovered

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

    for line in proc.stdout:
        # Also re-check the deadline on each line so a scan that keeps
        # producing output but runs long still stops promptly.
        if time.monotonic() > deadline:
            timed_out.set()
            stop_scan(proc)
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

        stripped = _scannable_content(line, in_hunk)
        if stripped is None:
            continue
        _collect_line_secrets(stripped, repo_name, seen, discovered)

    return discovered


def scan_repo_for_secrets(
    repo_path: Path,
    *,
    timeout: int = 300,
) -> list[str]:
    """Scan repository content for well-known credential patterns.

    Iterates over all blob content in the repository using
    ``git log --all -p`` and matches each line against the
    built-in :data:`SCAN_PATTERNS` dictionary.

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
        ProcessAbandonedError: If the batch was abandoned before or
            during the scan.
    """
    if not repo_path.exists():
        return []

    cmd = _build_scan_command(repo_path)
    with contextlib.ExitStack() as stack:
        proc = _start_scan_process(stack, cmd, repo_path)
        deadline = time.monotonic() + timeout
        run = run_bounded(
            proc,
            lambda timed_out: _consume_scan_stream(
                proc, deadline, timed_out, repo_path.name
            ),
            timeout,
        )

    # Raised only now, the block left normally: its cleanup on an
    # exception would close pipes a stuck reader may still hold.
    if run.error is not None:
        raise run.error
    # A git that outlived its stop has no exit status; it counts as one
    # that failed.  The abandon is checked first: a scan the batch cut
    # short has not read the whole history, and must pass for neither a
    # clean repository nor git failing.
    returncode = -1 if run.returncode is None else run.returncode
    raise_if_abandoned(returncode, cmd)
    if run.returncode is None:
        msg = f"Secret scan for {repo_path.name} could not be stopped; gave up on it"
    elif run.timed_out:
        msg = f"Secret scan timed out for {repo_path.name} after {timeout}s"
    elif returncode != 0:
        msg = f"Secret scan git log failed for {repo_path.name}: {run.stderr.strip()}"
    else:
        msg = ""
    if msg:
        logger.error(msg)
        raise RuntimeError(msg)

    discovered = run.discovered
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
