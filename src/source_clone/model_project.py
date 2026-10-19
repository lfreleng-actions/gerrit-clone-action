# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""Core value objects for Gerrit clone operations.

Leaf module holding the immutable :class:`Project` record and the
:class:`RetryPolicy` settings object. Depends only on
:mod:`source_clone.model_enums` so that configuration and result models may
import it freely.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from source_clone.model_enums import ProjectState, SourceType


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
