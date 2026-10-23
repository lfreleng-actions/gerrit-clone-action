# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2025 Matthew Watkins <mwatkins@linuxfoundation.org>

"""Data models for Gerrit clone operations.

Defines :class:`Config`, the central configuration object, and re-exports the
enumerations, value objects, filtering helpers and result records that live in
the sibling model modules, so ``source_clone.models`` stays the single place
the rest of the package needs to reference for model types.
"""

from __future__ import annotations

import os
import platform
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from source_clone.model_clone_results import BatchResult, CloneResult
from source_clone.model_config_validation import validate_and_normalize
from source_clone.model_enums import (
    CloneStatus,
    DiscoveryMethod,
    ProjectState,
    RefreshStatus,
    SourceType,
)
from source_clone.model_project import Project, RetryPolicy
from source_clone.model_project_filters import (
    filter_projects,
    match_project_pattern,
    normalize_project_list,
)
from source_clone.model_refresh_results import RefreshBatchResult, RefreshResult

if TYPE_CHECKING:
    from source_clone.content_spec import ContentFilterSpec

__all__ = [
    "BatchResult",
    "CloneResult",
    "CloneStatus",
    "Config",
    "DiscoveryMethod",
    "Project",
    "ProjectState",
    "RefreshBatchResult",
    "RefreshResult",
    "RefreshStatus",
    "RetryPolicy",
    "SourceType",
    "filter_projects",
    "match_project_pattern",
    "normalize_project_list",
]

# Absolute ceiling on concurrent SSH sessions to a Gerrit server for the
# auto-computed default thread count. The real constraint is the server's
# per-user SSH connection limit, not local CPU, so cap independently of core
# count to avoid throttling on high-core machines. Users can still override the
# default explicitly with ``--threads``.
_MAX_GERRIT_THREADS = 8


