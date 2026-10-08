# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""One repository through the content filters, for every command.

``clone``, ``refresh`` and ``mirror`` each filter what they fetched after
fetching it, and each makes the same two refusals before rewriting
anything:

- A project whose intent replaces ``--git-filter`` tokens the run does
  not supply is not filtered at all: the tokens are kept only as
  digests, and filtering it without them would leave them in place.
- ``--git-filter`` and ``--redact-secrets`` scan the whole history, and
  a shallow clone hides part of it, so they would mark it filtered
  having seen only some of it.  They are dropped for that repository,
  which counts as failed; ``--remove-files``, which needs no history,
  still runs.

Each rewrite is journalled before it starts and once it ends (see
:mod:`gerrit_clone.content_journal`), and one that cannot be journalled
does not start.

Another run may extend the tree's intent while this one filters.  A
result filtered under less than the intent now holds must not be
published, or it would undo that run's decision: see :func:`publishing`.
"""

from __future__ import annotations

from contextlib import contextmanager, nullcontext
from typing import TYPE_CHECKING, Protocol

from gerrit_clone.content_filter import apply_content_filters, is_shallow_repository
from gerrit_clone.content_intent import (
    LOCK_WAIT,
    IntentError,
    intent_locked,
    load_intent,
    project_locked,
)
from gerrit_clone.content_policy import FilterPolicy, collect_filter_tokens
from gerrit_clone.content_spec import missing_tokens_refusal

if TYPE_CHECKING:
    from collections.abc import Callable, Generator
    from pathlib import Path

    from gerrit_clone.content_spec import ContentFilterSpec

UNJOURNALLED_REFUSAL = (
    "Could not journal the rewrite in the tree's filter journal, so the "
    "repository was not filtered; see the log for why"
)

STALE_INTENT_REFUSAL = (
    "Another run extended the tree's filter intent while this one filtered "
    "the repository, so it was not published filtered under less than the "
    "tree now decides. Run again"
)

SHALLOW_HISTORY_REFUSED = (
    "Refused --git-filter / --redact-secrets on a shallow repository: "
    "truncated history can hide older secrets. Re-clone it without --depth"
)


class ApplyContentFilters(Protocol):
    """Call signature of :func:`gerrit_clone.content_filter.apply_content_filters`."""

    def __call__(
        self,
        repo_path: Path,
        project_name: str,
        remove_patterns: list[str] | None = None,
        git_filter_projects: dict[str, list[str]] | None = None,
        *,
        redact_secrets: bool = False,
        timeout: int = 600,
    ) -> tuple[bool, str | None]:
        """Filter the repository at *repo_path*."""
        ...


def filter_repository(
    spec: ContentFilterSpec,
    repo_path: Path,
    project: str,
    timeout: int,
    *,
    is_shallow: Callable[[Path], bool] = is_shallow_repository,
    apply: ApplyContentFilters = apply_content_filters,
) -> str | None:
    """Filter *repo_path*, project *project*, as *spec* decides for it.

    Args:
        spec: The run's filters, its options and the tree's intent.
        repo_path: Repository to filter.
        project: Its project name, for ``--git-filter`` patterns.
        timeout: Seconds each filtering step may take.
        is_shallow: Shallow-clone probe, injectable for the caller.
        apply: Filter runner, injectable for the caller.

    Returns:
        ``None`` if it was filtered as decided, else why it was not.
    """
    filters = spec.filters_for(project)
    if filters.missing_tokens:
        return missing_tokens_refusal(filters.missing_tokens)
    if filters.empty:
        return None
    git_filter = filters.git_filter_projects
    redact = filters.redact_secrets
    refused = None
    if filters.history and is_shallow(repo_path):
        if not filters.remove_patterns:
            return SHALLOW_HISTORY_REFUSED
        refused, git_filter, redact = SHALLOW_HISTORY_REFUSED, None, False
    journal = spec.journal
    try:
        with (
            project_locked(journal.root, project, max(timeout, LOCK_WAIT))
            if journal is not None
            else nullcontext()
        ):
            failure = _rewrite(
                spec,
                repo_path,
                project,
                (filters.remove_patterns, git_filter, redact),
                timeout,
                apply,
            )
    except IntentError as exc:
        failure = str(exc)
    return "; ".join(reason for reason in (refused, failure) if reason) or None


def _rewrite(
    spec: ContentFilterSpec,
    repo_path: Path,
    project: str,
    filters: tuple[list[str] | None, dict[str, list[str]] | None, bool],
    timeout: int,
    apply: ApplyContentFilters,
) -> str | None:
    """Journal, apply and check one rewrite; why it failed, or ``None``.

    Held under the project's rewrite lock by the caller, so no two
    rewrites of one project overlap (see
    :func:`gerrit_clone.content_intent.project_locked`).
    """
    remove_patterns, git_filter, redact = filters
    journal = spec.journal
    entry = None
    if journal is not None:
        tokens = collect_filter_tokens(project, git_filter) if git_filter else []
        policy = FilterPolicy.of(remove_patterns, tokens, redact)
        entry = journal.start(repo_path, project, policy)
        if entry is None:
            return UNJOURNALLED_REFUSAL
    ok, error = apply(
        repo_path,
        project,
        remove_patterns=remove_patterns,
        git_filter_projects=git_filter,
        redact_secrets=redact,
        timeout=timeout,
    )
    if journal is not None and entry is not None:
        # Left without an end if apply raised: then the start stays binding.
        journal.end(entry, repo_path, ok=ok)
    if ok:
        # Clone and mirror filter in place: this is where they publish.
        with publishing(spec, project) as stale:
            if stale is not None:
                ok, error = False, stale
    return None if ok else (error or "content filtering failed")


@contextmanager
def publishing(spec: ContentFilterSpec, project: str) -> Generator[str | None]:
    """Hold the tree's intent lock while *project*'s filtered result goes out.

    Yields:
        Why it must not go out -- the intent now asks more of *project*
        than this run filtered it with -- or ``None``.  Holding the lock
        until publishing ends stops another run extending the intent
        meanwhile.  A run that writes nothing, such as a dry run, takes
        no lock and is never stale.

    Raises:
        IntentError: If the lock could not be taken, or the intent read.
    """
    journal = spec.journal
    if journal is None:
        yield None
        return
    with intent_locked(journal.root):
        decided = load_intent(journal.root).for_project(project)
        yield (
            None
            if spec.project_policy(project).covers(decided)
            else (STALE_INTENT_REFUSAL)
        )
