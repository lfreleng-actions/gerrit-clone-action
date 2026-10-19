# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""Interpretation of git command output for refresh operations.

This module owns everything that turns raw git stdout/stderr into a decision:
the refresh exception taxonomy, the pattern tables used to classify a failure,
and the small parsers that count what a successful fetch/pull actually did.

It performs no I/O of its own, which keeps the classification rules (whose
ordering is load-bearing, see ``_GIT_ERROR_CLASSIFICATIONS``) reviewable in
isolation from the subprocess plumbing that produces the output.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import subprocess

# Transient SSH / network failures that are safe to retry. Gerrit prints some of
# these (notably "could not read from remote repository") for genuine
# authentication failures too, so callers MUST check for authentication markers
# (see ``_AUTH_ERROR_PATTERNS``) BEFORE treating an error as transient.
_TRANSIENT_GIT_ERROR_PATTERNS = (
    "could not read from remote repository",
    "early eof",
    "the remote end hung up unexpectedly",
    "kex_exchange_identification",
    "ssh_exchange_identification",
    "connection closed by remote host",
    "connection reset by peer",
    "connection reset",
    "broken pipe",
)

# Markers that unambiguously indicate an authentication / authorization failure.
_AUTH_ERROR_PATTERNS = (
    "permission denied",
    "publickey",
    "authentication failed",
    "access denied",
)

# Markers indicating the remote repository does not exist (or is not visible as
# a project). Gerrit and GitHub also print the generic "could not read from
# remote repository" line for a missing repo, so callers MUST check these
# BEFORE the transient patterns to avoid misclassifying a permanently missing
# repository as a retryable network blip.
_NOT_FOUND_GIT_ERROR_PATTERNS = (
    "repository not found",
    "does not exist",
)

# Markers indicating the local branch has diverged from upstream and a
# fast-forward-only update is not possible (i.e. local commits exist).
_DIVERGED_BRANCH_PATTERNS = (
    "diverging branches",
    "not possible to fast-forward",
    "can't be fast-forwarded",
    "cannot be fast-forwarded",
)

# Network-level failures (DNS resolution, TCP connect) which are unrelated to
# SSH handshakes but are equally retryable.
_NETWORK_ERROR_PATTERNS = (
    "could not resolve host",
    "failed to connect",
    "connection timed out",
    "connection refused",
)

# Ordered git-error classification table consumed by ``_analyze_git_error``.
# The ORDER IS LOAD-BEARING: each entry's note explains why it must be matched
# before the ones below it. The first matching entry wins.
_GIT_ERROR_CLASSIFICATIONS: tuple[tuple[tuple[str, ...], str], ...] = (
    # Network errors.
    (_NETWORK_ERROR_PATTERNS, "Network error during {operation}"),
    # Authentication errors. Checked BEFORE transient SSH errors because
    # Gerrit prints a generic "Could not read from remote repository" line
    # for auth failures too; the "permission denied"/"publickey" markers
    # disambiguate a real auth failure from transient throttling.
    (_AUTH_ERROR_PATTERNS, "Authentication error during {operation}"),
    # Repository not found. Checked BEFORE the transient SSH patterns
    # because a missing repository also produces the generic "could not
    # read from remote repository" line; without this ordering a
    # permanently missing repo would be misreported as a transient network
    # error and needlessly retried.
    (_NOT_FOUND_GIT_ERROR_PATTERNS, "Repository not found during {operation}"),
    # Transient SSH / connection failures (e.g. Gerrit throttling a burst of
    # concurrent connections). Reported as a network error so the retry
    # logic treats them as retryable.
    (_TRANSIENT_GIT_ERROR_PATTERNS, "Network error during {operation}"),
    # Diverging branches: local commits prevent a fast-forward-only update.
    # git's wording ("Diverging branches can't be fast-forwarded") does not
    # contain "non-fast-forward", so it must be matched explicitly.
    (
        _DIVERGED_BRANCH_PATTERNS,
        "Diverging branches during {operation}: local commits differ "
        "from upstream; use --force-hard to reset to the remote",
    ),
    # Merge conflicts.
    (("conflict",), "Merge conflicts during {operation}"),
    # Non-fast-forward.
    (
        ("non-fast-forward", "rejected"),
        "Non-fast-forward update rejected during {operation}",
    ),
)


