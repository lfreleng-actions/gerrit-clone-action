# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""Git execution environment and remote handling for refresh operations.

Bottom layer of the :class:`~source_clone.refresh_worker.RefreshWorker` mixin
stack. It answers related questions about *how* we talk to a remote:
which URL is configured, whether the repository came from Gerrit, whether
that URL implies an SSH handshake (and so needs pacing), and what
environment git subprocesses should run with.

Every higher layer that performs a network operation depends on the handshake
jitter provided here, and launches its git commands through :func:`run_git`.
"""

from __future__ import annotations

import json
import os
import random
import threading
import time
from typing import TYPE_CHECKING

from source_clone.logging import get_logger
from source_clone.model_enums import SourceType
from source_clone.subprocess_tracking import (
    ProcessAbandonedError,
    batch_abandoned,
    run_tracked,
)
from source_clone.url_credentials import remote_location

if TYPE_CHECKING:
    import subprocess
    from collections.abc import Mapping
    from pathlib import Path

    from source_clone.models import Config

logger = get_logger(__name__)

# Maximum random delay (seconds) inserted before each SSH-backed git network
# operation. Spreading handshakes across a small window de-synchronises worker
# threads so we do not open many simultaneous SSH connections to Gerrit, which
# is a common cause of transient "Could not read from remote repository"
# throttling failures.
SSH_HANDSHAKE_JITTER_SECONDS = 0.25

#: The manifest ``clone`` writes at the root of a tree by default.  A
#: tree cloned with another ``--manifest-filename`` has none ``refresh``
#: can find, and is classified by its remotes' hosts alone.
CLONE_MANIFEST_FILENAME = "clone-manifest.json"

#: Hosts that never serve Gerrit; any ``*.github.com`` host too.
_NON_GERRIT_HOSTS = frozenset({"github.com", "gitlab.com", "bitbucket.org"})

#: Gerrit's default SSH port.
GERRIT_SSH_PORT = 29418

_SSH_SCHEMES = frozenset({"ssh", "git+ssh", "ssh+git"})
_HTTP_SCHEMES = frozenset({"http", "https"})

#: A host label that is one of these, or starts with one and a dash,
#: names a Gerrit host: ``gerrit.onap.org``, ``review.opendev.org``.
_GERRIT_HOST_LABELS = ("gerrit", "review")

#: The first path segment under which Gerrit serves HTTP(S) clones.
_GERRIT_PATH_PREFIXES = frozenset({"r", "gerrit"})


def is_gerrit_url(remote_url: str | None) -> bool:
    """Whether *remote_url*'s host, port or path prefix marks it as Gerrit.

    Only the host, the SSH port and an HTTP(S) URL's first path segment
    count; a repository's name, or anything later in its path, never
    does.  GitHub, GitLab and Bitbucket hosts are never Gerrit.

    Args:
        remote_url: Remote URL to check

    Returns:
        True if the URL points at a Gerrit server
    """
    location = remote_location(remote_url) if remote_url else None
    if location is None or not location.host:
        return False
    host = location.host
    if host in _NON_GERRIT_HOSTS or host.endswith(".github.com"):
        return False
    if location.scheme in _SSH_SCHEMES and location.port == GERRIT_SSH_PORT:
        return True
    if any(
        label in _GERRIT_HOST_LABELS
        or label.startswith(tuple(f"{name}-" for name in _GERRIT_HOST_LABELS))
        for label in host.split(".")
    ):
        return True
    if location.scheme in _HTTP_SCHEMES:
        segments = [segment for segment in location.path.split("/") if segment]
        return bool(segments) and segments[0] in _GERRIT_PATH_PREFIXES
    return False


def _read_clone_manifest(path: Path) -> SourceType | None:
    """The source type the clone manifest at *path* records.

    Args:
        path: The manifest file

    Returns:
        The recorded source type, or None if the manifest cannot be read
        or parsed, or records none this tool recognises.
    """
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        logger.debug(f"Ignoring unreadable clone manifest {path}: {e}")
        return None
    source = data.get("source_type") if isinstance(data, dict) else None
    if isinstance(source, str) and source in {member.value for member in SourceType}:
        return SourceType(source)
    logger.debug(f"Ignoring clone manifest {path}: no recognised source_type")
    return None


def _find_clone_manifest(directory: Path) -> tuple[bool, SourceType | None]:
    """Whether *directory* holds a clone manifest, and what it records.

    Args:
        directory: Directory to look in

    Returns:
        ``(True, source type or None)`` for a manifest, readable or not;
        ``(False, None)`` when there is none.
    """
    manifest = directory / CLONE_MANIFEST_FILENAME
    try:
        present = manifest.exists()
    except OSError as e:
        logger.debug(f"Ignoring clone manifest {manifest} that cannot be read: {e}")
        return True, None
    return (True, _read_clone_manifest(manifest)) if present else (False, None)


class CloneManifestSources:
    """What the clone manifests above repositories record, read once each.

    One instance serves a whole run, from every worker thread, so a tree
    of thousands of repositories reads each manifest, and looks for one
    in each directory, once.
    """

    def __init__(self) -> None:
        """Start with nothing looked up."""
        self._lock = threading.Lock()
        # Each directory looked in: whether it holds a manifest, and the
        # source type that manifest records, if any.
        self._directories: dict[Path, tuple[bool, SourceType | None]] = {}

    def source_type(self, repo_path: Path) -> SourceType | None:
        """The source type the nearest clone manifest above *repo_path* records.

        The repository's parent directory is searched first, then each
        ancestor.  The first manifest found decides: one that cannot be
        read, or records no recognised source type, gives no answer
        rather than deferring to one further up.

        Args:
            repo_path: Repository path

        Returns:
            The source type recorded, or None if there is no answer.
        """
        with self._lock:
            for directory in repo_path.resolve().parents:
                found = self._directories.get(directory)
                if found is None:
                    found = _find_clone_manifest(directory)
                    self._directories[directory] = found
                has_manifest, source = found
                if has_manifest:
                    return source
        return None


def run_git(
    cmd: list[str],
    repo_path: Path,
    *,
    timeout: float,
    env: Mapping[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run a refresh git command as a tracked child of the calling batch.

    Tracked so that a refresh pool abandoned on Ctrl+C or SIGTERM can
    refuse the command, or stop it together with the ssh helpers git
    spawned, instead of leaving a fetch updating refs after the command
    has returned.

    A child the batch terminated exits nonzero exactly like git failing
    on its own, and callers would report or retry it as such.  Only the
    tracker can tell the two apart, so that case is raised instead.

    Args:
        cmd: Git command to run.
        repo_path: Repository to run it in.
        timeout: Seconds to wait before killing the child and raising.
        env: Environment for the child; inherited when omitted.

    Returns:
        The completed process, with text stdout and stderr.

    Raises:
        ProcessAbandonedError: If the calling thread's batch was abandoned
            before or while the command ran.
        subprocess.TimeoutExpired: If *timeout* elapses.
    """
    result = run_tracked(cmd, cwd=repo_path, timeout=timeout, env=env)
    if result.returncode != 0 and batch_abandoned():
        raise ProcessAbandonedError(f"{' '.join(cmd[:2])} in {repo_path} was abandoned")
    return result


