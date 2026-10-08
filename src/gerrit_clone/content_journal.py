# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""The tree's journal of content-filtering rewrites.

The intent (:mod:`gerrit_clone.content_intent`) records what was decided
for a tree.  This records the runs that carried it out:
``<tree>/.gerrit-clone/filter-journal.jsonl`` gets one JSON line before
each rewrite starts and another once it ends.  The start names the
command and release, the project and repository, the method --
``git filter-repo`` or the worktree fallback -- the filters, and a digest
of every ref, and the remotes it fetches from, without credentials; the
end says whether the rewrite worked, with the digest of the refs it
left.  Like the intent, it holds a token's digest only.

It is an audit trail, with one exception: a start without a successful
end.  A crash, or a rewrite that failed part-way, may have left the
repository partly rewritten, so its filters stay in force -- added to
the intent at the next run -- until a later rewrite of the same project
completes under filters that cover them.

A start is written, and flushed to disk, before the rewrite begins; if it
cannot be, the repository is not filtered.  Appends take the intent's
lock, so a line from one process never lands inside another's.  A last
line a crash cut short is a start or an end whose rewrite never began or
whose start already counts, so readers skip it and the next append
removes it.  Any other line that cannot be understood stops the run, as
an unreadable intent does.  The journal is never written through a
symbolic link, which a checkout could track to aim it outside the tree.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from gerrit_clone import __version__
from gerrit_clone.content_intent import (
    INTENT_DIR,
    JOURNAL_FILE,
    NO_FOLLOW,
    FilterIntent,
    IntentError,
    intent_locked,
    policy_fields,
    policy_from_fields,
)
from gerrit_clone.content_origin import git
from gerrit_clone.content_removal import _check_git_filter_repo
from gerrit_clone.logging import get_logger
from gerrit_clone.url_credentials import redact_url

if TYPE_CHECKING:
    from pathlib import Path

    from gerrit_clone.content_policy import FilterPolicy

logger = get_logger(__name__)

#: Every entry's ``schema``; one written by another release stops the run.
JOURNAL_SCHEMA = 1

_SHA256 = re.compile(r"[0-9a-f]{64}")


@dataclass(frozen=True)
class Journal:
    """Where a run journals its rewrites, and which command it is."""

    root: Path
    command: str

    @property
    def path(self) -> Path:
        return journal_path(self.root)

    def start(self, repo_path: Path, project: str, policy: FilterPolicy) -> str | None:
        """Journal a rewrite about to begin.

        Returns:
            The entry's id, for :meth:`end`; ``None`` if it could not be
            written, or the refs or remotes could not be read for it, in
            which case the rewrite must not begin.
        """
        refs = refs_digest(repo_path)
        remotes = sources(repo_path)
        if refs is None or remotes is None:
            logger.error(f"Could not read the refs or remotes of {repo_path}")
            return None
        entry_id = uuid.uuid4().hex
        written = self._append(
            {
                "schema": JOURNAL_SCHEMA,
                "event": "start",
                "id": entry_id,
                "time": _now(),
                "version": __version__,
                "command": self.command,
                "project": project,
                "path": _relative(repo_path, self.root),
                "sources": remotes,
                "method": "filter-repo" if _check_git_filter_repo() else "worktree",
                "policy": policy_fields(policy),
                "refs_sha256": refs,
            }
        )
        return entry_id if written else None

    def end(self, entry_id: str, repo_path: Path, *, ok: bool) -> None:
        """Journal the end of the rewrite :meth:`start` returned *entry_id* for.

        Without the refs it left, the end is not written: the start stays
        binding, which is safe.  Failing to write it does the same, so
        neither is raised.
        """
        refs = refs_digest(repo_path)
        written = refs is not None and self._append(
            {
                "schema": JOURNAL_SCHEMA,
                "event": "end",
                "id": entry_id,
                "time": _now(),
                "ok": ok,
                "refs_sha256": refs,
            }
        )
        if not written:
            logger.warning(
                f"Could not journal the end of filtering {repo_path}; its "
                f"filters stay in force for the tree until it is filtered again"
            )

    def _append(self, entry: dict[str, Any]) -> bool:
        line = (json.dumps(entry, sort_keys=True) + "\n").encode("utf-8")
        try:
            with intent_locked(self.root):
                _drop_torn_line(self.path)
                created = not self.path.exists()
                # Owner-only, as the intent is: a token's digest lets anyone
                # who can read it confirm a guessed token offline.
                handle = os.open(
                    self.path,
                    os.O_WRONLY | os.O_APPEND | os.O_CREAT | NO_FOLLOW,
                    0o600,
                )
                try:
                    # A file already there keeps its mode: a checkout can
                    # track one, as 0644.
                    if os.name != "nt":
                        os.fchmod(handle, 0o600)
                    written = os.write(handle, line)
                    os.fsync(handle)
                finally:
                    os.close(handle)
                if created:
                    # Without their entries on disk, a start could be lost
                    # to a crash with the rewrite already under way; the
                    # first run may have made .gerrit-clone/ just now.
                    _sync_directory(self.path.parent)
                    _sync_directory(self.root)
        except (IntentError, OSError) as exc:
            logger.error(f"Could not write the filter journal {self.path}: {exc}")
            return False
        return written == len(line)


