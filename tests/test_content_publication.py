# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""A run publishes nothing filtered under less than the tree now decides.

Another run can extend the tree's intent while this one filters.  Its
result then goes out only if this run's filters still cover the intent,
checked under the intent lock, which it holds until publishing ends.  And
nothing under ``.gerrit-clone/`` is written through a symbolic link a
checkout could track to aim it outside the tree.
"""

from __future__ import annotations

import subprocess
import sys
from contextlib import contextmanager
from typing import TYPE_CHECKING, Any
from unittest.mock import patch

import pytest

from gerrit_clone import content_stage, refresh_filtered
from gerrit_clone.content_intent import IntentError, project_locked
from gerrit_clone.content_intent_resolve import resolve_filters
from gerrit_clone.content_spec import ContentFilterSpec
from gerrit_clone.content_stage import (
    STALE_INTENT_REFUSAL,
    UNJOURNALLED_REFUSAL,
    filter_repository,
)
from gerrit_clone.models import RefreshStatus, RetryPolicy
from gerrit_clone.refresh_filtered import OVERTAKEN_REFUSAL
from gerrit_clone.refresh_worker import RefreshWorker

if TYPE_CHECKING:
    from collections.abc import Generator
    from pathlib import Path


def _git(*args: str, cwd: Path | None = None) -> str:
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
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()


def _commit(repo: Path, name: str, content: str) -> None:
    (repo / name).write_text(content)
    _git("add", name, cwd=repo)
    _git("commit", "-q", "-m", name, cwd=repo)


@pytest.fixture
def upstream(tmp_path: Path) -> Path:
    path = tmp_path / "up"
    path.mkdir()
    _git("init", "-q", "-b", "main", str(path))
    _commit(path, "file.txt", "one\n")
    return path


@pytest.fixture
def tree(tmp_path: Path) -> Path:
    path = tmp_path / "tree"
    path.mkdir()
    return path


def _resolved(tree: Path, pattern: str) -> ContentFilterSpec:
    options = ContentFilterSpec([pattern], None, False, tree)
    spec = resolve_filters(tree, options, persist=True, command="refresh")
    assert spec is not None
    return spec


def _worker(filters: ContentFilterSpec) -> RefreshWorker:
    return RefreshWorker(
        retry_policy=RetryPolicy(max_attempts=1),
        timeout=60,
        filter_gerrit_only=False,
        ssh_jitter_seconds=0,
        content_filters=filters,
    )


def _refs(repo: Path) -> str:
    return _git("for-each-ref", "--format=%(refname) %(objectname)", cwd=repo)


class TestAStaleRun:
    """This run resolved ``a.txt``; another then added ``b.txt``."""

    def test_its_refreshed_mirror_is_not_published(
        self, tree: Path, upstream: Path
    ) -> None:
        mirror = tree / "proj"
        _git("clone", "-q", "--mirror", upstream.as_uri(), str(mirror))
        stale = _resolved(tree, "a.txt")
        _resolved(tree, "b.txt")
        before = _refs(mirror)
        _commit(upstream, "file.txt", "two\n")

        result = _worker(stale).refresh_repository(mirror)

        assert result.status == RefreshStatus.FAILED
        assert STALE_INTENT_REFUSAL in (result.error_message or "")
        assert _refs(mirror) == before

    def test_its_refreshed_working_copy_is_not_published(
        self, tree: Path, upstream: Path
    ) -> None:
        checkout = tree / "proj"
        _git("clone", "-q", upstream.as_uri(), str(checkout))
        stale = _resolved(tree, "a.txt")
        _resolved(tree, "b.txt")
        before = _refs(checkout)
        _commit(upstream, "file.txt", "two\n")

        result = _worker(stale).refresh_repository(checkout)

        assert result.status == RefreshStatus.FAILED
        assert STALE_INTENT_REFUSAL in (result.error_message or "")
        assert _refs(checkout) == before

    def test_its_filtering_in_place_fails(self, tree: Path, upstream: Path) -> None:
        """Clone and mirror publish what they filtered in place."""
        mirror = tree / "proj"
        _git("clone", "-q", "--mirror", upstream.as_uri(), str(mirror))
        stale = _resolved(tree, "a.txt")
        _resolved(tree, "b.txt")

        assert filter_repository(stale, mirror, "proj", 60) == STALE_INTENT_REFUSAL

    def test_one_covering_the_addition_goes_ahead(
        self, tree: Path, upstream: Path
    ) -> None:
        mirror = tree / "proj"
        _git("clone", "-q", "--mirror", upstream.as_uri(), str(mirror))
        spec = _resolved(tree, "a.txt")
        _resolved(tree, "a.txt")

        assert filter_repository(spec, mirror, "proj", 60) is None


@pytest.mark.skipif(sys.platform == "win32", reason="symbolic links need privileges")
class TestSymbolicLinks:
    def test_a_linked_directory_is_not_written_through(
        self, tree: Path, tmp_path: Path
    ) -> None:
        elsewhere = tmp_path / "elsewhere"
        elsewhere.mkdir()
        (tree / ".gerrit-clone").symlink_to(elsewhere)

        with pytest.raises(IntentError, match="symbolic link"):
            _resolved(tree, "a.txt")

        assert list(elsewhere.iterdir()) == []

    def test_a_linked_lock_is_not_written_through(
        self, tree: Path, tmp_path: Path
    ) -> None:
        target = tmp_path / "target.txt"
        target.write_text("keep\n")
        (tree / ".gerrit-clone").mkdir()
        (tree / ".gerrit-clone" / "filter-policy.lock").symlink_to(target)

        with pytest.raises(IntentError):
            _resolved(tree, "a.txt")

        assert target.read_text() == "keep\n"

    def test_a_linked_lock_is_refused_where_links_are_followed(
        self, tree: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Without O_NOFOLLOW, as on Windows, the link is checked for."""
        monkeypatch.setattr("gerrit_clone.content_intent.NO_FOLLOW", 0)
        target = tmp_path / "target.txt"
        target.write_text("keep\n")
        (tree / ".gerrit-clone").mkdir()
        (tree / ".gerrit-clone" / "filter-policy.lock").symlink_to(target)

        with pytest.raises(IntentError, match="symbolic link"):
            _resolved(tree, "a.txt")

        assert target.read_text() == "keep\n"

    def test_a_linked_journal_is_not_written_through(
        self, tree: Path, tmp_path: Path, upstream: Path
    ) -> None:
        mirror = tree / "proj"
        _git("clone", "-q", "--mirror", upstream.as_uri(), str(mirror))
        spec = _resolved(tree, "a.txt")
        target = tmp_path / "target.txt"
        target.write_text("keep")
        (tree / ".gerrit-clone" / "filter-journal.jsonl").symlink_to(target)

        assert filter_repository(spec, mirror, "proj", 60) == UNJOURNALLED_REFUSAL
        assert target.read_text() == "keep"


