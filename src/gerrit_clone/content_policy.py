# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""Which content filters a repository carries, and what may refresh it.

Refreshing a repository content filtering has rewritten brings the
original history back: a mirror's ``+refs/*:refs/*`` fetch forces every
upstream ref over the rewritten one, and a hard reset does the same to a
working copy.  That is only safe when the same run filters it again, with
at least the filters that rewrote it -- a different ``--remove-files``
pattern, or ``--git-filter`` tokens for another project, would leave the
removed content back in history.

So each repository records the policy it was filtered under, in its own
git config, before any rewrite starts: the file patterns, a digest of
each replaced token (never the token itself), and whether secrets were
redacted.  A refresh proceeds only if the run's own policy for that
repository covers the recorded one.  Recording fails closed -- a
repository whose policy cannot be recorded is not filtered -- and a
record is withdrawn only when the filtering provably changed no ref.

A repository an earlier release filtered has no record, and nothing can
cover filters nobody recorded: it reads as filtered under an unknown
policy, which no run covers (see :mod:`gerrit_clone.content_legacy`).
That finding is recorded before this release filters it in turn, so
filtering it again never makes it refreshable.
"""

from __future__ import annotations

import hashlib
from contextlib import contextmanager
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING

from gerrit_clone.content_legacy import earlier_release_traces
from gerrit_clone.content_origin import (
    block_pushes,
    git,
    git_config,
    origin_kept,
    push_urls,
    restore_push_urls,
)
from gerrit_clone.logging import get_logger
from gerrit_clone.models import match_project_pattern

if TYPE_CHECKING:
    from collections.abc import Callable, Generator
    from pathlib import Path

logger = get_logger(__name__)

_REMOVED = "gerrit-clone.filteredRemoves"
_TOKEN = "gerrit-clone.filteredTokenSha256"
_REDACTED = "gerrit-clone.filteredRedactsSecrets"
_EARLIER = "gerrit-clone.filteredByEarlierRelease"
#: Written once this release records the repository's filters, and never
#: withdrawn: from then on, its own ``filter-repo`` traces are not taken
#: for an earlier release's.
_RECORDED = "gerrit-clone.filterPolicyRecorded"


class PolicyRecordError(RuntimeError):
    """The filter policy could not be recorded, so nothing was filtered."""


class PolicyReadError(PolicyRecordError):
    """The recorded policy could not be read, so nothing may proceed.

    Read as "never filtered", an unreadable record would let a refresh
    force a filtered repository's original content back.
    """


def _digest(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class FilterPolicy:
    """The content filters applied to one repository.

    Tokens are held as digests: the policy is written to disk, and a
    token is exactly what filtering exists to keep off it.
    """

    remove_patterns: frozenset[str] = frozenset()
    token_digests: frozenset[str] = frozenset()
    redact_secrets: bool = False
    #: Filtered by an earlier release, under filters it never recorded.
    earlier_release: bool = False

    @classmethod
    def of(
        cls,
        remove_patterns: list[str] | None,
        tokens: list[str],
        redact_secrets: bool,
    ) -> FilterPolicy:
        return cls(
            frozenset(remove_patterns or ()),
            frozenset(_digest(token) for token in tokens),
            redact_secrets,
        )

    @property
    def empty(self) -> bool:
        return not (
            self.remove_patterns
            or self.token_digests
            or self.redact_secrets
            or self.earlier_release
        )

    def covers(self, recorded: FilterPolicy) -> bool:
        """Whether filtering under this policy re-applies all of *recorded*.

        Never when *recorded* came from an earlier release: what it
        filtered is unknown.
        """
        return (
            not recorded.earlier_release
            and recorded.remove_patterns <= self.remove_patterns
            and recorded.token_digests <= self.token_digests
            and (self.redact_secrets or not recorded.redact_secrets)
        )

    def union(self, other: FilterPolicy) -> FilterPolicy:
        return FilterPolicy(
            self.remove_patterns | other.remove_patterns,
            self.token_digests | other.token_digests,
            self.redact_secrets or other.redact_secrets,
            self.earlier_release or other.earlier_release,
        )

    def minus(self, other: FilterPolicy) -> FilterPolicy:
        """What this policy holds that *other* does not."""
        return FilterPolicy(
            self.remove_patterns - other.remove_patterns,
            self.token_digests - other.token_digests,
            self.redact_secrets and not other.redact_secrets,
            self.earlier_release and not other.earlier_release,
        )

    def describe_missing(self, recorded: FilterPolicy) -> str:
        """What of *recorded* this policy lacks, without naming any token."""
        if recorded.earlier_release:
            return "the unrecorded filters of an earlier release"
        missing: list[str] = []
        patterns = sorted(recorded.remove_patterns - self.remove_patterns)
        if patterns:
            missing.append(f"--remove-files {','.join(patterns)}")
        tokens = len(recorded.token_digests - self.token_digests)
        if tokens:
            missing.append(f"--git-filter for {tokens} token(s)")
        if recorded.redact_secrets and not self.redact_secrets:
            missing.append("--redact-secrets")
        return "; ".join(missing)


def collect_filter_tokens(
    project_name: str,
    git_filter_projects: dict[str, list[str]],
) -> list[str]:
    """Aggregate the tokens configured for *project_name*.

    Tokens from every matching project pattern are combined and
    de-duplicated (preserving order) so ``git filter-repo`` only has to
    run once for the repository.

    Returns:
        De-duplicated token list; empty when no pattern matched.
    """
    unique_tokens: list[str] = []
    for pattern, token_list in git_filter_projects.items():
        if match_project_pattern(project_name, pattern):
            unique_tokens.extend(t for t in token_list if t not in unique_tokens)
    return unique_tokens


def recorded_policy(repo_path: Path) -> FilterPolicy:
    """The policy *repo_path* was last filtered under; empty if never.

    One with no record but an earlier release's traces reads as filtered
    by that release (see :mod:`gerrit_clone.content_legacy`).

    Raises:
        PolicyReadError: If the record could not be read.  Only a key
            that is not set -- git's exit status 1 -- reads as empty.
    """
    stored, stamped = _stored(repo_path)
    if stamped or stored.earlier_release:
        return stored
    traces = earlier_release_traces(repo_path)
    if traces is None:
        raise PolicyReadError(
            f"Could not tell whether an earlier release content-filtered {repo_path}"
        )
    return replace(stored, earlier_release=traces)


def _stored(repo_path: Path) -> tuple[FilterPolicy, bool]:
    """The policy *repo_path*'s config holds, and whether it was stamped.

    Every key in one git call: refresh asks this of each repository, and
    a run gathers it from every repository in the tree.

    Raises:
        PolicyReadError: If the config could not be read.  Only git's
            exit status 1 -- no key set -- reads as no record.
    """
    result = git_config(repo_path, "--null", "--get-regexp", r"^gerrit-clone\.")
    if result is None or result.returncode not in (0, 1):
        detail = result.stderr.strip() if result is not None else "git failed"
        raise PolicyReadError(
            f"Could not read the content-filter policy of {repo_path}: {detail}"
        )
    values: dict[str, list[str]] = {}
    for entry in result.stdout.split("\0") if result.returncode == 0 else []:
        key, _, value = entry.partition("\n")
        if key and value:
            # Git prints variable names in lower case.
            values.setdefault(key.lower(), []).append(value)

    def get(key: str) -> list[str]:
        return values.get(key.lower(), [])

    policy = FilterPolicy(
        frozenset(get(_REMOVED)),
        frozenset(get(_TOKEN)),
        "true" in get(_REDACTED),
        "true" in get(_EARLIER),
    )
    return policy, "true" in get(_RECORDED)


def stored_policy(repo_path: Path) -> FilterPolicy:
    """The policy *repo_path*'s config records, its traces not inspected.

    Raises:
        PolicyReadError: If the record could not be read.
    """
    return _stored(repo_path)[0]


def add_policy(repo_path: Path, policy: FilterPolicy) -> bool:
    """Add *policy* to the recorded one; False if git refused any of it.

    Only ever adds.  Removing the old record before writing the new one
    would leave a window -- or, if a write then failed, a lasting state
    -- in which a filtered repository records less than it should, and
    an unfiltered refresh could force its original content back.  A
    write that fails part-way here leaves the record larger, which only
    refuses more.

    Compared with what the config holds, not with what is read back: an
    earlier release's filtering, only detected so far, is written down
    too.  The stamp goes last, once everything it vouches for is there.
    """
    stored, stamped = _stored(repo_path)
    settings = _settings(policy.minus(stored))
    if not stamped:
        settings.append((_RECORDED, "true"))
    for key, value in settings:
        result = git_config(repo_path, "--add", key, value)
        if result is None or result.returncode != 0:
            return False
    return True


def mark_recorded(repo_path: Path) -> bool:
    """Stamp *repo_path* as one whose filters this release records.

    For a staging copy of a mirror whose record is already known: the
    copy has none of the mirror's config, but may carry the same branch
    tips, which would otherwise read as an earlier release's filtering.
    """
    result = git_config(repo_path, "--add", _RECORDED, "true")
    return result is not None and result.returncode == 0


def _withdraw(repo_path: Path, added: FilterPolicy) -> None:
    """Remove exactly *added* from the record, nothing recorded earlier.

    A removal that fails leaves the record larger, which only refuses
    more.
    """
    for key, value in _settings(added):
        git_config(repo_path, "--fixed-value", "--unset-all", key, value)


def _settings(policy: FilterPolicy) -> list[tuple[str, str]]:
    """*policy* as the git config values that record it."""
    settings = [(_REMOVED, p) for p in sorted(policy.remove_patterns)]
    settings += [(_TOKEN, d) for d in sorted(policy.token_digests)]
    if policy.redact_secrets:
        settings.append((_REDACTED, "true"))
    if policy.earlier_release:
        settings.append((_EARLIER, "true"))
    return settings


def _content_refs(repo_path: Path) -> list[str] | None:
    """Every ref and its target, bar remote-tracking ones; ``None`` if unread.

    Remote-tracking refs are left out because removing ``origin`` deletes
    them whether or not any content was filtered.  A filter that rewrote
    nothing leaves every other ref where it was.
    """
    result = git(repo_path, "for-each-ref", "--format=%(refname) %(objectname)")
    if result is None or result.returncode != 0:
        return None
    return [
        line
        for line in result.stdout.splitlines()
        if not line.startswith("refs/remotes/")
    ]


def record_and_block(
    repo_path: Path, policy: FilterPolicy
) -> tuple[FilterPolicy, list[tuple[str, str]]]:
    """Record *policy* in *repo_path* and block pushing: both or neither.

    Returns:
        What was added to the record, and the push URLs from before, for
        a caller that may later withdraw and restore them.

    Raises:
        PolicyRecordError: If either failed; whatever part of it took
            effect is then undone.  Push URLs that cannot be read refuse
            it before anything changes: blocking could fail part-way,
            with nothing to put back.
    """
    recorded = recorded_policy(repo_path)
    added = policy.minus(recorded)
    # An earlier release's filtering, if only detected so far, is written
    # down with the rest -- but is not among what may be withdrawn.
    persisted = replace(added, earlier_release=recorded.earlier_release)
    saved_push_urls = push_urls(repo_path)
    if saved_push_urls is None:
        raise PolicyRecordError(f"Could not read the push URLs of {repo_path}")
    if not add_policy(repo_path, persisted):
        failure = f"Could not record the content-filter policy for {repo_path}"
    elif not block_pushes(repo_path):
        failure = f"Could not block pushing from {repo_path}"
    else:
        return added, saved_push_urls
    _withdraw(repo_path, added)
    restore_push_urls(repo_path, saved_push_urls)
    raise PolicyRecordError(failure)


@contextmanager
def recorded_until_published(
    repo_path: Path, policy: FilterPolicy
) -> Generator[Callable[[], None], None, None]:
    """Record *policy* and block pushing, undone unless publishing completes.

    Call the yielded function once the publication it guards has
    completed.  Leaving the block without calling it -- returning early
    or raising -- withdraws what was added and restores the push URLs,
    so a failed publication leaves the repository as it was.

    Raises:
        PolicyRecordError: As :func:`record_and_block`.
    """
    added, saved_push_urls = record_and_block(repo_path, policy)
    completed = False

    def complete() -> None:
        nonlocal completed
        completed = True

    try:
        yield complete
    finally:
        if not completed:
            _withdraw(repo_path, added)
            restore_push_urls(repo_path, saved_push_urls)


@contextmanager
def content_filtering(
    repo_path: Path, policy: FilterPolicy
) -> Generator[list[str], None, None]:
    """Filter *repo_path* under *policy*, recorded and made safe.

    Before the enclosed filtering, both of these or no filtering at all:
    whatever of the policy is not already recorded is added, and pushing
    is blocked on every remote -- so no rewritten history is ever
    pushable, even for a moment, even if what follows is cut short.
    After it, even if it raises, ``origin`` is restored for fetching.
    If no ref changed -- shown by comparing them, never assumed -- the
    additions are withdrawn and the push URLs put back; an earlier
    record is never touched, nor an earlier release's filtering once it
    has been written down.

    Yields:
        A list that receives any error making the result safe, for the
        caller to report as a filtering failure.

    Raises:
        PolicyRecordError: If the policy could not be recorded, or
            pushing blocked.
    """
    try:
        added, saved_push_urls = record_and_block(repo_path, policy)
    except PolicyRecordError as exc:
        raise PolicyRecordError(f"{exc}; not filtering it") from exc
    refs_before = _content_refs(repo_path)
    errors: list[str] = []
    try:
        with origin_kept(repo_path):
            yield errors
    finally:
        refs_after = _content_refs(repo_path)
        if refs_before is not None and refs_before == refs_after:
            _withdraw(repo_path, added)
            restore_push_urls(repo_path, saved_push_urls)
        elif not block_pushes(repo_path):
            errors.append(
                f"Could not block pushing from {repo_path} after content "
                f"filtering rewrote it"
            )
