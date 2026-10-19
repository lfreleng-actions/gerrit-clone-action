# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""Exception hierarchy for filesystem path handling.

Leaf module so that path validation, naming and conflict handling can share a
single exception hierarchy without importing each other.
"""

from __future__ import annotations


class PathError(Exception):
    """Base exception for path-related errors."""


class PathConflictError(PathError):
    """Raised when a path conflict prevents operation."""


class PathValidationError(PathError):
    """Raised when a path fails validation."""
