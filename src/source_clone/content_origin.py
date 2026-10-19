# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""Keeping a content-filtered repository fetchable, and never pushable.

``git filter-repo`` removes the ``origin`` remote whenever it rewrites
history, so that the rewritten history is not pushed back over the
original by mistake.  For a mirror that is also the only record of where
it came from: without ``remote.origin.fetch`` a later refresh has nothing
to fetch, and skips the repository from then on.

So what fetching needs -- the URL and the fetch refspecs -- is put back,
and filter-repo's protection is kept by pointing pushes nowhere.  That
protection is extended to every remote, not only ``origin``: a mirror
cloned with ``--origin upstream`` keeps its source remote through the
rewrite, and filter-repo does nothing to stop a push to it.
``remote.<name>.mirror`` is left alone; with nowhere to push to, it no
longer matters.

Which filters rewrote a repository, and so what may refresh it, is
:mod:`source_clone.content_policy`'s concern.
"""

from __future__ import annotations

import subprocess
from contextlib import contextmanager
from typing import TYPE_CHECKING, NamedTuple

from source_clone.logging import get_logger
from source_clone.subprocess_tracking import (
    ProcessAbandonedError,
    batch_abandoned,
    run_tracked,
)

if TYPE_CHECKING:
    from collections.abc import Generator
    from pathlib import Path

logger = get_logger(__name__)

#: Push URL left on every remote of a content-filtered repository.  Its
#: scheme has no remote helper, so git refuses a push at once -- and,
#: unlike a bare word, it can never name a local path, such as a nested
#: repository that happens to share it.
NO_PUSH_URL = "no-push://content-filtered-history"


class _Origin(NamedTuple):
    """What fetching from ``origin`` needs."""

    url: str
    fetch: list[str]


def git(repo_path: Path, *args: str) -> subprocess.CompletedProcess[str] | None:
    """Run git in *repo_path*, or ``None`` if git could not run.

    Tracked like every other child, since a refresh asks these questions
    from its pool threads.  An abandoned batch's refusal propagates, and
    so does a child it terminated: that exits nonzero exactly as git
    failing would, and is not a failure to report as one.

    Raises:
        ProcessAbandonedError: If the calling thread's batch was abandoned.
    """
    try:
        result = run_tracked(["git", *args], cwd=repo_path, timeout=10)
    except (OSError, subprocess.SubprocessError) as exc:
        logger.debug(f"Could not run git in {repo_path}: {exc}")
        return None
    if result.returncode != 0 and batch_abandoned():
        raise ProcessAbandonedError(f"git {args[0]} in {repo_path} was abandoned")
    return result


def git_config(repo_path: Path, *args: str) -> subprocess.CompletedProcess[str] | None:
    """Run ``git config`` in *repo_path*, or ``None`` if git could not run."""
    return git(repo_path, "config", *args)


def _read_origin(repo_path: Path) -> _Origin | None:
    """The repository's ``origin`` fetch settings, if it has an origin."""
    url = git_config(repo_path, "--get", "remote.origin.url")
    if url is None or url.returncode != 0 or not url.stdout.strip():
        return None
    fetch = git_config(repo_path, "--get-all", "remote.origin.fetch")
    refspecs = fetch.stdout.splitlines() if fetch is not None else []
    return _Origin(url.stdout.strip(), refspecs)


def _restore_origin(repo_path: Path, origin: _Origin) -> None:
    """Put *origin* back for fetching, with pushing disabled.

    The push URL goes first, so that a restore failing part-way leaves
    an ``origin`` that cannot be pushed to rather than one that can.
    """
    settings = [("remote.origin.pushurl", NO_PUSH_URL)]
    settings.append(("remote.origin.url", origin.url))
    settings += [("remote.origin.fetch", refspec) for refspec in origin.fetch]
    for key, value in settings:
        result = git_config(repo_path, "--add", key, value)
        if result is None or result.returncode != 0:
            detail = result.stderr.strip() if result is not None else "git failed"
            logger.warning(
                f"Could not restore {key} in {repo_path} after content "
                f"filtering: {detail}"
            )
            return
    logger.info(
        f"Restored origin in {repo_path} after content filtering rewrote "
        f"its history; pushing to it is disabled"
    )


@contextmanager
def origin_kept(repo_path: Path) -> Generator[None, None, None]:
    """Restore ``origin`` for fetching if the enclosed filtering removed it.

    Restored even if the filtering raises.

    Args:
        repo_path: Repository being filtered
    """
    before = _read_origin(repo_path)
    try:
        yield
    finally:
        if before is not None and _read_origin(repo_path) is None:
            _restore_origin(repo_path, before)


def block_pushes(repo_path: Path) -> bool:
    """Point every remote's pushes nowhere, so rewritten history stays put.

    Args:
        repo_path: A repository content filtering has rewritten

    Returns:
        True if every remote now refuses a push.
    """
    remotes = git(repo_path, "remote")
    if remotes is None or remotes.returncode != 0:
        return False
    for name in remotes.stdout.split():
        key = f"remote.{name}.pushurl"
        result = git_config(repo_path, "--replace-all", key, NO_PUSH_URL)
        if result is None or result.returncode != 0:
            return False
    return True


def push_urls(repo_path: Path) -> list[tuple[str, str]] | None:
    """Every remote's push URL setting, to put back later; ``None`` if unread."""
    result = git_config(repo_path, "--get-regexp", r"^remote\..*\.pushurl$")
    if result is None or result.returncode not in (0, 1):
        return None
    return [
        (key, value)
        for key, _, value in (
            line.partition(" ") for line in result.stdout.splitlines()
        )
    ]


def restore_push_urls(repo_path: Path, saved: list[tuple[str, str]]) -> None:
    """Put back the push URLs *saved* before pushing was blocked.

    Only for a filter that changed nothing: there is no rewritten history
    to protect, and the remotes should push as they did before.
    """
    remotes = git(repo_path, "remote")
    if remotes is None or remotes.returncode != 0:
        return
    for name in remotes.stdout.split():
        git_config(repo_path, "--unset-all", f"remote.{name}.pushurl")
    for key, value in saved:
        git_config(repo_path, "--add", key, value)
