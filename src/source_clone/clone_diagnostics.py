# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""Interpretation of git clone failures.

Turns the combined stderr/stdout of a failed ``git clone`` into an operator
facing explanation, and preserves the raw transcript when SSH debugging is on.
Whether a failure is worth retrying, and how long to wait, lives in
``clone_retry_policy``.

The pattern matching is expressed as an ordered rule table, mirroring
``clone_utils._CLONE_ERROR_DIAGNOSTICS``.  Order is significant: the first entry
that matches wins, so narrow patterns must precede the broad catch-alls that
would otherwise shadow them.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from source_clone.logging import get_logger

if TYPE_CHECKING:
    import subprocess
    from collections.abc import Callable

    from source_clone.models import Config

logger = get_logger(__name__)


def log_ssh_debug_output(
    config: Config, process_result: subprocess.CompletedProcess[str]
) -> None:
    """Log raw git output when SSH debugging is enabled and the clone failed.

    Emitted before the output is condensed into a diagnosis, so that the
    unabridged git/SSH transcript is still available when the pattern matching
    below picks the wrong explanation.

    Args:
        config: Configuration used for the clone
        process_result: Completed subprocess result
    """
    # If SSH debug enabled and clone failed, log raw stderr/stdout (truncated)
    # before analysis
    if not getattr(config, "ssh_debug", False) or process_result.returncode == 0:
        return

    raw_stderr = (process_result.stderr or "").strip()
    raw_stdout = (process_result.stdout or "").strip()
    max_len = 1200
    if len(raw_stderr) > max_len:
        raw_stderr_display = raw_stderr[:max_len] + "...(truncated)"
    else:
        raw_stderr_display = raw_stderr
    if len(raw_stdout) > max_len:
        raw_stdout_display = raw_stdout[:max_len] + "...(truncated)"
    else:
        raw_stdout_display = raw_stdout
    logger.debug(f"[ssh-debug][raw-stderr] {raw_stderr_display}")
    if raw_stdout_display:
        logger.debug(f"[ssh-debug][raw-stdout] {raw_stdout_display}")


@dataclass(frozen=True)
class _CloneFailure:
    """A failed clone invocation handed to the diagnostic builders."""

    output: str
    lowered: str
    exit_code: int
    project_name: str
    config: Config


def _first_line_containing(output: str, needle: str, fallback: str) -> str:
    """Quote the first output line mentioning *needle*.

    Preserves the real path or detail git reported instead of substituting a
    hardcoded placeholder, which matters when diagnosing lock contention.

    Args:
        output: Combined stderr/stdout from the clone
        needle: Lowercase substring identifying the interesting line
        fallback: Message to use when no line matched

    Returns:
        Quoted git error line, or *fallback*
    """
    line = next(
        (line for line in output.split("\n") if needle in line.lower()),
        "",
    )
    if line:
        return f"Git error: {line.strip()}"
    return fallback


def _permission_denied(err: _CloneFailure) -> str:
    ssh_user = getattr(err.config, "ssh_user", "git")
    identity_file = getattr(err.config, "ssh_identity_file", "default")
    return f"Permission denied - SSH auth failed for {ssh_user}@{err.config.host}:{err.config.port} (key: {identity_file}) accessing {err.project_name}"


def _host_key_failure(err: _CloneFailure) -> str:
    return f"Host key verification failed for {err.config.host} - run: ssh-keyscan -p {err.config.port} {err.config.host} >> ~/.ssh/known_hosts"


def _connection_refused(err: _CloneFailure) -> str:
    return f"Connection refused - check if SSH service is running on {err.config.host}:{err.config.port}"


def _dns_failure(err: _CloneFailure) -> str:
    return f"DNS resolution failed - cannot resolve {err.config.host}"


def _repository_missing(err: _CloneFailure) -> str:
    return f"Repository not found: {err.project_name}"


def _config_lock_failure(err: _CloneFailure) -> str:
    return _first_line_containing(
        err.output,
        "could not lock config file",
        "Git error: could not lock config file (path not captured)",
    )