def sources(repo_path: Path) -> dict[str, str] | None:
    """Where *repo_path* fetches from: each remote's URL, without secrets.

    A staging copy's own path is a temporary directory, so the journal
    names the upstream by these, through
    :func:`gerrit_clone.url_credentials.redact_url`.

    Returns:
        ``None`` if the remotes could not be read; empty if it has none.
    """
    listed = git(repo_path, "config", "--get-regexp", r"^remote\..*\.url$")
    # Exit status 1 is git finding no remote; anything else is a failure.
    if listed is None or listed.returncode not in (0, 1):
        return None
    found: dict[str, str] = {}
    for line in listed.stdout.splitlines() if listed.returncode == 0 else []:
        key, _, url = line.partition(" ")
        # Git fetches from a remote's first URL, so that one names it.
        found.setdefault(
            key.removeprefix("remote.").removesuffix(".url"), redact_url(url)
        )
    return dict(sorted(found.items()))


def journal_path(root: Path) -> Path:
    return root / INTENT_DIR / JOURNAL_FILE


def refs_digest(repo_path: Path, *prefixes: str) -> str | None:
    """SHA-256 of every ref, or those under *prefixes*, and what each
    points to; ``None`` if they could not be read."""
    listed = git(
        repo_path, "for-each-ref", "--format=%(objectname) %(refname)", *prefixes
    )
    if listed is None or listed.returncode != 0:
        return None
    return hashlib.sha256(listed.stdout.encode("utf-8")).hexdigest()


def unfinished_intent(root: Path) -> FilterIntent:
    """The filters of every rewrite the journal shows not completed.

    Each is scoped to its project, and released once it, or a rewrite of
    that project that started after it, completes under filters covering
    it.  Rewrites of one project never overlap -- each holds the
    project's rewrite lock from its start to its end -- so a start left
    without an end before a later one began is one that crashed.

    Raises:
        IntentError: If the journal could not be read, or holds an entry
            that cannot be understood.
    """
    path = journal_path(root)
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        if path.is_symlink():
            # It marks the tree's root (see find_root): read as no journal,
            # a run in a subtree would set a parent's intent aside.
            raise IntentError(
                f"The filter journal {path} is a link to nothing"
            ) from None
        return FilterIntent()
    except (OSError, UnicodeError) as exc:
        raise IntentError(f"Could not read the filter journal {path}: {exc}") from exc
    # Whatever follows the last newline is a line a crash cut short.
    lines = text.split("\n")[:-1]
    pending: dict[str, tuple[str, FilterPolicy]] = {}
    for number, line in enumerate(lines, 1):
        if not line.strip():
            continue
        try:
            _apply_entry(pending, json.loads(line))
        except (ValueError, TypeError, KeyError) as exc:
            raise IntentError(
                f"Could not understand line {number} of the filter journal "
                f"{path}: {exc}"
            ) from exc
    intent = FilterIntent()
    for project, policy in pending.values():
        intent = intent.union(FilterIntent({("project", project): policy}))
    return intent


def _apply_entry(pending: dict[str, tuple[str, FilterPolicy]], entry: Any) -> None:
    if not isinstance(entry, dict):
        raise TypeError("entry is not a JSON object")
    schema = entry.get("schema")
    # bool is an int, and 1.0 == 1: only the JSON integer itself will do.
    if type(schema) is not int or schema != JOURNAL_SCHEMA:
        raise ValueError(
            f"schema {schema!r} is not {JOURNAL_SCHEMA}; written by another release"
        )
    digest = entry.get("refs_sha256")
    # The refs' digest is each entry's evidence; none is written without.
    if not isinstance(digest, str) or not _SHA256.fullmatch(digest):
        raise ValueError(f"refs_sha256 is not a SHA-256 digest: {digest!r}")
    event, entry_id = entry["event"], entry["id"]
    if event == "start":
        project = entry["project"]
        if not isinstance(project, str):
            raise TypeError(f"project is not a string: {project!r}")
        pending[entry_id] = (project, policy_from_fields(entry["policy"]))
    elif event != "end":
        raise ValueError(f"unknown event {event!r}")
    elif entry["ok"] is True and entry_id in pending:
        project, policy = pending[entry_id]
        # Starts in journal order, up to this one: a rewrite that started
        # later, in another run, may yet be cut short.
        for other in list(pending):
            other_project, other_policy = pending[other]
            if other_project == project and policy.covers(other_policy):
                del pending[other]
            if other == entry_id:
                break


def _drop_torn_line(path: Path) -> None:
    """Remove a last line a crash cut short, so the next one starts clean."""
    if path.is_symlink():
        raise OSError(f"{path} is a symbolic link; not writing through it")
    try:
        with os.fdopen(os.open(path, os.O_RDWR | NO_FOLLOW), "rb+") as stream:
            size = stream.seek(0, os.SEEK_END)
            if size == 0:
                return
            stream.seek(size - 1)
            if stream.read(1) == b"\n":
                return
            stream.seek(0)
            kept = stream.read().rfind(b"\n") + 1
            stream.truncate(kept)
    except FileNotFoundError:
        return


def _sync_directory(path: Path) -> None:
    """Flush *path*'s entries to disk, where the OS can sync a directory."""
    if os.name == "nt":
        return
    handle = os.open(path, os.O_RDONLY)
    try:
        os.fsync(handle)
    finally:
        os.close(handle)


def _relative(repo_path: Path, root: Path) -> str:
    try:
        return repo_path.resolve().relative_to(root.resolve()).as_posix()
    except ValueError:
        return str(repo_path)


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")