def _config(repo: Path) -> list[str]:
    """Its local config bar the stamp, which content_policy never withdraws."""
    listed = _git("config", "--list", "--local", cwd=repo).splitlines()
    return [
        line
        for line in listed
        if not line.startswith("gerrit-clone.filterpolicyrecorded")
    ]


class TestAFailedPublication:
    """Recording and blocking are undone when the refs are not published."""

    def _publish_failing(self, repo: Path, ref: str) -> None:
        lock = repo / (".git" if (repo / ".git").is_dir() else "") / f"{ref}.lock"
        lock.parent.mkdir(parents=True, exist_ok=True)
        lock.write_text("")

    def test_a_mirror_is_left_unrecorded_and_pushable(
        self, tree: Path, upstream: Path
    ) -> None:
        mirror = tree / "proj"
        _git("clone", "-q", "--mirror", upstream.as_uri(), str(mirror))
        spec = _resolved(tree, "secret.txt")
        _commit(upstream, "secret.txt", "hunter2\n")
        _commit(upstream, "file.txt", "two\n")
        config = _config(mirror)
        self._publish_failing(mirror, "refs/heads/main")

        result = _worker(spec).refresh_repository(mirror)

        assert result.status == RefreshStatus.FAILED
        assert _config(mirror) == config

    def test_a_working_copy_is_left_unrecorded_and_pushable(
        self, tree: Path, upstream: Path
    ) -> None:
        checkout = tree / "proj"
        _git("clone", "-q", upstream.as_uri(), str(checkout))
        spec = _resolved(tree, "secret.txt")
        _commit(upstream, "secret.txt", "hunter2\n")
        _commit(upstream, "file.txt", "two\n")
        config = _config(checkout)
        self._publish_failing(checkout, "refs/remotes/origin/main")

        result = _worker(spec).refresh_repository(checkout)

        assert result.status == RefreshStatus.FAILED
        assert _config(checkout) == config


class TestOneRewriteAtATime:
    """Each rewrite holds its project's lock from journal start to end."""

    def test_a_rewrite_waits_for_one_of_the_same_project(
        self, tree: Path, upstream: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        mirror = tree / "proj"
        _git("clone", "-q", "--mirror", upstream.as_uri(), str(mirror))
        spec = _resolved(tree, "a.txt")
        assert spec.journal is not None
        monkeypatch.setattr("gerrit_clone.content_stage.LOCK_WAIT", 0.3)
        applied: list[str] = []

        def apply(*_args: object, **_kwargs: object) -> tuple[bool, str | None]:
            applied.append("proj")
            return True, None

        with project_locked(spec.journal.root, "proj", 1):
            reason = filter_repository(spec, mirror, "proj", 0, apply=apply)

        assert reason is not None
        assert "another gerrit-clone run" in reason
        assert applied == []
        assert filter_repository(spec, mirror, "proj", 0, apply=apply) is None
        assert applied == ["proj"]

    def test_another_project_does_not_wait(self, tree: Path, upstream: Path) -> None:
        mirror = tree / "proj"
        _git("clone", "-q", "--mirror", upstream.as_uri(), str(mirror))
        spec = _resolved(tree, "a.txt")
        assert spec.journal is not None

        with project_locked(spec.journal.root, "other", 1):
            reason = filter_repository(
                spec, mirror, "proj", 0, apply=lambda *_a, **_k: (True, None)
            )

        assert reason is None


class TestAnOvertakenMirror:
    def test_its_copy_is_not_published_over_a_newer_refresh(
        self, tree: Path, upstream: Path
    ) -> None:
        """Forced over refs another run moved, the copy would roll them back."""
        mirror = tree / "proj"
        _git("clone", "-q", "--mirror", upstream.as_uri(), str(mirror))
        spec = _resolved(tree, "a.txt")
        _commit(upstream, "file.txt", "two\n")
        real = content_stage.publishing

        @contextmanager
        def overtaken(*args: Any) -> Generator[str | None, None, None]:
            _commit(upstream, "file.txt", "three\n")
            _git("fetch", "-q", "origin", "+refs/heads/*:refs/heads/*", cwd=mirror)
            with real(*args) as stale:
                yield stale

        with patch.object(refresh_filtered, "publishing", overtaken):
            result = _worker(spec).refresh_repository(mirror)

        assert result.status == RefreshStatus.FAILED
        assert result.error_message == OVERTAKEN_REFUSAL
        assert _git("rev-parse", "main", cwd=mirror) == _git(
            "rev-parse", "main", cwd=upstream
        )
