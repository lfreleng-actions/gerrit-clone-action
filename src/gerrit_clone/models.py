# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2025 Matthew Watkins <mwatkins@linuxfoundation.org>

"""Data models for Gerrit clone operations."""

from __future__ import annotations

import os
import platform
import subprocess
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import Enum
from pathlib import Path
from typing import Any


class ProjectState(str, Enum):
    """Gerrit project states."""

    ACTIVE = "ACTIVE"
    READ_ONLY = "READ_ONLY"
    HIDDEN = "HIDDEN"


class SourceType(str, Enum):
    """Source repository platform type."""

    GERRIT = "gerrit"
    GITHUB = "github"


class DiscoveryMethod(str, Enum):
    """Method for discovering projects."""

    SSH = "ssh"
    HTTP = "http"
    BOTH = "both"
    GITHUB_API = "github_api"


class CloneStatus(str, Enum):
    """Clone operation status."""

    PENDING = "pending"
    CLONING = "cloning"
    SUCCESS = "success"
    FAILED = "failed"
    SKIPPED = "skipped"
    ALREADY_EXISTS = "already_exists"
    REFRESHED = "refreshed"
    VERIFIED = "verified"


class RefreshStatus(str, Enum):
    """Refresh operation status."""

    PENDING = "pending"
    REFRESHING = "refreshing"
    SUCCESS = "success"
    UP_TO_DATE = "up_to_date"
    FAILED = "failed"
    SKIPPED = "skipped"
    CONFLICTS = "conflicts"
    NOT_GIT_REPO = "not_git_repo"
    NOT_GERRIT_REPO = "not_gerrit_repo"
    UNCOMMITTED_CHANGES = "uncommitted_changes"
    DETACHED_HEAD = "detached_head"


# Parent/child policy is always "both" - clone all repositories


@dataclass(frozen=True)
class Project:
    """Represents a project from any source (Gerrit or GitHub)."""

    name: str
    state: ProjectState
    description: str | None = None
    web_links: list[dict[str, str]] | None = None
    source_type: SourceType = SourceType.GERRIT
    clone_url: str | None = None
    ssh_url_override: str | None = None
    default_branch: str | None = None
    metadata: dict[str, Any] | None = None

    @property
    def is_active(self) -> bool:
        """Check if project is in ACTIVE state."""
        return self.state == ProjectState.ACTIVE

    def ssh_url(self, host: str, port: int = 29418, user: str | None = None) -> str:
        """Generate SSH clone URL for this project."""
        if self.ssh_url_override:
            return self.ssh_url_override
        user_prefix = f"{user}@" if user else ""
        return f"ssh://{user_prefix}{host}:{port}/{self.name}"

    def https_url(self, base_url: str | None = None) -> str:
        """Generate HTTPS clone URL for this project."""
        if self.clone_url:
            return self.clone_url
        if base_url:
            return f"{base_url.rstrip('/')}/{self.name}"
        return f"https://{self.name}"

    @property
    def filesystem_path(self) -> Path:
        """Get the filesystem path where this project should be cloned."""
        return Path(self.name)