class GitEnvironmentMixin:
    """Remote-URL inspection and git subprocess environment construction."""

    # Supplied by RefreshWorker.__init__; declared here because this layer
    # reads them.
    config: Config | None
    ssh_jitter_seconds: float
    clone_manifests: CloneManifestSources

    def _remote_urls(self, repo_path: Path) -> list[tuple[str, str]]:
        """List the URL of every configured remote, in configuration order.

        Args:
            repo_path: Repository path

        Returns:
            ``(remote name, URL)`` pairs; empty if there are no remotes or
            the configuration could not be read.

        Raises:
            ProcessAbandonedError: If the batch was abandoned.  Answering
                "no remotes" instead would misreport the repository.
        """
        try:
            result = run_git(
                ["git", "config", "--get-regexp", r"^remote\..*\.url$"],
                repo_path,
                timeout=5,
            )
        except ProcessAbandonedError:
            raise
        except Exception as e:
            logger.debug(f"Failed to read remote URLs: {e}")
            return []
        if result.returncode != 0:
            return []
        remotes = []
        for line in result.stdout.splitlines():
            key, _, url = line.partition(" ")
            # Remote names may themselves contain dots, so the name is
            # whatever lies between the fixed prefix and suffix.
            if url.strip():
                remotes.append((key[len("remote.") : -len(".url")], url.strip()))
        return remotes

    def _get_remote_url(self, repo_path: Path) -> str | None:
        """Get the remote URL to report for the repository.

        Args:
            repo_path: Repository path

        Returns:
            The ``origin`` URL if that remote exists, else the first
            remote's URL, or None if there are no remotes.
        """
        remotes = self._remote_urls(repo_path)
        origin = [url for name, url in remotes if name == "origin"]
        if origin:
            # git config --get reports the last of a multi-valued key.
            return origin[-1]
        return remotes[0][1] if remotes else None

    def _has_gerrit_remote(self, repo_path: Path) -> bool:
        """Whether the repository came from Gerrit.

        The clone manifest nearest above the repository decides, when
        one records a source type: ``clone`` knew where it cloned from.
        Otherwise any remote on a Gerrit host makes the repository
        Gerrit.  The refresh fetch is ``--all``, so a repository whose
        Gerrit remote is not called ``origin`` -- ``git clone --mirror
        --origin upstream`` -- is still refreshed from Gerrit.

        Args:
            repo_path: Repository path

        Returns:
            True if the repository came from Gerrit
        """
        source = self.clone_manifests.source_type(repo_path)
        if source is not None:
            logger.debug(
                f"{repo_path}: clone manifest records source type {source.value}"
            )
            return source == SourceType.GERRIT
        return any(
            self._is_gerrit_repository(url) for _, url in self._remote_urls(repo_path)
        )

    @staticmethod
    def _is_gerrit_repository(remote_url: str | None) -> bool:
        """Check if a remote URL points at a Gerrit server.

        See :func:`is_gerrit_url`.

        Args:
            remote_url: Remote URL to check

        Returns:
            True if the URL points at a Gerrit server
        """
        return is_gerrit_url(remote_url)

    @staticmethod
    def _remote_uses_ssh(remote_url: str | None) -> bool:
        """Return True if the origin remote performs an SSH handshake.

        Only SSH-backed remotes benefit from handshake jitter. HTTP(S), the
        anonymous git protocol, ``file://`` URLs and local filesystem paths
        never open an SSH connection, so jittering them just adds latency. An
        unknown/empty remote is treated as SSH so the throttling protection is
        preserved when the URL cannot be read.

        Args:
            remote_url: The origin remote URL, or None if it is unknown.

        Returns:
            True if a handshake (and therefore jitter) is warranted.
        """
        if not remote_url:
            return True
        url = remote_url.strip()
        lowered = url.lower()
        if lowered.startswith("ssh://"):
            return True
        # Non-SSH transports and local paths never open an SSH handshake.
        if lowered.startswith(("http://", "https://", "git://", "file://")):
            return False
        if url.startswith(("/", "./", "../", "~")):
            return False
        # scp-like syntax (``[user@]host:path``) is SSH. git only recognises it
        # when a colon appears before the first slash; a colon after a slash
        # (or no colon at all) denotes a local filesystem path.
        colon = url.find(":")
        slash = url.find("/")
        return colon != -1 and (slash == -1 or colon < slash)

    def _ssh_handshake_jitter(self, repo_path: Path) -> None:
        """Sleep a small random interval before an SSH-backed git operation.

        De-synchronises concurrent worker threads so we avoid opening many
        simultaneous SSH connections to Gerrit, which is a common cause of
        transient "Could not read from remote repository" throttling. The
        sleep is skipped for HTTP(S)/git-protocol remotes, which perform no
        SSH handshake and so gain nothing from jitter.

        Args:
            repo_path: Repository whose origin remote is about to be contacted.
        """
        if self.ssh_jitter_seconds <= 0:
            return
        if not self._remote_uses_ssh(self._get_remote_url(repo_path)):
            return
        time.sleep(random.uniform(0, self.ssh_jitter_seconds))

    def _build_git_environment(self) -> dict[str, str]:
        """Build environment for Git operations.

        Returns:
            Environment dictionary
        """
        env = os.environ.copy()

        # Add Git SSH command if config is provided, otherwise use safe defaults
        if self.config and self.config.git_ssh_command:
            env["GIT_SSH_COMMAND"] = self.config.git_ssh_command
        else:
            # SSH Configuration Trade-offs:
            #
            # We explicitly disable SSH multiplexing (ControlMaster=no) for thread safety.
            # This prevents race conditions when multiple threads connect to the same host
            # simultaneously, which can cause:
            # - Socket file conflicts in ~/.ssh/
            # - Connection hangs or failures
            # - Unpredictable behavior in parallel operations
            #
            # PERFORMANCE TRADE-OFF:
            # Disabling multiplexing means each git operation requires a new SSH handshake,
            # adding ~100-500ms latency per operation. However, in practice:
            # - Most operations are I/O bound (git fetch/pull), not connection-bound
            # - Parallel execution across multiple repos still provides significant speedup
            # - The reliability gain outweighs the connection overhead
            # - Real-world testing shows acceptable performance for typical use cases
            #
            # Alternative approaches considered:
            # - Connection pooling: Complex to implement, would require shared state
            # - Single-threaded SSH: Eliminates parallelism benefits entirely
            # - Master socket per thread: Still has filesystem race conditions
            #
            # Current configuration prioritizes reliability and thread safety over
            # optimal SSH connection reuse. If performance becomes an issue, consider:
            # - Using HTTPS instead of SSH (no connection multiplexing issues)
            # - Increasing thread count to compensate for per-connection overhead
            # - Custom connection pooling implementation (significant complexity)
            ssh_opts = [
                "ssh",
                "-o",
                "BatchMode=yes",
                "-o",
                "ControlMaster=no",  # Disable multiplexing for thread safety
                "-o",
                "ConnectTimeout=10",
                "-o",
                "ServerAliveInterval=5",
                "-o",
                "ServerAliveCountMax=3",
                "-o",
                "ConnectionAttempts=2",
                "-o",
                "StrictHostKeyChecking=accept-new",
            ]
            env["GIT_SSH_COMMAND"] = " ".join(ssh_opts)

        # Disable terminal prompts
        env["GIT_TERMINAL_PROMPT"] = "0"

        return env
