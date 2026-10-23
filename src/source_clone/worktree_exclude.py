# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""Keeping the tool's own files out of a checkout's ``git status``.

A run writes files into its output path: ``refresh`` its log and
manifest, and any filtering run the tree's intent.  When that path is a
working copy's top level, git reports those files as untracked changes
there, and refresh skips a working copy with uncommitted changes -- so
it would never update the one checkout it was pointed at.

They are listed in the checkout's ``info/exclude`` instead.  That file
belongs to the clone alone: nothing is committed, and the checkout's own
``.gitignore`` stays as it was.  It only hides untracked files, so a
change to a file git tracks still counts -- and a run refuses to start
rather than overwrite a tracked file that has one of its names.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from source_clone.content_origin import git
from source_clone.logging import get_logger

if TYPE_CHECKING:
    from collections.abc import Sequence

logger = get_logger(__name__)

#: Characters a gitignore pattern treats as wildcards or escapes.
_SPECIAL = frozenset("\\*?[")


def literal_pattern(relative: str) -> str | None:
    """An ``info/exclude`` line matching exactly *relative*, from the top.

    Returns:
        ``None`` for a name no exclude line can hold, one spanning lines.
    """
    if "\n" in relative or "\r" in relative:
        return None
    escaped = "".join(f"\\{char}" if char in _SPECIAL else char for char in relative)
    # Git drops trailing spaces from a pattern unless they are escaped.
    kept = escaped.rstrip(" ")
    return "/" + kept + "\\ " * (len(escaped) - len(kept))


def is_checkout_top(root: Path) -> bool:
    """Whether *root* is a working copy's top level.

    Raises:
        OSError: If *root* holds a ``.git`` entry but git could not say:
            a time-out, or a repository git refuses, such as one owned by
            another user.  Read as "not a checkout", it would let a run
            overwrite what the checkout tracks.
    """
    top = git(root, "rev-parse", "--show-toplevel")
    if top is not None and top.returncode == 0:
        return Path(top.stdout.strip()).resolve() == root.resolve()
    marker = root / ".git"
    if marker.exists() or marker.is_symlink():
        detail = top.stderr.strip() if top is not None else "git failed"
        raise OSError(f"Could not tell whether {root} is a checkout: {detail}")
    return False


def tracked_names(root: Path, names: Sequence[str]) -> list[str]:
    """Which of *names* git tracks, if *root* is a checkout's top level.

    ``info/exclude`` cannot hide a tracked file, and writing one would
    overwrite committed content.  Anywhere else the answer is none, as
    for :func:`hide_from_checkout`.

    Raises:
        OSError: If git could not tell, in what may be a checkout.
    """
    if not names or not is_checkout_top(root):
        return []
    found = git(root, "ls-files", "-z", "--", *(f":(literal){n}" for n in names))
    if found is None or found.returncode != 0:
        detail = found.stderr.strip() if found is not None else "git failed"
        raise OSError(f"Could not list the files {root} tracks: {detail}")
    return [name for name in found.stdout.split("\0") if name]


def hide_from_checkout(root: Path, patterns: Sequence[str]) -> None:
    """List *patterns* in ``info/exclude`` if *root* is a checkout's top level.

    Anywhere else this does nothing: *root* inside some unrelated
    checkout is not that checkout's business.

    Raises:
        OSError: If they could not be listed in a checkout at *root*, or
            git could not tell whether it is one.  Left unhidden, a
            file written there makes refresh skip the checkout, so the
            caller decides whether to go on.
    """
    if not is_checkout_top(root):
        return
    found = git(root, "rev-parse", "--git-path", "info/exclude")
    if found is None or found.returncode != 0:
        detail = found.stderr.strip() if found is not None else "git failed"
        raise OSError(f"Could not find the info/exclude of {root}: {detail}")
    exclude = root / found.stdout.strip()
    try:
        lines = (
            exclude.read_text(encoding="utf-8").splitlines() if exclude.exists() else []
        )
        missing = [pattern for pattern in patterns if pattern not in lines]
        if missing:
            exclude.parent.mkdir(parents=True, exist_ok=True)
            exclude.write_text("\n".join([*lines, *missing]) + "\n", encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise OSError(f"Could not write {exclude}: {exc}") from exc