@dataclass
class RetryPolicy:
    """Configuration for retry behavior."""

    max_attempts: int = 3
    base_delay: float = 2.0
    factor: float = 2.0
    max_delay: float = 30.0
    jitter: bool = True

    def __post_init__(self) -> None:
        """Validate retry policy parameters."""
        if self.max_attempts < 1:
            raise ValueError("max_attempts must be at least 1")
        if self.base_delay <= 0:
            raise ValueError("base_delay must be positive")
        if self.factor < 1:
            raise ValueError("factor must be at least 1")
        if self.max_delay < self.base_delay:
            raise ValueError("max_delay must be >= base_delay")


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
    discovery_method: DiscoveryMethod = DiscoveryMethod.SSH

    # GitHub-specific settings
    github_token: str | None = None
    github_org: str | None = None
    use_gh_cli: bool = False

    # Clone behavior
    path: Path = field(default_factory=lambda: Path())
    skip_archived: bool = True
    threads: int | None = None
    depth: int | None = None
    branch: str | None = None
    mirror: bool = True  # Use git clone --mirror by default for complete metadata
    use_https: bool = False
    keep_remote_protocol: bool = False
    # Optional inclusion filter: if non-empty, only clone listed projects (exact names)
    include_projects: list[str] = field(default_factory=list)
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

    def __post_init__(self) -> None:
        """Validate and normalize configuration."""
        if not self.host:
            raise ValueError("host is required")

        # Set default port based on source type if not explicitly provided
        if self.port is None:
            if self.source_type == SourceType.GERRIT:
                # Default Gerrit SSH port
                self.port = 29418
            # For GitHub, leave port as None (not used)

        # Validate port range for Gerrit sources
        # After applying defaults above, port is guaranteed non-None for Gerrit
        # Port is only meaningful for Gerrit (SSH/HTTP endpoint configuration)
        # For GitHub sources, port should be None and is not validated
        if self.source_type == SourceType.GERRIT:
            # Type assertion for mypy - port is guaranteed non-None after defaults
            assert self.port is not None
            if self.port <= 0 or self.port > 65535:
                raise ValueError("port must be between 1 and 65535")


        if self.threads is not None and self.threads < 1:
            raise ValueError("threads must be at least 1")

        if self.depth is not None and self.depth < 1:
            raise ValueError("depth must be at least 1")

        if self.clone_timeout <= 0:
            raise ValueError("clone_timeout must be positive")

        # Validate mirror option compatibility
        if self.mirror:
            # --mirror is incompatible with --depth and --branch
            # When mirror is enabled, we override these options
            if self.depth is not None:
                logger = __import__(
                    "gerrit_clone.logging", fromlist=["get_logger"]
                ).get_logger(__name__)
                logger.warning(
                    "mirror mode is incompatible with --depth option. "
                    "Ignoring --depth and using full clone."
                )
                object.__setattr__(self, "depth", None)

            if self.branch is not None:
                logger = __import__(
                    "gerrit_clone.logging", fromlist=["get_logger"]
                ).get_logger(__name__)
                logger.warning(
                    "mirror mode is incompatible with --branch option. "
                    "Ignoring --branch and cloning all refs."
                )
                object.__setattr__(self, "branch", None)

        # Normalize include_projects (strip whitespace, drop empties, de-dup preserve order)
        if self.include_projects:
            seen: set[str] = set()
            normalized: list[str] = []
            for name in self.include_projects:
                clean = name.strip()
                if clean and clean not in seen:
                    normalized.append(clean)
                    seen.add(clean)
            object.__setattr__(self, "include_projects", normalized)

        # Ensure path is absolute
        self.path = self.path.resolve()

        # Generate base_url if not provided
        if self.base_url is None:
            if self.source_type == SourceType.GERRIT:
                from gerrit_clone.discovery import discover_gerrit_base_url

                try:
                    self.base_url = discover_gerrit_base_url(self.host)
                except Exception as e:
                    # Fall back to basic URL if discovery fails
                    logger = __import__(
                        "gerrit_clone.logging", fromlist=["get_logger"]
                    ).get_logger(__name__)
                    logger.debug(
                        f"API discovery failed for {self.host}, using basic URL: {e}"
                    )
                    self.base_url = f"https://{self.host}"
            elif self.source_type == SourceType.GITHUB:
                # For GitHub, use api.github.com or GitHub Enterprise URL
                if "github.com" in self.host.lower():
                    self.base_url = "https://api.github.com"
                else:
                    # GitHub Enterprise
                    self.base_url = f"https://{self.host}/api/v3"

        # Validate GitHub-specific requirements
        # Check for token: explicit config > GERRIT_CLONE_TOKEN > GITHUB_TOKEN
        if (
            self.source_type == SourceType.GITHUB
            and not self.github_token
            and not os.getenv("GERRIT_CLONE_TOKEN")
            and not os.getenv("GITHUB_TOKEN")
        ):
            logger = __import__(
                "gerrit_clone.logging", fromlist=["get_logger"]
            ).get_logger(__name__)
            logger.warning(
                "No GitHub token provided. SSH clones of public repos "
                "will still work; a token is required for private "
                "repositories or authenticated HTTPS/API access. "
                "Set GERRIT_CLONE_TOKEN, GITHUB_TOKEN, or use "
                "--github-token when needed."
            )

    @property
    def effective_threads(self) -> int:
        """Get the effective thread count to use.

        macOS / Apple Silicon: prefer performance cores only.
        Attempts to query performance core count; falls back to 10,
        then caps at 32. For other systems, use heuristic cpu_count * 4.

        For GitHub sources, uses 2x multiplier since operations are network-limited
        rather than CPU/filesystem-limited. This optimization typically halves
        clone time for GitHub repositories (e.g., 10 cores -> 20 threads for GitHub,
        max 64 threads vs 32 for Gerrit).
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

        return base_threads

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


@dataclass
class CloneResult:
    """Result of a clone operation for a single project."""

    project: Project
    status: CloneStatus
    path: Path
    attempts: int = 0
    duration_seconds: float = 0.0
    error_message: str | None = None
    started_at: datetime | None = None
    completed_at: datetime | None = None
    # Name of ancestor (parent) project if this repository was cloned nested under a parent
    nested_under: str | None = None
    # Retry tracking fields for complete attempt history
    first_started_at: datetime | None = None
    retry_count: int = 0
    last_attempt_duration: float = 0.0
    # Refresh tracking fields
    was_refreshed: bool = False
    refresh_had_updates: bool = False
    refresh_commits_pulled: int = 0

    @property
    def success(self) -> bool:
        """Check if clone operation was successful.

        Returns True for all non-error statuses:
        - SUCCESS: Newly cloned repository
        - ALREADY_EXISTS: Repository existed, no changes
        - REFRESHED: Repository existed and was updated (pulled new commits)
        - VERIFIED: Repository existed and was verified as up-to-date (no changes)

        Note: For detailed statistics, use refresh_had_updates to distinguish
        between repos that were updated (REFRESHED with updates) vs merely
        verified as current (VERIFIED or REFRESHED without updates).
        """
        return self.status in (CloneStatus.SUCCESS, CloneStatus.ALREADY_EXISTS, CloneStatus.REFRESHED, CloneStatus.VERIFIED)

    @property
    def failed(self) -> bool:
        """Check if clone failed."""
        return self.status == CloneStatus.FAILED

    @property
    def skipped(self) -> bool:
        """Check if clone was skipped."""
        return self.status == CloneStatus.SKIPPED

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary for JSON serialization."""
        data = {
            "project": self.project.name,
            "path": str(self.path),
            "status": self.status.value,
            "attempts": self.attempts,
            "duration_s": round(self.duration_seconds, 3),
            "error": self.error_message,
            "started_at": self.started_at.isoformat() if self.started_at else None,
            "completed_at": self.completed_at.isoformat()
            if self.completed_at
            else None,
            "nested_under": self.nested_under,
            "first_started_at": self.first_started_at.isoformat()
            if self.first_started_at
            else None,
            "retry_count": self.retry_count,
            "last_attempt_duration_s": round(self.last_attempt_duration, 3),
        }
        if self.nested_under:
            data["nested_under"] = self.nested_under
        return data


