# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""What the operator decided to filter, for a whole clone tree.

A repository's own record (:mod:`gerrit_clone.content_policy`) says which
filters *rewrote* it -- an effect, withdrawn when a filter changed
nothing.  It cannot say what was decided for the tree: a project cloned
later, or a file that only appears upstream later, has no record at all.

This is that decision.  It lives beside the projects, in
``<tree>/.gerrit-clone/filter-policy.json``, so it outlives any one
repository: one deleted and cloned again, by hand or by
``mirror --overwrite``, is filtered from it as before.  It only ever
grows.  Each scope -- every project, a ``--git-filter`` project pattern,
or one project exactly -- holds a :class:`~gerrit_clone.content_policy.\
FilterPolicy`; a project's share is the union of every scope covering it.

Like the repository record, it never holds a token: only the SHA-256
digest of each, to check tokens given again against.  A file that
cannot be read, or that a newer release wrote, is an error, never an
empty decision.

ADR 0001 records the design: ``docs/adr/0001-content-filter-intent.md``.
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
import tempfile
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from gerrit_clone.content_policy import FilterPolicy
from gerrit_clone.models import match_project_pattern

if TYPE_CHECKING:
    from collections.abc import Generator, Iterable, Mapping

INTENT_DIR = ".gerrit-clone"
INTENT_FILE = "filter-policy.json"
#: Kept beside the intent: see :mod:`gerrit_clone.content_journal`.
JOURNAL_FILE = "filter-journal.jsonl"
LOCK_FILE = "filter-policy.lock"
#: Per-project rewrite locks: see :func:`project_locked`.
LOCKS_DIR = "locks"
SCHEMA = 1

#: Seconds a run waits for another to finish extending the intent.
LOCK_WAIT = 120.0

#: Opens a file without following a symbolic link, where the OS can.
NO_FOLLOW = getattr(os, "O_NOFOLLOW", 0)

#: Scope of every project in the tree.
ALL_PROJECTS = ("projects", "*")


class IntentError(RuntimeError):
    """The tree's filter intent could not be read or written.

    Nothing may be cloned, refreshed or mirrored: either way, what the
    tree must be filtered under is unknown.
    """


def intent_path(root: Path) -> Path:
    return root / INTENT_DIR / INTENT_FILE


def find_root(start: Path) -> Path | None:
    """The nearest of *start* and its parents holding an intent or journal.

    The journal counts too: with the intent file gone, its unfinished
    rewrites still bind, and a run in a subtree must find them.  Anything
    at either path counts, a directory or a broken link included: passed
    over, it would let a run in a subtree choose a weaker root, where
    reading it fails closed.
    """
    here = start.resolve()
    for candidate in (here, *here.parents):
        for name in (INTENT_FILE, JOURNAL_FILE):
            found = candidate / INTENT_DIR / name
            if found.exists() or found.is_symlink():
                return candidate
    return None


@dataclass(frozen=True)
class FilterIntent:
    """Filter policy per scope.

    A scope is ``("projects", pattern)``, matched as ``--git-filter``
    and ``--include-projects`` patterns are, or ``("project", name)``,
    matched exactly.
    """

    scopes: Mapping[tuple[str, str], FilterPolicy] = field(default_factory=dict)

    @property
    def empty(self) -> bool:
        return all(policy.empty for policy in self.scopes.values())

    def for_project(self, name: str) -> FilterPolicy:
        """What was decided for project *name*, from every scope covering it."""
        policy = FilterPolicy()
        for (kind, value), scoped in self.scopes.items():
            if (kind == "project" and value == name) or (
                kind == "projects" and match_project_pattern(name, value)
            ):
                policy = policy.union(scoped)
        return policy

    def union(self, other: FilterIntent) -> FilterIntent:
        scopes = dict(self.scopes)
        for scope, policy in other.scopes.items():
            scopes[scope] = scopes.get(scope, FilterPolicy()).union(policy)
        return FilterIntent(scopes)

    @classmethod
    def of_options(
        cls,
        remove_patterns: Iterable[str] | None,
        git_filter_projects: Mapping[str, Iterable[str]] | None,
        redact_secrets: bool,
    ) -> FilterIntent:
        """The decision a run's ``--remove-files``, ``--git-filter`` and
        ``--redact-secrets`` options make."""
        scopes: dict[tuple[str, str], FilterPolicy] = {}
        everywhere = FilterPolicy.of(list(remove_patterns or ()), [], redact_secrets)
        if not everywhere.empty:
            scopes[ALL_PROJECTS] = everywhere
        for pattern, tokens in (git_filter_projects or {}).items():
            scoped = FilterPolicy.of(None, list(tokens), False)
            if not scoped.empty:
                scopes[("projects", pattern)] = scoped
        return cls(scopes)


