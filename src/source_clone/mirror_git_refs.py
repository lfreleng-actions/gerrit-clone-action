# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""Indirection for the git ref helpers used by mirror branch handling.

The mirror manager reads branch state from local clones through a
:class:`GitRefInspector` rather than importing the ``git_utils``
helpers directly into every collaborating module.  The manager builds
the inspector from its own module globals at call time, which keeps a
single, patchable definition site for these helpers while still
allowing the branch logic itself to live outside ``mirror_manager``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

# Preferred default branch names, most preferred first.  Used when a
# repository's HEAD does not point at a branch and a candidate must be
# chosen from the available refs/heads/* entries.
PREFERRED_DEFAULT_BRANCHES = ("master", "main", "develop")


@dataclass(frozen=True)
class GitRefInspector:
    """Callables that read ref state from a local git clone.

    Each field mirrors one helper from :mod:`source_clone.git_utils`.
    Calls are made lazily by the consuming code, preserving the order
    and number of git invocations of the original inline logic.
    """

    current_branch: Callable[[Path], str | None]
    head_ref: Callable[[Path], str | None]
    is_parent_project: Callable[[Path], bool]
    list_branches: Callable[[Path], list[str]]


def pick_default_branch(branches: list[str]) -> str | None:
    """Choose the best default-branch candidate from *branches*.

    Prefers well-known default names and otherwise falls back to the
    first branch reported by git.

    Args:
        branches: Local branch names under ``refs/heads/``.

    Returns:
        The chosen branch name, or ``None`` when *branches* is empty.
    """
    for candidate in PREFERRED_DEFAULT_BRANCHES:
        if candidate in branches:
            return candidate
    return branches[0] if branches else None