@dataclass
class Config:
    """Configuration for repository clone operations (Gerrit or GitHub)."""

    # Connection settings
    host: str
    # Port for Gerrit SSH/HTTP connections (default: 29418 for Gerrit, None for GitHub)
    # For GitHub sources, port is None since GitHub APIs use standard HTTPS port 443.
    # This design makes invalid states unrepresentable - GitHub configs won't have
    # a meaningless port value.
    port: int | None = None
    base_url: str | None = None
    ssh_user: str | None = None

    # Source type and discovery settings
    source_type: SourceType = SourceType.GERRIT
    # ``None`` means "derive from source type and clone protocol" (resolved in
    # __post_init__). This keeps discovery and clone protocol consistent and
    # makes contradictory combinations (e.g. SSH discovery with HTTPS cloning)
    # impossible to reach silently.
    discovery_method: DiscoveryMethod | None = None

    # GitHub-specific settings
    github_token: str | None = None
    github_org: str | None = None
    use_gh_cli: bool = False

    # Clone behavior
    path: Path = field(default_factory=Path)
    skip_archived: bool = True
    threads: int | None = None
    depth: int | None = None
    branch: str | None = None
    mirror: bool = True  # Use git clone --mirror by default for complete metadata
    use_https: bool = False
    keep_remote_protocol: bool = False
    # Optional inclusion filter: if non-empty, only clone listed projects
    # Supports shell-style wildcards (*, ?, [seq]) and hierarchical matching
    include_projects: list[str] = field(default_factory=list)
    # Optional exclusion filter: matching projects are removed after inclusion
    # Supports the same pattern syntax as include_projects
    exclude_projects: list[str] = field(default_factory=list)
    # Enable verbose SSH (-vvv) for debugging single-project auth issues
    ssh_debug: bool = False
    # Exit cloning immediately when the first error occurs (for debugging)
    exit_on_error: bool = False

    # Parent/child strategy is always "both" - clone all repositories
    # Allow nested git working trees when BOTH is selected (safety switch)
    allow_nested_git: bool = True
    # When True, automatically add nested child repo paths to parent .git/info/exclude
    nested_protection: bool = True
    # When True, move conflicting files/directories in parent repos to [NAME].parent to allow nested cloning
    move_conflicting: bool = True

    # SSH/Security settings
    strict_host_checking: bool = True
    ssh_identity_file: Path | None = None
    clone_timeout: int = 600

    # Retry configuration
    retry_policy: RetryPolicy = field(default_factory=RetryPolicy)

    # Output settings
    manifest_filename: str = "clone-manifest.json"
    verbose: bool = False
    quiet: bool = False

    # Refresh settings (for clone command integration)
    auto_refresh: bool = True
    force_refresh: bool = False
    fetch_only: bool = False
    skip_conflicts: bool = True
    # The content filters the run applies to what it clones and refreshes,
    # if any.  A repository content filtering has rewritten is refreshed
    # only under filters covering the ones that rewrote it; see
    # source_clone.refresh_filtered.  Kept out of repr: it holds tokens.
    content_filters: ContentFilterSpec | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        """Validate and normalize configuration."""
        validate_and_normalize(self)

    @property
    def effective_threads(self) -> int:
        """Get the effective thread count to use.

        macOS / Apple Silicon: prefer performance cores only.
        Attempts to query performance core count; falls back to 10,
        then caps at 32. For other systems, use heuristic cpu_count * 4.

        For Gerrit (SSH) sources, the dynamically calculated default is then
        halved (floor of 1) to reduce the risk of overwhelming a Gerrit
        server's SSH front-end with too many concurrent handshakes. Bursts of
        parallel SSH connections are frequently throttled by Gerrit and
        surface as transient "Could not read from remote repository"
        failures. The halved value is additionally capped at
        ``_MAX_GERRIT_THREADS`` since the binding constraint is the server's
        per-user SSH connection limit rather than local CPU. Users who want a
        different concurrency can still pass ``--threads`` explicitly.

        For GitHub sources the halving does not apply; instead a 2x multiplier
        is used since operations are network-limited rather than
        CPU/filesystem-limited. This optimization typically halves clone time
        for GitHub repositories (e.g., 10 cores -> 20 threads for GitHub,
        max 64 threads vs 16 halved for Gerrit).
        """
        if self.threads is not None:
            return self.threads

        # Apple platform heuristic
        if platform.system() == "Darwin":
            perf_cores: int | None = None
            # Newer macOS exposes performance core count via sysctl keys
            candidates = [
                # Primary (performance) cluster
                "hw.perflevel0.physicalcpu",
                "hw.perflevel0.cores",
            ]
            for key in candidates:
                try:
                    out = subprocess.run(
                        ["sysctl", "-n", key],
                        capture_output=True,
                        text=True,
                        timeout=0.25,
                        check=True,
                    )
                    val = int(out.stdout.strip())
                    if val > 0:
                        perf_cores = val
                        break
                except Exception:
                    continue
            if perf_cores is None:
                # Fallback assumption for common 10-performance-core configs
                perf_cores = 10
            base_threads = max(1, min(32, perf_cores))
        else:
            cpu_count = os.cpu_count() or 4
            base_threads = min(32, cpu_count * 4)

        # Apply 2x multiplier for GitHub sources (network-limited operations)
        # GitHub cloning is primarily network-bound rather than CPU/filesystem-bound,
        # so we can safely use more concurrent workers. Testing shows this typically
        # halves clone time (e.g., 78 repos: ~2min -> ~1min on 10-core system).
        if self.source_type == SourceType.GITHUB:
            return min(64, base_threads * 2)

        # Halve the dynamically calculated default (floor of 1) for Gerrit (SSH)
        # sources to avoid saturating Gerrit's SSH server with concurrent
        # handshakes, which can trigger transient "Could not read from remote
        # repository" errors. The result is additionally capped at
        # _MAX_GERRIT_THREADS because the real limit is the server's per-user
        # SSH connection count, not local core count. GitHub sources are
        # unaffected: they are handled above with their own higher, network-bound
        # concurrency.
        return max(1, min(base_threads // 2, _MAX_GERRIT_THREADS))

    @property
    def protocol(self) -> str:
        """Get the clone protocol being used."""
        return "HTTPS" if self.use_https else "SSH"

    @property
    def effective_port(self) -> int | None:
        """Get the effective port for the protocol.

        Returns:
            Port number for Gerrit sources, None for GitHub sources.
        """
        return self.port

    @property
    def projects_url(self) -> str:
        """Get the Gerrit projects API URL."""
        return f"{self.base_url}/projects/?d"

    @property
    def git_ssh_command(self) -> str | None:
        """Get GIT_SSH_COMMAND environment value if needed."""
        # Add aggressive timeouts to prevent hanging in CI environments
        base_opts = [
            "ssh",
            "-o",
            "BatchMode=yes",
            "-o",
            "ConnectTimeout=10",
            "-o",
            "ServerAliveInterval=5",
            "-o",
            "ServerAliveCountMax=3",
            "-o",
            "ConnectionAttempts=2",
        ]

        # Add SSH identity file if specified
        if self.ssh_identity_file:
            base_opts.extend(["-i", str(self.ssh_identity_file)])

        if self.strict_host_checking:
            base_opts.extend(["-o", "StrictHostKeyChecking=yes"])
        else:
            base_opts.extend(["-o", "StrictHostKeyChecking=accept-new"])

        # Append verbose SSH diagnostics when ssh_debug is enabled
        if getattr(self, "ssh_debug", False):
            base_opts.append("-vvv")
        return " ".join(base_opts)