@dataclass
class BatchResult:
    """Results of a batch clone operation."""

    config: Config
    results: list[CloneResult]
    started_at: datetime
    completed_at: datetime | None = None

    @property
    def total_count(self) -> int:
        """Total number of projects processed."""
        return len(self.results)

    @property
    def success_count(self) -> int:
        """Number of successful operations (aggregate of all non-error statuses).

        This includes:
        - Newly cloned repositories (SUCCESS)
        - Already existing repositories (ALREADY_EXISTS)
        - Refreshed repositories that pulled changes (REFRESHED)
        - Verified repositories that were up-to-date (VERIFIED)

        For more granular statistics, use the individual count properties:
        - already_exists_count: repos that existed, not refreshed
        - refreshed_count: repos that were refreshed (pulled changes)
        - verified_count: repos verified as up-to-date (no changes)
        """
        return sum(1 for r in self.results if r.success)

    @property
    def already_exists_count(self) -> int:
        """Number of repositories that already existed (not refreshed)."""
        return sum(1 for r in self.results if r.status == CloneStatus.ALREADY_EXISTS)

    @property
    def refreshed_count(self) -> int:
        """Number of repositories that were refreshed (pulled changes)."""
        return sum(1 for r in self.results if r.status == CloneStatus.REFRESHED)

    @property
    def verified_count(self) -> int:
        """Number of repositories that were verified as up-to-date."""
        return sum(1 for r in self.results if r.status == CloneStatus.VERIFIED)

    @property
    def failed_count(self) -> int:
        """Number of failed clones."""
        return sum(1 for r in self.results if r.failed)

    @property
    def skipped_count(self) -> int:
        """Number of skipped clones."""
        return sum(1 for r in self.results if r.skipped)

    @property
    def duration_seconds(self) -> float:
        """Total duration of batch operation."""
        if self.completed_at is None:
            return 0.0
        return (self.completed_at - self.started_at).total_seconds()

    @property
    def success_rate(self) -> float:
        """Success rate as a percentage (includes already existing, refreshed, and verified repos)."""
        if self.total_count == 0:
            return 0.0
        return (self.success_count / self.total_count) * 100

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary for JSON serialization."""
        return {
            "version": "1.0",
            "generated_at": (self.completed_at or datetime.now(UTC)).isoformat(),
            "host": self.config.host,
            "port": self.config.port if self.config.port is not None else "N/A",
            "source_type": self.config.source_type.value,
            "clone_config": {
                "use_https": self.config.use_https,
                "use_gh_cli": self.config.use_gh_cli,
                "depth": self.config.depth,
                "branch": self.config.branch,
                "discovery_method": self.config.discovery_method.value,
            },
            "total": self.total_count,
            "succeeded": self.success_count,
            "already_exists": self.already_exists_count,
            "refreshed": self.refreshed_count,
            "verified": self.verified_count,
            "failed": self.failed_count,
            "skipped": self.skipped_count,
            "success_rate": round(self.success_rate, 2),
            "duration_s": round(self.duration_seconds, 3),
            "config": {
                "skip_archived": self.config.skip_archived,
                "threads": self.config.effective_threads,
                "depth": self.config.depth,
                "branch": self.config.branch,
                "strict_host_checking": self.config.strict_host_checking,
                "path": str(self.config.path),
            },
            "results": [result.to_dict() for result in self.results],
        }


@dataclass
class RefreshResult:
    """Result of a refresh operation for a single repository."""

    path: Path
    project_name: str
    status: RefreshStatus
    started_at: datetime

    # Git state tracking
    was_behind: bool = False
    commits_pulled: int = 0
    files_changed: int = 0
    current_branch: str | None = None
    remote_url: str | None = None

    # Operation metadata
    attempts: int = 0
    duration_seconds: float = 0.0
    error_message: str | None = None
    completed_at: datetime | None = None

    # Conflict/warning tracking
    had_uncommitted_changes: bool = False
    stash_created: bool = False
    stash_popped: bool = False
    detached_head: bool = False

    # Retry tracking
    first_started_at: datetime | None = None
    retry_count: int = 0
    last_attempt_duration: float = 0.0

    @property
    def success(self) -> bool:
        """Check if refresh was successful."""
        return self.status in (RefreshStatus.SUCCESS, RefreshStatus.UP_TO_DATE)

    @property
    def failed(self) -> bool:
        """Check if refresh failed."""
        return self.status == RefreshStatus.FAILED

    @property
    def skipped(self) -> bool:
        """Check if refresh was skipped."""
        return self.status in (
            RefreshStatus.SKIPPED,
            RefreshStatus.NOT_GIT_REPO,
            RefreshStatus.NOT_GERRIT_REPO,
            RefreshStatus.UNCOMMITTED_CHANGES,
        )

    @property
    def has_conflicts(self) -> bool:
        """Check if refresh resulted in conflicts."""
        return self.status == RefreshStatus.CONFLICTS

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary for JSON serialization."""
        data = {
            "project": self.project_name,
            "path": str(self.path),
            "status": self.status.value,
            "attempts": self.attempts,
            "duration_s": round(self.duration_seconds, 3),
            "error": self.error_message,
            "started_at": self.started_at.isoformat() if self.started_at else None,
            "completed_at": self.completed_at.isoformat()
            if self.completed_at
            else None,
            "was_behind": self.was_behind,
            "commits_pulled": self.commits_pulled,
            "files_changed": self.files_changed,
            "current_branch": self.current_branch,
            "remote_url": self.remote_url,
            "had_uncommitted_changes": self.had_uncommitted_changes,
            "stash_created": self.stash_created,
            "stash_popped": self.stash_popped,
            "detached_head": self.detached_head,
            "first_started_at": self.first_started_at.isoformat()
            if self.first_started_at
            else None,
            "retry_count": self.retry_count,
            "last_attempt_duration_s": round(self.last_attempt_duration, 3),
        }
        return data