def load_intent(root: Path) -> FilterIntent:
    """The intent recorded for the tree at *root*; empty if none is.

    Raises:
        IntentError: If the file exists but cannot be read or understood.
    """
    path = intent_path(root)
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        if path.is_symlink():
            # A link to nothing is a broken intent, not an absent one.
            raise IntentError(
                f"The filter intent {path} is a link to nothing"
            ) from None
        return FilterIntent()
    except (OSError, UnicodeError) as exc:
        raise IntentError(f"Could not read the filter intent {path}: {exc}") from exc
    try:
        return _decode(json.loads(text))
    except (ValueError, TypeError, KeyError) as exc:
        raise IntentError(
            f"Could not understand the filter intent {path}: {exc}"
        ) from exc


def save_intent(root: Path, intent: FilterIntent) -> None:
    """Write *intent* for the tree at *root*, all of it or none of it.

    Hold :func:`intent_locked` around reading the intent, extending it
    and calling this, or another run's additions can be lost.

    Raises:
        IntentError: If it could not be written.
    """
    path = intent_path(root)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        handle, temporary = tempfile.mkstemp(prefix=f".{INTENT_FILE}.", dir=path.parent)
        try:
            with os.fdopen(handle, "w", encoding="utf-8") as stream:
                json.dump(_encode(intent), stream, indent=2, sort_keys=True)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            Path(temporary).replace(path)
        except BaseException:
            Path(temporary).unlink(missing_ok=True)
            raise
    except OSError as exc:
        raise IntentError(f"Could not write the filter intent {path}: {exc}") from exc


@contextmanager
def intent_locked(root: Path) -> Generator[None, None, None]:
    """Hold the tree's intent lock, so runs extend the intent one at a time.

    Every run reads the intent, adds to it and writes it back.  Two at
    once would each write what they read, and the last write would undo
    the other's additions -- filtering that run had just decided on.

    Raises:
        IntentError: If the lock could not be taken within
            :data:`LOCK_WAIT` seconds, or not at all.
    """
    with _held(root, root / INTENT_DIR / LOCK_FILE, LOCK_WAIT):
        yield


@contextmanager
def project_locked(
    root: Path, project: str, wait: float
) -> Generator[None, None, None]:
    """Hold *project*'s rewrite lock, so its rewrites run one at a time.

    Then a journal start without an end, earlier than one that ended,
    belongs to a rewrite that crashed, never to one still running.

    Raises:
        IntentError: If the lock could not be taken within *wait*
            seconds, or not at all.
    """
    name = hashlib.sha256(project.encode("utf-8")).hexdigest()
    with _held(root, root / INTENT_DIR / LOCKS_DIR / f"{name}.lock", wait):
        yield


@contextmanager
def _held(root: Path, path: Path, wait: float) -> Generator[None, None, None]:
    """Hold the lock file *path*, under *root*'s ``.gerrit-clone/``.

    The file stays in place: deleting it would let a run lock a file
    another had already replaced.  Neither it nor a directory above it
    in ``.gerrit-clone/`` may be a symbolic link: a checkout can track
    one, and writes through it would land outside the tree.  Everything
    written there happens under one of these locks.
    """
    # Checked as well as opened without following: not every OS can.
    for linked in (root / INTENT_DIR, path.parent, path):
        if linked.is_symlink():
            raise IntentError(f"{linked} is a symbolic link; not writing through it")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        handle = os.open(path, os.O_RDWR | os.O_CREAT | NO_FOLLOW, 0o644)
    except OSError as exc:
        raise IntentError(f"Could not open the lock {path}: {exc}") from exc
    try:
        deadline = time.monotonic() + wait
        while not _try_lock(handle):
            if time.monotonic() >= deadline:
                raise IntentError(
                    f"Gave up after {wait:.0f}s waiting for another "
                    f"gerrit-clone run on this tree to release {path}; run "
                    f"again once it finishes"
                )
            time.sleep(0.1)
        try:
            yield
        finally:
            _unlock(handle)
    finally:
        os.close(handle)