class RefreshError(Exception):
    """Base exception for refresh operations."""


class RefreshTimeoutError(RefreshError):
    """Raised when refresh operation times out."""


class RefreshAuthError(RefreshError):
    """Raised when a refresh fails with an authentication-style error.

    Kept distinct from :class:`RefreshError` so the retry loop can apply a
    small, dedicated retry budget: a throttled Gerrit may reject a valid key
    with "Permission denied (publickey)" while dropping a connection, which a
    couple of retries recover, whereas a genuine auth misconfiguration should
    fail without consuming the full network-retry budget.
    """


class GitOutputAnalysisMixin:
    """Classification of git command output.

    Provides the failure-classification and change-counting helpers used by the
    refresh worker. Stateless: every method derives its answer purely from the
    output it is handed.
    """

    def _analyze_git_error(
        self, process_result: subprocess.CompletedProcess[str], operation: str
    ) -> str:
        """Analyze Git error output and generate meaningful error message.

        Args:
            process_result: Completed process result
            operation: Git operation name (fetch/pull)

        Returns:
            Error message string
        """
        stderr = process_result.stderr.lower()
        stdout = process_result.stdout.lower()
        combined = stderr + stdout

        for patterns, template in _GIT_ERROR_CLASSIFICATIONS:
            if any(phrase in combined for phrase in patterns):
                return template.format(operation=operation)

        # Generic error
        error_output = process_result.stderr.strip() or process_result.stdout.strip()
        if error_output:
            # Take first line of error
            first_line = error_output.split("\n")[0]
            return f"Git {operation} failed: {first_line}"

        return f"Git {operation} failed with exit code {process_result.returncode}"

    def _is_auth_git_error(
        self, process_result: subprocess.CompletedProcess[str]
    ) -> bool:
        """Determine if a failed Git result looks like an authentication error.

        A throttled Gerrit can surface a transient connection-limit drop as a
        "Permission denied (publickey)" rejection, so auth-classified errors are
        retried a small, bounded number of times (see
        ``_MAX_AUTH_RETRY_ATTEMPTS``) rather than treated as immediately fatal.

        Args:
            process_result: Completed process result

        Returns:
            True if the failure carries authentication markers
        """
        combined = (process_result.stderr + process_result.stdout).lower()

        # Missing repositories are never auth errors. Check first so a
        # permanently absent repo is not misclassified as a retryable auth
        # throttle.
        if any(pattern in combined for pattern in _NOT_FOUND_GIT_ERROR_PATTERNS):
            return False

        return any(pattern in combined for pattern in _AUTH_ERROR_PATTERNS)

    def _raise_for_retryable_git_error(
        self, process_result: subprocess.CompletedProcess[str], error_msg: str
    ) -> None:
        """Raise the appropriate retryable error for a failed Git result.

        Raises :class:`RefreshAuthError` for auth-style failures (which get a
        small, bounded retry budget) and :class:`RefreshError` for transient
        network/SSH failures (full retry budget). Returns normally when the
        failure is not retryable, letting the caller treat it as a hard
        failure.

        Args:
            process_result: Completed process result
            error_msg: Human-readable error message for the raised exception

        Raises:
            RefreshAuthError: If the failure looks like an auth error
            RefreshError: If the failure is a retryable transient error
        """
        if self._is_auth_git_error(process_result):
            raise RefreshAuthError(error_msg)
        if self._is_retryable_git_error(process_result):
            raise RefreshError(error_msg)

    def _is_retryable_git_error(
        self, process_result: subprocess.CompletedProcess[str]
    ) -> bool:
        """Determine if a Git error is retryable.

        Args:
            process_result: Completed process result

        Returns:
            True if error is retryable
        """
        stderr = process_result.stderr.lower()
        stdout = process_result.stdout.lower()
        combined = stderr + stdout

        # Authentication / authorization failures are never retryable. Check
        # these FIRST: Gerrit prints a generic "could not read from remote
        # repository" line (which also appears for transient throttling) on real
        # auth failures, so the "permission denied"/"publickey" markers are what
        # distinguish them and must take precedence.
        if any(pattern in combined for pattern in _AUTH_ERROR_PATTERNS):
            return False

        # Missing repositories are never retryable. Check BEFORE the transient
        # patterns: Gerrit/GitHub also print "could not read from remote
        # repository" when a project does not exist, so without this ordering a
        # permanently missing repo would match a transient pattern and be
        # retried pointlessly.
        if any(pattern in combined for pattern in _NOT_FOUND_GIT_ERROR_PATTERNS):
            return False

        # Retryable: network and transient SSH handshake failures. The transient
        # SSH patterns (e.g. "could not read from remote repository", "early
        # EOF", "kex_exchange_identification") cover Gerrit throttling a burst of
        # concurrent connections, which succeeds on retry.
        retryable_patterns = [
            *_NETWORK_ERROR_PATTERNS,
            "temporary failure",
            "try again",
            *_TRANSIENT_GIT_ERROR_PATTERNS,
        ]

        for pattern in retryable_patterns:
            if pattern in combined:
                return True

        # Non-retryable: conflicts, divergence, etc. (missing repositories are
        # handled earlier, before the transient-pattern check).
        non_retryable_patterns = [
            "authentication failed",
            "conflict",
            "non-fast-forward",
            "rejected",
            *_DIVERGED_BRANCH_PATTERNS,
        ]

        for pattern in non_retryable_patterns:
            if pattern in combined:
                return False

        # Default: do not retry on unknown errors (conservative approach)
        # Only retry on explicitly recognized transient errors
        return False

    def _is_retryable_error(self, error_msg: str) -> bool:
        """Determine if an error message indicates a retryable error.

        Args:
            error_msg: Error message

        Returns:
            True if error is retryable
        """
        error_lower = error_msg.lower()

        retryable_patterns = [
            "network error",
            "timeout",
            "connection",
            "temporary",
        ]

        return any(pattern in error_lower for pattern in retryable_patterns)

    def _count_pulled_commits(self, output: str) -> int:
        """Count commits pulled from output.

        Note: This is an approximation based on git pull output.
        Returns number of repositories that received commits, not total commit count.
        Actual commit counting would require additional git commands.

        Args:
            output: Git pull output

        Returns:
            1 if commits were pulled, 0 otherwise (repository count, not commit count)
        """
        # Look for patterns like:
        # "Updating abc123..def456"
        # "Fast-forward"
        # "1 file changed, 2 insertions(+), 3 deletions(-)"  # noqa: ERA001

        if "Already up to date" in output or "Already up-to-date" in output:
            return 0

        # Try to find commit range
        match = re.search(r"Updating\s+([0-9a-f]+)\.\.([0-9a-f]+)", output)
        if match:
            # Indicates at least one commit was pulled
            # (Actual count would require: git rev-list --count old..new)
            return 1

        # Look for "Fast-forward" or merge commit messages
        if "Fast-forward" in output or "Merge made" in output:
            return 1

        return 0

    def _count_fetched_commits(self, output: str) -> int:
        """Count commits fetched from output.

        Args:
            output: Git fetch output

        Returns:
            Number of commits fetched (approximate)
        """
        # Git fetch output shows updated refs
        # Count lines with "->" indicating ref updates
        count = len(re.findall(r"->\s+\S+", output))
        return count if count > 0 else 0

    def _count_changed_files(self, output: str) -> int:
        """Count changed files from output.

        Args:
            output: Git pull output

        Returns:
            Number of files changed
        """
        # Look for pattern like "1 file changed" or "2 files changed"
        match = re.search(r"(\d+)\s+files?\s+changed", output)
        if match:
            return int(match.group(1))

        return 0
