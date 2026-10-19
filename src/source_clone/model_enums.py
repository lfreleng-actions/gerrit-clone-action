# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""Enumerations shared by the Gerrit clone data models.

Leaf module: defines the project, source, discovery and operation-status
enumerations used across configuration, results and reporting. It imports
nothing from the rest of the package so any model module may depend on it.
"""

from __future__ import annotations

from enum import StrEnum


class ProjectState(StrEnum):
    """Gerrit project states."""

    ACTIVE = "ACTIVE"
    READ_ONLY = "READ_ONLY"
    HIDDEN = "HIDDEN"


class SourceType(StrEnum):
    """Source repository platform type."""

    GERRIT = "gerrit"
    GITHUB = "github"


class DiscoveryMethod(StrEnum):
    """Method for discovering projects."""

    SSH = "ssh"
    HTTP = "http"
    BOTH = "both"
    GITHUB_API = "github_api"


class CloneStatus(StrEnum):
    """Clone operation status."""

    PENDING = "pending"
    CLONING = "cloning"
    SUCCESS = "success"
    FAILED = "failed"
    SKIPPED = "skipped"
    ALREADY_EXISTS = "already_exists"
    REFRESHED = "refreshed"
    VERIFIED = "verified"


class RefreshStatus(StrEnum):
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
