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
from typing import Any, Optional


class ProjectState(str, Enum):
    """Gerrit project states."""

    ACTIVE = "ACTIVE"
    READ_ONLY = "READ_ONLY"
    HIDDEN = "HIDDEN"


class CloneStatus(str, Enum):
    """Clone operation status."""

    PENDING = "pending"
    CLONING = "cloning"
    SUCCESS = "success"
    FAILED = "failed"
    SKIPPED = "skipped"
    ALREADY_EXISTS = "already_exists"


# Parent/child policy is always "both" - clone all repositories


@dataclass(frozen=True)
class Project:
    """Represents a Gerrit project."""

    name: str
    state: ProjectState
    description: str | None = None
    web_links: list[dict[str, str]] | None = None

    @property
    def is_active(self) -> bool:
        """Check if project is in ACTIVE state."""
        return self.state == ProjectState.ACTIVE

    def ssh_url(self, host: str, port: int = 29418, user: str | None = None) -> str:
        """Generate SSH clone URL for this project."""
        user_prefix = f"{user}@" if user else ""
        return f"ssh://{user_prefix}{host}:{port}/{self.name}"

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
    """Configuration for Gerrit clone operations."""

    # Connection settings
    host: str
    port: int = 29418
    base_url: str | None = None
    ssh_user: str | None = None

    # Clone behavior
    path_prefix: Path = field(default_factory=lambda: Path())
    skip_archived: bool = True
    threads: int | None = None
    depth: int | None = None
    branch: str | None = None
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

    def __post_init__(self) -> None:
        """Validate and normalize configuration."""
        if not self.host:
            raise ValueError("host is required")

        if self.port <= 0 or self.port > 65535:
            raise ValueError("port must be between 1 and 65535")

        if self.threads is not None and self.threads < 1:
            raise ValueError("threads must be at least 1")

        if self.depth is not None and self.depth < 1:
            raise ValueError("depth must be at least 1")

        if self.clone_timeout <= 0:
            raise ValueError("clone_timeout must be positive")

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



        # Ensure path_prefix is absolute
        self.path_prefix = self.path_prefix.resolve()

        # Generate base_url if not provided using discovery
        if self.base_url is None:
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

    @property
    def effective_threads(self) -> int:
        """Get the effective thread count to use.

        macOS / Apple Silicon: prefer performance cores only.
        Attempts to query performance core count; falls back to 10,
        then caps at 32. For other systems, use heuristic cpu_count * 4.
        """
        if self.threads is not None:
            return self.threads

        # Apple platform heuristic
        if platform.system() == "Darwin":
            perf_cores: Optional[int] = None
            # Newer macOS exposes performance core count via sysctl keys
            candidates = [
                "hw.perflevel0.physicalcpu",  # Primary (performance) cluster
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
                # Fallback assumption for common 10‑performance‑core configs
                perf_cores = 10
            return max(1, min(32, perf_cores))

        cpu_count = os.cpu_count() or 4
        return min(32, cpu_count * 4)

    @property
    def protocol(self) -> str:
        """Get the clone protocol being used."""
        return "HTTPS" if self.use_https else "SSH"

    @property
    def effective_port(self) -> int:
        """Get the effective port for the protocol."""
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

    @property
    def success(self) -> bool:
        """Check if clone was successful."""
        return self.status in (CloneStatus.SUCCESS, CloneStatus.ALREADY_EXISTS)

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
        """Number of successful clones."""
        return sum(1 for r in self.results if r.success)

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
        """Success rate as a percentage."""
        if self.total_count == 0:
            return 0.0
        return (self.success_count / self.total_count) * 100

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary for JSON serialization."""
        return {
            "version": "1.0",
            "generated_at": (self.completed_at or datetime.now(UTC)).isoformat(),
            "host": self.config.host,
            "port": self.config.port,
            "total": self.total_count,
            "succeeded": self.success_count,
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
                "path_prefix": str(self.config.path_prefix),
            },
            "results": [result.to_dict() for result in self.results],
        }