def _fatal_open_failure(err: _CloneFailure) -> str:
    return _first_line_containing(
        err.output,
        "fatal: could not open",
        "Git error: fatal could not open (details not captured)",
    )


def _network_timeout(err: _CloneFailure) -> str:
    return f"Network timeout during clone (timeout: {err.config.clone_timeout}s) - consider increasing --clone-timeout"


def _git_general_error(err: _CloneFailure) -> str:
    # Git error code 128 is general error
    if err.output:
        return f"Git error: {err.output[:200]}..."
    return f"Git error (exit code {err.exit_code})"


def _summarize_output(err: _CloneFailure) -> str:
    # Try to find the most informative line (error/fatal/warning)
    important_line = None
    for line in err.output.split("\n"):
        if any(
            keyword in line.lower()
            for keyword in ["error:", "fatal:", "warning:", "failed"]
        ):
            important_line = line.strip()
            break

    if important_line:
        return f"Clone failed (exit code {err.exit_code}): {important_line}"
    return f"Clone failed (exit code {err.exit_code}): {err.output[:150]}..."


# Ordered most-specific first; the first matching entry wins.  Note the mix of
# case-sensitive checks against `output` and case-insensitive checks against
# `lowered` — both are reproduced exactly as the original chain expressed them.
_CLONE_FAILURE_DIAGNOSTICS: tuple[
    tuple[Callable[[_CloneFailure], bool], Callable[[_CloneFailure], str]], ...
] = (
    (lambda err: "Permission denied" in err.output, _permission_denied),
    (lambda err: "Host key verification failed" in err.output, _host_key_failure),
    (lambda err: "Connection refused" in err.output, _connection_refused),
    (lambda err: "could not resolve hostname" in err.lowered, _dns_failure),
    (
        lambda err: "Repository not found" in err.output or "not found" in err.lowered,
        _repository_missing,
    ),
    (lambda err: "could not lock config file" in err.lowered, _config_lock_failure),
    (
        lambda err: "could not open" in err.lowered and "fatal:" in err.lowered,
        _fatal_open_failure,
    ),
    (
        lambda err: "timeout" in err.lowered or "timed out" in err.lowered,
        _network_timeout,
    ),
    (
        lambda err: "too many open files" in err.lowered,
        lambda _err: (
            "Resource exhaustion: too many open files - reduce --threads or increase system limits"
        ),
    ),
    (
        lambda err: "no space left" in err.lowered,
        lambda _err: "Disk space exhausted - check available disk space",
    ),
    (
        lambda err: "connection reset" in err.lowered,
        lambda _err: (
            "Network connection reset - possible network instability or rate limiting"
        ),
    ),
    # "early EOF" is tested against the lowercased output, so this entry never
    # fires in practice. Preserved verbatim: dropping it would hand such
    # failures to a different entry and change the reported message.
    (
        lambda err: "early EOF" in err.lowered,
        lambda _err: "Connection terminated unexpectedly",
    ),
    (
        lambda err: "remote end hung up" in err.lowered,
        lambda _err: "Remote server disconnected",
    ),
    (lambda err: err.exit_code == 128, _git_general_error),
    (lambda err: bool(err.output), _summarize_output),
)


def analyze_clone_error(
    process_result: subprocess.CompletedProcess[str], project_name: str, config: Config
) -> str:
    """Analyze clone error and return descriptive message.

    Args:
        process_result: Completed subprocess result
        project_name: Name of project that failed
        config: Configuration used for the clone

    Returns:
        Descriptive error message
    """
    stderr = process_result.stderr.strip()
    stdout = process_result.stdout.strip()
    exit_code = process_result.returncode

    # Combine stderr and stdout for analysis
    error_output = f"{stderr}\n{stdout}".strip()

    err = _CloneFailure(
        output=error_output,
        lowered=error_output.lower(),
        exit_code=exit_code,
        project_name=project_name,
        config=config,
    )
    for matches, build in _CLONE_FAILURE_DIAGNOSTICS:
        if matches(err):
            return build(err)

    return f"Clone failed with exit code {exit_code}"