if sys.platform == "win32":
    import msvcrt

    def _try_lock(handle: int) -> bool:
        """Take the lock on *handle* if no other process holds it."""
        try:
            msvcrt.locking(handle, msvcrt.LK_NBLCK, 1)
        except OSError:
            return False  # msvcrt reports a held lock as a plain OSError.
        return True

    def _unlock(handle: int) -> None:
        msvcrt.locking(handle, msvcrt.LK_UNLCK, 1)

else:
    import fcntl

    def _try_lock(handle: int) -> bool:
        """Take the lock on *handle* if no other process holds it."""
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return False
        except OSError as exc:
            raise IntentError(f"Could not lock the filter intent: {exc}") from exc
        return True

    def _unlock(handle: int) -> None:
        fcntl.flock(handle, fcntl.LOCK_UN)


def _encode(intent: FilterIntent) -> dict[str, Any]:
    entries = []
    for (kind, value), policy in sorted(intent.scopes.items()):
        if policy.empty:
            continue
        entries.append({kind: value, **policy_fields(policy)})
    return {"schema": SCHEMA, "entries": entries}


def manifest_summary(root: Path, intent: FilterIntent) -> dict[str, Any]:
    """*intent*, for a run's manifest: tokens counted, never their digests.

    Manifests often end up as public CI artifacts, and a digest lets
    anyone confirm a guessed token offline.
    """
    scopes = []
    for (kind, value), policy in sorted(intent.scopes.items()):
        if policy.empty:
            continue
        scope: dict[str, Any] = {kind: value}
        if policy.remove_patterns:
            scope["remove"] = sorted(policy.remove_patterns)
        if policy.token_digests:
            scope["tokens"] = len(policy.token_digests)
        if policy.redact_secrets:
            scope["redact_secrets"] = True
        scopes.append(scope)
    return {"intent_file": str(intent_path(root)), "scopes": scopes}


def policy_fields(policy: FilterPolicy) -> dict[str, Any]:
    """*policy* as JSON fields: tokens only as digests, empty ones left out."""
    fields: dict[str, Any] = {}
    if policy.remove_patterns:
        fields["remove"] = sorted(policy.remove_patterns)
    if policy.token_digests:
        fields["token_sha256"] = sorted(policy.token_digests)
    if policy.redact_secrets:
        fields["redact_secrets"] = True
    return fields


def policy_from_fields(fields: Mapping[str, Any]) -> FilterPolicy:
    """The policy :func:`policy_fields` wrote.

    Raises:
        TypeError: If a field holds the wrong kind of value.
    """
    redact = fields.get("redact_secrets", False)
    if not isinstance(redact, bool):
        raise TypeError(f"redact_secrets is not true or false: {redact!r}")
    return FilterPolicy(
        frozenset(_strings(fields, "remove")),
        frozenset(_strings(fields, "token_sha256")),
        redact,
    )


def _decode(data: Any) -> FilterIntent:
    if not isinstance(data, dict):
        raise TypeError("not a JSON object")
    schema = data.get("schema")
    # bool is an int, and 1.0 == 1: only the JSON integer itself will do.
    if type(schema) is not int or schema != SCHEMA:
        raise ValueError(
            f"schema {schema!r} is not {SCHEMA}; written by another release"
        )
    entries = data["entries"]
    if not isinstance(entries, list):
        raise TypeError("entries is not a list")
    scopes: dict[tuple[str, str], FilterPolicy] = {}
    for entry in entries:
        if not isinstance(entry, dict):
            raise TypeError(f"entry is not an object: {entry!r}")
        kinds = [kind for kind in ("projects", "project") if kind in entry]
        if len(kinds) != 1 or not isinstance(entry[kinds[0]], str):
            raise ValueError(f"entry without exactly one scope: {entry!r}")
        policy = policy_from_fields(entry)
        scope = (kinds[0], entry[kinds[0]])
        scopes[scope] = scopes.get(scope, FilterPolicy()).union(policy)
    return FilterIntent(scopes)


def _strings(entry: Mapping[str, Any], key: str) -> list[str]:
    values = entry.get(key, [])
    if not isinstance(values, list) or not all(isinstance(v, str) for v in values):
        raise TypeError(f"{key} is not a list of strings")
    return values
