# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""Upgrading over repositories an earlier release content-filtered.

Releases before the filter policy was recorded left no record of what
they filtered.  Refreshing what they left -- a mirror the worktree
fallback committed a removal on, keeping ``origin``; or one
``git filter-repo`` rewrote, keeping a source remote -- would fetch the
filtered content straight back.  Each must be refused, for good.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, Mock, patch

import pytest

from source_clone import content_legacy
from source_clone.content_filter import apply_content_filters
from source_clone.content_intent import FilterIntent
from source_clone.content_legacy import LEGACY_REMOVAL_SUBJECT
from source_clone.content_policy import (
    PolicyReadError,
    recorded_policy,
)
from source_clone.content_spec import ContentFilterSpec
from source_clone.github_api import GitHubRepo, transform_gerrit_name_to_github
from source_clone.mirror_manager import MirrorManager
from source_clone.mirror_models import MirrorResult, MirrorStatus
from source_clone.models import (
    CloneResult,
    CloneStatus,
    Config,
    Project,
    ProjectState,
    RefreshStatus,
)
from source_clone.refresh_manager import RefreshManager
from source_clone.refresh_worker import EARLIER_RELEASE_REFUSAL, RefreshWorker

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path


def _git(*args: str, cwd: Path | None = None, stdin: str | None = None) -> str:
    return subprocess.run(
        [
            "git",
            "-c",
            "user.email=t@example.com",
            "-c",
            "user.name=T",
            "-c",
            "commit.gpgsign=false",
            *args,
        ],
        cwd=cwd,
        input=stdin,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()


@pytest.fixture
def upstream(tmp_path: Path) -> Path:
    """An upstream holding a file that filtering removes."""
    repo = tmp_path / "upstream"
    _git("init", "-q", "-b", "main", str(repo))
    (repo / "secret.txt").write_text("hunter2\n")
    (repo / "file.txt").write_text("one\n")
    _git("add", ".", cwd=repo)
    _git("commit", "-q", "-m", "one", cwd=repo)
    return repo


def _mirror(upstream: Path) -> Path:
    mirror = upstream.parent / "tree" / "proj.git"
    _git("clone", "-q", "--mirror", upstream.as_uri(), str(mirror))
    return mirror


def _fallback_filtered(upstream: Path) -> Path:
    """A mirror as the earlier releases' worktree fallback left it.

    ``secret.txt`` removed by a commit on the branch tip, under that
    fallback's subject; ``origin`` and its refspec kept; nothing in the
    config to say any of it happened.
    """
    mirror = _mirror(upstream)
    listing = _git("ls-tree", "main", cwd=mirror).splitlines()
    kept = "\n".join(line for line in listing if not line.endswith("\tsecret.txt"))
    tree = _git("mktree", cwd=mirror, stdin=kept + "\n")
    message = (
        f"{LEGACY_REMOVAL_SUBJECT}\n\nFiles removed by gerrit-clone content filter"
    )
    commit = _git("commit-tree", tree, "-p", "main", "-m", message, cwd=mirror)
    _git("update-ref", "refs/heads/main", commit, cwd=mirror)
    return mirror


def _filter_repo_filtered(upstream: Path) -> Path:
    """A mirror ``git filter-repo`` rewrote, its source remote put back."""
    mirror = _mirror(upstream)
    _git(
        "-C",
        str(mirror),
        "filter-repo",
        "--force",
        "--path",
        "secret.txt",
        "--invert-paths",
    )
    _git("remote", "add", "--mirror=fetch", "upstream", upstream.as_uri(), cwd=mirror)
    return mirror


def _advance(upstream: Path) -> None:
    (upstream / "file.txt").write_text("two\n")
    _git("commit", "-q", "-am", "two", cwd=upstream)


def _secret_at_tip(mirror: Path) -> bool:
    return "secret.txt" in _git("ls-tree", "--name-only", "main", cwd=mirror)


def _worker(filters: ContentFilterSpec | None = None) -> RefreshWorker:
    return RefreshWorker(
        filter_gerrit_only=False, ssh_jitter_seconds=0, content_filters=filters
    )


def _spec(mirror: Path) -> ContentFilterSpec:
    spec = ContentFilterSpec.from_options("secret.txt", None, False, mirror.parent)
    assert spec is not None
    return spec


LEGACY = {"fallback": _fallback_filtered, "filter-repo": _filter_repo_filtered}


@pytest.mark.parametrize("legacy", LEGACY.values(), ids=LEGACY.keys())
class TestUpgrade:
    """An earlier release's filtering, met by this one."""

    @pytest.mark.parametrize("with_filters", [False, True], ids=["bare", "filtered"])
    def test_a_refresh_is_refused_and_fetches_nothing(
        self, upstream: Path, legacy: Callable[[Path], Path], with_filters: bool
    ) -> None:
        """Not even with the same filters: what they were is unknown."""
        mirror = legacy(upstream)
        before = _git("for-each-ref", cwd=mirror)
        _advance(upstream)
        filters = _spec(mirror) if with_filters else None

        result = _worker(filters).refresh_repository(mirror)

        assert result.status == RefreshStatus.SKIPPED
        assert result.error_message == EARLIER_RELEASE_REFUSAL
        assert _git("for-each-ref", cwd=mirror) == before
        assert not _secret_at_tip(mirror)

    def test_a_missing_token_does_not_hide_the_lasting_refusal(
        self, upstream: Path, legacy: Callable[[Path], Path]
    ) -> None:
        """Passing the token would only meet the re-clone refusal next."""
        mirror = legacy(upstream)
        intent = FilterIntent.of_options(None, {"proj.git": ["tok-123"]}, False)
        spec = ContentFilterSpec(None, None, False, mirror.parent, intent)

        result = _worker(spec).refresh_repository(mirror)

        assert result.error_message == EARLIER_RELEASE_REFUSAL

    def test_the_dry_run_predicts_the_refusal(
        self, upstream: Path, legacy: Callable[[Path], Path]
    ) -> None:
        mirror = legacy(upstream)

        batch = RefreshManager(
            dry_run=True, filter_gerrit_only=False
        ).refresh_repositories(mirror.parent, [mirror])

        [prediction] = batch.results
        assert prediction.status == RefreshStatus.SKIPPED
        assert prediction.error_message == EARLIER_RELEASE_REFUSAL

    def test_filtering_it_again_keeps_it_refused(
        self, upstream: Path, legacy: Callable[[Path], Path]
    ) -> None:
        """Recorded first, so this release's own record cannot clear it.

        The filter finds nothing left to remove, and withdraws what it
        added; the earlier release's filtering stays written down.
        """
        mirror = legacy(upstream)

        ok, error = apply_content_filters(
            mirror, "proj", remove_patterns=["secret.txt"]
        )
        assert ok, error
        _advance(upstream)
        result = _worker(_spec(mirror)).refresh_repository(mirror)

        assert recorded_policy(mirror).earlier_release
        assert (
            _git("config", "--get", "gerrit-clone.filteredByEarlierRelease", cwd=mirror)
            == "true"
        )
        assert result.error_message == EARLIER_RELEASE_REFUSAL
        assert not _secret_at_tip(mirror)


class TestThisReleaseIsNotMistaken:
    """Its own filtering leaves the same traces, and a stamp beside them."""

    def test_its_filter_repo_rewrite_stays_refreshable(self, upstream: Path) -> None:
        mirror = _mirror(upstream)
        ok, error = apply_content_filters(
            mirror, "proj", remove_patterns=["secret.txt"]
        )
        assert ok, error
        _advance(upstream)

        result = _worker(_spec(mirror)).refresh_repository(mirror)

        assert result.status == RefreshStatus.SUCCESS, result.error_message
        assert not _secret_at_tip(mirror)

    def test_its_worktree_fallback_stays_refreshable(self, upstream: Path) -> None:
        """Through a staging copy too, though it keeps fallback tips.

        Fetching only ``main``, the copy keeps the fallback's removal
        commit at the tip of ``dev``: no earlier release's trace, since
        the mirror's own record is known.
        """
        _git("branch", "dev", cwd=upstream)
        mirror = _mirror(upstream)
        _git("config", "--unset-all", "remote.origin.fetch", cwd=mirror)
        _git(
            "config",
            "--add",
            "remote.origin.fetch",
            "+refs/heads/main:refs/heads/main",
            cwd=mirror,
        )
        with patch(
            "source_clone.content_filter._check_git_filter_repo", return_value=False
        ):
            ok, error = apply_content_filters(
                mirror, "proj", remove_patterns=["secret.txt"]
            )
            assert ok, error
            _advance(upstream)
            result = _worker(_spec(mirror)).refresh_repository(mirror)

        assert result.status == RefreshStatus.SUCCESS, result.error_message
        assert not recorded_policy(mirror).earlier_release
        assert not _secret_at_tip(mirror)

    def test_an_unfiltered_mirror_is_not_flagged(self, upstream: Path) -> None:
        assert recorded_policy(_mirror(upstream)).empty

    def test_an_analysis_alone_is_not_filtering(self, upstream: Path) -> None:
        """``filter-repo --analyze`` leaves its directory, and every ref."""
        mirror = _mirror(upstream)
        _git("-C", str(mirror), "filter-repo", "--analyze")
        _advance(upstream)

        result = _worker().refresh_repository(mirror)

        assert recorded_policy(mirror).empty
        assert result.status == RefreshStatus.SUCCESS, result.error_message


@dataclass
class _Overwritten:
    """What one ``mirror --overwrite`` run did."""

    pushed: list[str]
    deleted: list[str]
    created: list[str]
    results: list[MirrorResult]


def _overwrite(
    tree: Path, name: str, upstream: Path, *, recreate: bool = False
) -> _Overwritten:
    """``mirror --overwrite`` of project *name*, unfiltered.

    Its GitHub repository already exists, so ``--recreate`` would delete
    and recreate it.
    """
    pushed: list[str] = []

    def clone(self: Any, projects: list[Project]) -> list[CloneResult]:
        results = []
        for project in projects:
            path = self.config.path / project.name
            status = CloneStatus.ALREADY_EXISTS
            if not path.exists():
                _git("clone", "-q", "--mirror", upstream.as_uri(), str(path))
                status = CloneStatus.SUCCESS
            results.append(CloneResult(project=project, status=status, path=path))
        return results

    def push(_self: Any, local: Path, _repo: Any) -> tuple[bool, None]:
        pushed.append(str(local))
        return True, None

    github_name = transform_gerrit_name_to_github(name)
    existing: dict[str, Any] = {
        "name": github_name,
        "full_name": f"org/{github_name}",
        "html_url": f"https://github.com/org/{github_name}",
        "clone_url": f"https://github.com/org/{github_name}.git",
        "ssh_url": f"git@github.com:org/{github_name}.git",
        "private": False,
    }
    repo = GitHubRepo(
        name=github_name,
        full_name=f"org/{github_name}",
        ssh_url=f"git@github.com:org/{github_name}.git",
        clone_url=f"https://github.com/org/{github_name}.git",
        html_url=f"https://github.com/org/{github_name}",
        private=False,
    )
    api = Mock()
    api.list_all_repos_graphql = Mock(return_value={github_name: existing})
    api.list_repos = Mock(return_value=[])
    api.batch_delete_repos = AsyncMock(return_value={})
    api.batch_create_repos = AsyncMock(return_value={github_name: (repo, None)})
    with (
        patch("source_clone.clone_orchestrator.CloneManager.clone_projects", clone),
        patch.object(MirrorManager, "_push_to_github", push),
    ):
        results = MirrorManager(
            config=Config(host="gerrit.example.org", port=29418, path=tree),
            github_api=api,
            github_org="org",
            overwrite=True,
            recreate=recreate,
        ).mirror_projects([Project(name, ProjectState.ACTIVE)])
    return _Overwritten(
        pushed,
        [n for call in api.batch_delete_repos.call_args_list for n in call.args[1]],
        [
            c["name"]
            for call in api.batch_create_repos.call_args_list
            for c in call.args[1]
        ],
        results,
    )


@pytest.mark.parametrize("legacy", LEGACY.values(), ids=LEGACY.keys())
class TestOverwrite:
    """``mirror --overwrite`` leaves what an earlier release filtered alone."""

    @pytest.mark.parametrize("recreate", [False, True], ids=["reuse", "recreate"])
    def test_the_repository_is_kept_and_never_published(
        self, upstream: Path, legacy: Callable[[Path], Path], recreate: bool
    ) -> None:
        """Cloned again it would go out unfiltered, and with --recreate even
        the kept copy would replace its GitHub repository."""
        mirror = legacy(upstream)
        before = _git("for-each-ref", cwd=mirror)

        run = _overwrite(mirror.parent, "proj.git", upstream, recreate=recreate)

        assert run.pushed == []
        assert run.deleted == []
        assert run.created == []
        [result] = run.results
        assert result.status == MirrorStatus.SKIPPED
        assert "earlier gerrit-clone release" in (result.error_message or "")
        assert _git("for-each-ref", cwd=mirror) == before
        assert not _secret_at_tip(mirror)

    def test_a_directory_holding_one_is_kept(
        self, upstream: Path, legacy: Callable[[Path], Path]
    ) -> None:
        """Deleting the parent would take the nested repository with it."""
        nested = legacy(upstream)
        parent = nested.parent / "com"
        _git("clone", "-q", "--mirror", upstream.as_uri(), str(parent))
        inner = parent / "proj.git"
        nested.rename(inner)

        run = _overwrite(nested.parent, "com", upstream, recreate=True)

        assert inner.is_dir()
        assert not _secret_at_tip(inner)
        assert run.pushed == []


def test_unreadable_traces_fail_closed(upstream: Path) -> None:
    """Not a guess of "never filtered" when git cannot answer."""
    mirror = _mirror(upstream)

    with (
        patch.object(content_legacy, "git", return_value=None),
        pytest.raises(PolicyReadError),
    ):
        recorded_policy(mirror)
