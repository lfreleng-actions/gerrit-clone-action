# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""Project name validation and filesystem-safe name derivation.

Rejects names that could escape the clone root (traversal sequences, absolute
paths, reserved git names) and rewrites the remainder into a name that is safe
on every supported platform. Leaf module over
:mod:`source_clone.path_errors`.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from source_clone.path_errors import PathValidationError

if TYPE_CHECKING:
    from pathlib import Path


def validate_project_name(project_name: str) -> None:
    """Validate that a project name is safe for filesystem use.

    Args:
        project_name: Project name to validate

    Raises:
        PathValidationError: If project name is invalid
    """
    if not project_name or not project_name.strip():
        raise PathValidationError("Project name cannot be empty")

    if project_name.startswith("/"):
        raise PathValidationError("Project name cannot start with '/'")

    dangerous_names = {".", "..", ".git"}
    if project_name in dangerous_names:
        raise PathValidationError(f"Project name cannot be '{project_name}'")

    if (
        project_name.startswith("../")
        or "/../" in project_name
        or project_name.endswith("/..")
    ):
        raise PathValidationError(
            "Project name cannot contain path traversal sequences"
        )

    if (
        project_name.startswith("./")
        or "/./" in project_name
        or project_name.endswith("/.")
    ):
        raise PathValidationError(
            "Project name cannot contain current directory references"
        )

    # Check for problematic characters (though Gerrit names are usually clean)
    problematic_chars = set('\0<>:"|?*')
    if any(char in project_name for char in problematic_chars):
        raise PathValidationError(
            f"Project name contains invalid characters: {project_name}"
        )


def sanitize_project_name(project_name: str) -> str:
    """Sanitize project name for filesystem use.

    Args:
        project_name: Raw project name

    Returns:
        Sanitized project name safe for filesystem

    Raises:
        PathValidationError: If project name cannot be sanitized
    """
    if not project_name or not project_name.strip():
        raise PathValidationError("Project name cannot be empty")

    sanitized = project_name.strip()

    reserved_names = {
        "CON",
        "PRN",
        "AUX",
        "NUL",
        "COM1",
        "COM2",
        "COM3",
        "COM4",
        "COM5",
        "COM6",
        "COM7",
        "COM8",
        "COM9",
        "LPT1",
        "LPT2",
        "LPT3",
        "LPT4",
        "LPT5",
        "LPT6",
        "LPT7",
        "LPT8",
        "LPT9",
    }
    if sanitized.upper() in reserved_names:
        sanitized = f"{sanitized}_project"

    # Replace problematic characters with safe alternatives
    char_replacements = {
        "<": "_lt_",
        ">": "_gt_",
        ":": "_colon_",
        '"': "_quote_",
        "|": "_pipe_",
        "?": "_q_",
        "*": "_star_",
        "\0": "_null_",
        "\\": "/",  # Convert backslashes to forward slashes
    }

    for bad_char, replacement in char_replacements.items():
        sanitized = sanitized.replace(bad_char, replacement)

    # Remove leading/trailing slashes; preserve a legitimate leading dot unless it's a dangerous name
    # (We already explicitly reject ".", "..", and ".git" earlier)
    leading_dot = sanitized.startswith(".") and not sanitized.startswith("..")
    sanitized = sanitized.strip("/\\")
    if not leading_dot:
        # Only strip leading dot if it wasn't a legitimate project like ".github"
        sanitized = sanitized.lstrip(".")
    sanitized = sanitized.rstrip(".")

    if sanitized in {".", "..", ".git"}:
        sanitized = f"_{sanitized}_safe"

    # Replace path traversal sequences
    sanitized = sanitized.replace("../", "_dotdot_")
    sanitized = sanitized.replace("/..", "_dotdot_")
    sanitized = sanitized.replace("./", "_dot_")
    sanitized = sanitized.replace("/.", "_dot_")

    if not sanitized:
        raise PathValidationError("Project name becomes empty after sanitization")

    return sanitized


def get_project_path(project_name: str, base_path: Path) -> Path:
    """Get the full filesystem path for a project.

    Args:
        project_name: Project name from Gerrit
        base_path: Base directory for all clones

    Returns:
        Full path where project should be cloned

    Raises:
        PathValidationError: If project name is invalid
    """
    sanitized_name = sanitize_project_name(project_name)
    return base_path / sanitized_name
