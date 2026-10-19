# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""Recognising a repository an earlier release content-filtered.

Releases before :mod:`source_clone.content_policy` existed filtered
repositories without recording which filters they applied.  Nothing
can show that a refresh re-applies filters nobody recorded, so such a
repository has to be refused one -- and to be recognised, it has to be
recognised by what those releases left behind:

- ``git filter-repo`` leaves its ``filter-repo/`` metadata directory in
  the git directory, with a ``commit-map`` once it has rewritten
  anything: ``--analyze`` creates the directory too, but rewrites
  nothing.  It also removes every remote, but a remote added back, or
  one not named ``origin``, would fetch the original history.
- Without ``git filter-repo`` installed, which no release declared as a
  dependency, ``--remove-files`` committed the removal on every branch
  instead, keeping ``origin`` and its ``+refs/*:refs/*`` refspec: one
  fetch puts the removed files back at every tip.

This release leaves the same traces when it filters, so a repository
whose filters are recorded carries a stamp saying so, and is never
inspected for them.
"""

from __future__ import annotations

from pathlib import Path

from source_clone.content_origin import git

#: Subject of the commit the worktree fallback made on every branch.
LEGACY_REMOVAL_SUBJECT = "Remove filtered files for platform sync"


def earlier_release_traces(repo_path: Path) -> bool | None:
    """Whether *repo_path* shows an earlier release's content filtering.

    Returns:
        ``True`` or ``False``; ``None`` if git could not tell, which the
        caller must treat as the repository being unreadable.
    """
    git_dir = git(repo_path, "rev-parse", "--absolute-git-dir")
    if git_dir is None or git_dir.returncode != 0:
        return None
    if (Path(git_dir.stdout.strip()) / "filter-repo" / "commit-map").is_file():
        return True
    tips = git(repo_path, "for-each-ref", "--format=%(contents:subject)", "refs/heads/")
    if tips is None or tips.returncode != 0:
        return None
    return LEGACY_REMOVAL_SUBJECT in tips.stdout.splitlines()
