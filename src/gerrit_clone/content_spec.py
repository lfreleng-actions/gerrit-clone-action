# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""The content filters a run applies, decided per repository.

Two sources, combined: the run's own ``--remove-files``, ``--git-filter``
and ``--redact-secrets`` options, and the tree's recorded intent
(:mod:`gerrit_clone.content_intent`) -- so a run without the options
still filters as the tree was filtered before, and one with them only
ever adds.  Removal patterns and secret redaction come from either.
Tokens come only from the options, since the intent keeps digests alone:
a project whose intent replaces a token the run does not supply cannot
be filtered as decided, and :meth:`ContentFilterSpec.missing_tokens`
says so for the caller to refuse it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from gerrit_clone.content_intent import FilterIntent, manifest_summary
from gerrit_clone.content_patterns import (
    normalize_file_patterns,
    parse_git_filter_spec,
)
from gerrit_clone.content_policy import FilterPolicy, collect_filter_tokens
from gerrit_clone.refresh_discovery import project_name_for

if TYPE_CHECKING:
    from pathlib import Path

    from gerrit_clone.content_journal import Journal


@dataclass(frozen=True)
class ProjectFilters:
    """What to filter one project with, ready for ``apply_content_filters``."""

    remove_patterns: list[str] | None
    # Tokens are secrets; kept out of any repr that might reach a log.
    git_filter_projects: dict[str, list[str]] | None = field(repr=False)
    redact_secrets: bool
    #: Tokens the intent replaces in this project that the run lacks.
    missing_tokens: int
    #: Whether any filter scans history, which a shallow clone truncates.
    history: bool

    @property
    def empty(self) -> bool:
        return not (self.remove_patterns or self.history)


@dataclass(frozen=True)
class ContentFilterSpec:
    """The content filters a whole run applies, to decide per repository."""

    remove_patterns: list[str] | None
    # Tokens are secrets; kept out of any repr that might reach a log.
    git_filter_projects: dict[str, list[str]] | None = field(repr=False)
    redact_secrets: bool
    base_path: Path
    intent: FilterIntent = field(default_factory=FilterIntent)
    #: Where rewrites are journalled; ``None`` for a run that writes nothing.
    journal: Journal | None = None

    def project_name(self, repo_path: Path) -> str:
        """*repo_path*'s project name, relative to the run's base path.

        Both sides are resolved: a clone's target path is not, and on
        a system where the temporary directory is a symlink the two
        would otherwise never compare, leaving only the leaf name.
        """
        return project_name_for(repo_path.resolve(), self.base_path.resolve())

    def filters_for(self, project: str) -> ProjectFilters:
        """What *project* is filtered with: the run's options and its intent."""
        intended = self.intent.for_project(project)
        tokens = self._tokens(project)
        options = list(self.remove_patterns or ())
        extra = sorted(intended.remove_patterns - set(options))
        supplied = FilterPolicy.of(None, tokens, False).token_digests
        redact = self.redact_secrets or intended.redact_secrets
        return ProjectFilters(
            remove_patterns=(options + extra) or None,
            git_filter_projects=self.git_filter_projects,
            redact_secrets=redact,
            missing_tokens=len(intended.token_digests - supplied),
            history=redact or bool(tokens),
        )

    def policy_for(self, repo_path: Path) -> FilterPolicy:
        """The policy this run would filter *repo_path* under."""
        return self.project_policy(self.project_name(repo_path))

    def project_policy(self, project: str) -> FilterPolicy:
        """The policy this run would filter project *project* under."""
        filters = self.filters_for(project)
        return FilterPolicy.of(
            filters.remove_patterns, self._tokens(project), filters.redact_secrets
        )

    def missing_tokens(self, repo_path: Path) -> int:
        """Tokens *repo_path*'s intent replaces that this run does not supply."""
        return self.filters_for(self.project_name(repo_path)).missing_tokens

    def _tokens(self, project: str) -> list[str]:
        if not self.git_filter_projects:
            return []
        return collect_filter_tokens(project, self.git_filter_projects)

    @classmethod
    def from_options(
        cls,
        remove_files: str | None,
        git_filter: str | None,
        redact_secrets: bool,
        base_path: Path,
    ) -> ContentFilterSpec | None:
        """Parse the command-line filter options; ``None`` if none were given."""
        if not (remove_files or git_filter or redact_secrets):
            return None
        return cls(
            normalize_file_patterns([remove_files]) if remove_files else None,
            parse_git_filter_spec(git_filter) if git_filter else None,
            redact_secrets,
            base_path,
        )


def manifest_entry(spec: ContentFilterSpec | None) -> dict[str, Any] | None:
    """A manifest's ``content_filters`` block: the tree's intent, or null.

    It summarises the intent the run resolved, its own options included,
    with each scope's patterns and a count of its tokens.
    """
    if spec is None:
        return None
    return manifest_summary(spec.base_path, spec.intent)


def missing_tokens_refusal(count: int) -> str:
    """Why a project whose intent needs *count* unsupplied tokens is refused."""
    return (
        f"This tree's filter intent replaces {count} --git-filter token(s) in "
        f"this project that the run does not supply, so it cannot be filtered "
        f"as decided. Pass --git-filter with them, as when it was first filtered"
    )