@dataclass
class RefreshBatchResult:
    """Results of a batch refresh operation."""

    base_path: Path
    results: list[RefreshResult]
    started_at: datetime
    completed_at: datetime | None = None

    @property
    def total_count(self) -> int:
        """Total number of repositories processed."""
        return len(self.results)

    @property
    def success_count(self) -> int:
        """Number of successful refreshes."""
        return sum(1 for r in self.results if r.success)

    @property
    def up_to_date_count(self) -> int:
        """Number of repositories already up-to-date."""
        return sum(1 for r in self.results if r.status == RefreshStatus.UP_TO_DATE)

    @property
    def updated_count(self) -> int:
        """Number of repositories actually updated."""
        return sum(
            1
            for r in self.results
            if r.status == RefreshStatus.SUCCESS and r.was_behind
        )

    @property
    def failed_count(self) -> int:
        """Number of failed refreshes."""
        return sum(1 for r in self.results if r.failed)

    @property
    def skipped_count(self) -> int:
        """Number of skipped refreshes."""
        return sum(1 for r in self.results if r.skipped)

    @property
    def conflicts_count(self) -> int:
        """Number of repositories with conflicts."""
        return sum(1 for r in self.results if r.has_conflicts)

    @property
    def duration_seconds(self) -> float:
        """Total duration of batch operation."""
        if self.completed_at is None:
            return 0.0
        return (self.completed_at - self.started_at).total_seconds()

    @property
    def success_rate(self) -> float:
        """Success rate as a percentage."""
        if self.total_count == 0:
            return 0.0
        return (self.success_count / self.total_count) * 100

    @property
    def total_commits_pulled(self) -> int:
        """Total commits pulled across all repositories."""
        return sum(r.commits_pulled for r in self.results)

    @property
    def total_files_changed(self) -> int:
        """Total files changed across all repositories."""
        return sum(r.files_changed for r in self.results)

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary for JSON serialization."""
        return {
            "version": "1.0",
            "operation": "refresh",
            "generated_at": (self.completed_at or datetime.now(UTC)).isoformat(),
            "base_path": str(self.base_path),
            "total": self.total_count,
            "succeeded": self.success_count,
            "up_to_date": self.up_to_date_count,
            "updated": self.updated_count,
            "failed": self.failed_count,
            "skipped": self.skipped_count,
            "conflicts": self.conflicts_count,
            "success_rate": round(self.success_rate, 2),
            "duration_s": round(self.duration_seconds, 3),
            "total_commits_pulled": self.total_commits_pulled,
            "total_files_changed": self.total_files_changed,
            "results": [result.to_dict() for result in self.results],
        }
