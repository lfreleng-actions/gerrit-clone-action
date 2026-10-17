# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""Filtering a working copy ``clone --no-mirror`` has just made.

It used to be filtered where it stood: ``git filter-repo`` folded its
remote-tracking refs into local branches, and no later refresh could
follow it.  It is filtered through a staged copy instead, keeps its
remote-tracking refs and branch tracking, and holds no unfiltered object
afterwards -- through its tags or its reflogs either.  One whose
filtering fails is deleted, never left unfiltered.
"""

from __future__ import annotations

import shutil
import subprocess
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any
from unittest.mock import patch

import pytest
from typer.testing import CliRunner

from gerrit_clone import refresh_checkout_stage
from gerrit_clone.cli import app
from gerrit_clone.cli_clone_run import _delete_working_copy
from gerrit_clone.content_origin import NO_PUSH_URL
from gerrit_clone.content_policy import stored_policy
from gerrit_clone.content_spec import ContentFilterSpec
from gerrit_clone.content_stage import SHALLOW_HISTORY_REFUSED
from gerrit_clone.models import (
    CloneResult,
    CloneStatus,
    Project,
    ProjectState,
    RefreshResult,
    RefreshStatus,
)
from gerrit_clone.refresh_checkout_fresh import NO_UPSTREAM_REFUSAL
from gerrit_clone.refresh_worker import RefreshWorker

if TYPE_CHECKING:
    from pathlib import Path

    from gerrit_clone.models import Config

#: A GitLab token shape ``--redact-secrets`` finds, built at runtime so
#: that no credential-shaped literal sits in the source for scanners.
TOKEN = "glpat-" + "x1Y2z3" * 3 + "Ab9C"

#: What the secret file holds.
SECRET = "hunter2-" + "0a1b2c3d" * 2

needs_filter_repo = pytest.mark.skipif(
    shutil.which("git-filter-repo") is None,
    reason="git filter-repo is not installed",
)


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


def _commit(repo: Path, name: str, content: str) -> str:
    (repo / name).write_text(content)
    _git("add", name, cwd=repo)
    _git("commit", "-q", "-m", f"add {name}", cwd=repo)
    return _git("rev-parse", "HEAD", cwd=repo)


@pytest.fixture
def upstream(tmp_path: Path) -> Path:
    """Upstream with a secret file, a token, and tags on both."""
    path = tmp_path / "up"
    path.mkdir()
    _git("init", "-q", "-b", "main", str(path))
    _commit(path, "file.txt", "one\n")
    _commit(path, "secret.txt", f"{SECRET}\n")
    _commit(path, "config.txt", f"token = {TOKEN}\n")
    _git("tag", "light", cwd=path)
    _git("tag", "-a", "annotated", "-m", "release", cwd=path)
    _commit(path, "file.txt", "two\n")
    return path


@pytest.fixture
def tree(tmp_path: Path) -> Path:
    return tmp_path / "tree"


def _cloning_from(upstream: Path) -> Any:
    """``clone_projects``, cloning each project from *upstream* for real."""

    def clone(self: Any, projects: list[Project]) -> list[CloneResult]:
        config: Config = self.config
        results = []
        for project in projects:
            path = config.path / project.name
            options = ["--mirror"] if config.mirror else []
            if config.depth:
                options += ["--depth", str(config.depth)]
            if config.branch:
                # As the GitHub backend clones a named branch or tag.
                options += ["--single-branch", "--branch", config.branch]
            _git("clone", "-q", *options, upstream.as_uri(), str(path))
            results.append(
                CloneResult(project=project, status=CloneStatus.SUCCESS, path=path)
            )
        return results

    return clone


def _clone(upstream: Path, tree: Path, *options: str, mirror: bool = False) -> Any:
    projects = [Project("proj", ProjectState.ACTIVE)]
    with (
        patch(
            "gerrit_clone.clone_manager.discover_projects",
            return_value=(projects, {"skipped": 0}),
        ),
        patch(
            "gerrit_clone.clone_orchestrator.CloneManager.clone_projects",
            _cloning_from(upstream),
        ),
    ):
        return CliRunner().invoke(
            app,
            [
                "clone",
                "--host",
                "gerrit.example.org",
                "--output-path",
                str(tree),
                "--mirror" if mirror else "--no-mirror",
                "--disable-log-file",
                *options,
            ],
        )


def _refresh(tree: Path, *options: str) -> Any:
    return CliRunner().invoke(
        app, ["refresh", "--output-path", str(tree), "--all-repos", *options]
    )


def _history(repo: Path) -> list[str]:
    return _git("log", "--all", "--name-only", "--format=", cwd=repo).split()


def _blobs(repo: Path) -> str:
    """Every blob in *repo*, reachable or not, concatenated."""
    listed = _git(
        "cat-file", "--batch-all-objects", "--batch-check", cwd=repo
    ).splitlines()
    return "\n".join(
        _git("cat-file", "-p", sha, cwd=repo)
        for sha, kind, _size in (line.split() for line in listed)
        if kind == "blob"
    )


def _stages_left(checkout: Path) -> list[Path]:
    return list((checkout / ".git").glob("gerrit-clone-stage-*"))


def _failing(*_args: object, **_kwargs: object) -> tuple[bool, str | None]:
    return False, "filter-repo failed"


@needs_filter_repo
class TestRemovedFiles:
    def test_it_keeps_tracking_its_upstream(self, upstream: Path, tree: Path) -> None:
        result = _clone(upstream, tree, "--remove-files", "secret.txt")

        assert result.exit_code == 0, result.output
        checkout = tree / "proj"
        assert _git("rev-parse", "--abbrev-ref", "@{upstream}", cwd=checkout) == (
            "origin/main"
        )
        assert _git(
            "for-each-ref", "--format=%(refname)", "refs/heads", cwd=checkout
        ) == ("refs/heads/main")
        assert not (checkout / "secret.txt").exists()
        assert "secret.txt" not in _history(checkout)
        assert (checkout / "file.txt").read_text() == "two\n"
        assert not stored_policy(checkout).empty
        assert _git("config", "remote.origin.pushurl", cwd=checkout) == NO_PUSH_URL

    def test_no_object_holds_the_removed_file(self, upstream: Path, tree: Path) -> None:
        result = _clone(upstream, tree, "--remove-files", "secret.txt")

        assert result.exit_code == 0, result.output
        checkout = tree / "proj"
        assert SECRET not in _blobs(checkout)
        for tag in ("light", "annotated"):
            shown = subprocess.run(
                ["git", "cat-file", "-p", f"{tag}:secret.txt"],
                cwd=checkout,
                capture_output=True,
                check=False,
            )
            assert shown.returncode != 0
        assert _stages_left(checkout) == []

    def test_a_later_refresh_follows_it(self, upstream: Path, tree: Path) -> None:
        assert _clone(upstream, tree, "--remove-files", "secret.txt").exit_code == 0
        checkout = tree / "proj"
        _commit(upstream, "file.txt", "three\n")
        _commit(upstream, "secret.txt", "again\n")

        result = _refresh(tree, "--remove-files", "secret.txt")

        assert result.exit_code == 0, result.output
        assert (checkout / "file.txt").read_text() == "three\n"
        assert "secret.txt" not in _history(checkout)
        assert not (checkout / "secret.txt").exists()


@needs_filter_repo
class TestReplacedTokens:
    @pytest.mark.parametrize(
        "options",
        [("--git-filter", f"proj:{TOKEN}"), ("--redact-secrets",)],
        ids=["git-filter", "redact-secrets"],
    )
    def test_no_object_holds_the_token(
        self, upstream: Path, tree: Path, options: tuple[str, ...]
    ) -> None:
        result = _clone(upstream, tree, *options)

        assert result.exit_code == 0, result.output
        checkout = tree / "proj"
        assert TOKEN not in _blobs(checkout)
        assert _git("rev-parse", "--abbrev-ref", "@{upstream}", cwd=checkout) == (
            "origin/main"
        )
        assert _stages_left(checkout) == []


@needs_filter_repo
class TestMatchingNothing:
    def test_nothing_is_recorded_or_rewritten(
        self, upstream: Path, tree: Path, tmp_path: Path
    ) -> None:
        plain = tmp_path / "plain"
        _git("clone", "-q", upstream.as_uri(), str(plain))

        result = _clone(upstream, tree, "--remove-files", "nomatch.txt")

        assert result.exit_code == 0, result.output
        checkout = tree / "proj"
        assert _git("for-each-ref", cwd=checkout) == _git("for-each-ref", cwd=plain)
        assert stored_policy(checkout).empty
        push_urls = subprocess.run(
            ["git", "config", "--get-all", "remote.origin.pushurl"],
            cwd=checkout,
            capture_output=True,
            text=True,
            check=False,
        )
        assert NO_PUSH_URL not in push_urls.stdout
        assert _git("rev-parse", "HEAD", cwd=checkout) == _git(
            "rev-parse", "HEAD", cwd=upstream
        )
        assert _git("rev-parse", "--abbrev-ref", "@{upstream}", cwd=checkout) == (
            "origin/main"
        )


class TestFailureDeletes:
    def test_a_shallow_checkout_refused_history_filters(
        self, upstream: Path, tree: Path
    ) -> None:
        result = _clone(upstream, tree, "--depth", "1", "--redact-secrets")

        assert result.exit_code != 0
        output = " ".join(result.output.split())
        assert SHALLOW_HISTORY_REFUSED in output
        assert "working copy was deleted" in output
        assert not (tree / "proj").exists()

    def test_a_filter_that_fails(self, upstream: Path, tree: Path) -> None:
        with patch.object(refresh_checkout_stage, "apply_content_filters", _failing):
            result = _clone(upstream, tree, "--remove-files", "secret.txt")

        assert result.exit_code != 0
        output = " ".join(result.output.split())
        assert "filter-repo failed" in output
        assert "working copy was deleted" in output
        assert not (tree / "proj").exists()

    @needs_filter_repo
    def test_a_copy_that_cannot_be_removed(self, upstream: Path, tree: Path) -> None:
        """It may hold unfiltered history: the project fails, naming it."""
        with patch(
            "gerrit_clone.refresh_filtered.shutil.rmtree",
            side_effect=OSError("busy"),
        ):
            result = _clone(upstream, tree, "--remove-files", "secret.txt")

        assert result.exit_code != 0
        output = " ".join(result.output.split())
        assert "Could not remove the staging copy" in output
        assert "working copy was deleted" in output
        assert not (tree / "proj").exists()

    def test_an_interrupted_filter(self, upstream: Path, tree: Path) -> None:
        with patch.object(
            refresh_checkout_stage,
            "apply_content_filters",
            side_effect=KeyboardInterrupt,
        ):
            result = _clone(upstream, tree, "--remove-files", "secret.txt")

        assert result.exit_code != 0
        assert not (tree / "proj").exists()

    @needs_filter_repo
    def test_a_branch_tracking_nothing(self, upstream: Path, tmp_path: Path) -> None:
        """It could not be reset to the filtered history."""
        checkout = tmp_path / "tree" / "proj"
        _git("clone", "-q", upstream.as_uri(), str(checkout))
        _git("branch", "--unset-upstream", cwd=checkout)
        worker = RefreshWorker(
            timeout=60,
            filter_gerrit_only=False,
            content_filters=ContentFilterSpec(
                ["secret.txt"], None, False, checkout.parent
            ),
        )
        result = RefreshResult(
            path=checkout,
            project_name="proj",
            status=RefreshStatus.REFRESHING,
            started_at=datetime.now(UTC),
        )

        assert not worker._filter_fresh_checkout(checkout, result)
        assert result.error_message == NO_UPSTREAM_REFUSAL
        assert _stages_left(checkout) == []

    def test_repositories_nested_in_it_are_kept(self, tmp_path: Path) -> None:
        parent = tmp_path / "parent"
        (parent / ".git").mkdir(parents=True)
        (parent / "file.txt").write_text("unfiltered\n")
        (parent / "sub" / "child" / ".git").mkdir(parents=True)
        (parent / "sub" / "other.txt").write_text("unfiltered\n")

        assert _delete_working_copy(parent) is None
        assert sorted(str(path.relative_to(parent)) for path in parent.rglob("*")) == [
            "sub",
            "sub/child",
            "sub/child/.git",
        ]


@needs_filter_repo
class TestUnchanged:
    def test_a_mirror_is_filtered_in_place(self, upstream: Path, tree: Path) -> None:
        result = _clone(upstream, tree, "--remove-files", "secret.txt", mirror=True)

        assert result.exit_code == 0, result.output
        mirror = tree / "proj"
        assert "secret.txt" not in _history(mirror)
        assert not stored_policy(mirror).empty

    def test_a_working_copy_without_filters_is_untouched(
        self, upstream: Path, tree: Path
    ) -> None:
        result = _clone(upstream, tree)

        assert result.exit_code == 0, result.output
        checkout = tree / "proj"
        assert "secret.txt" in _history(checkout)
        assert stored_policy(checkout).empty

    def test_an_existing_working_copy_is_refreshed_staged(
        self, tmp_path: Path, tree: Path
    ) -> None:
        """Content arriving later is filtered by its staged refresh."""
        upstream = tmp_path / "plain"
        upstream.mkdir()
        _git("init", "-q", "-b", "main", str(upstream))
        _commit(upstream, "file.txt", "one\n")
        checkout = tree / "proj"
        _git("clone", "-q", upstream.as_uri(), str(checkout))
        _commit(upstream, "secret.txt", f"{SECRET}\n")
        _commit(upstream, "file.txt", "two\n")

        result = _clone(upstream, tree, "--remove-files", "secret.txt")

        assert result.exit_code == 0, result.output
        assert (checkout / "file.txt").read_text() == "two\n"
        assert "secret.txt" not in _history(checkout)
        assert _git("rev-parse", "--abbrev-ref", "@{upstream}", cwd=checkout) == (
            "origin/main"
        )


@needs_filter_repo
class TestTagsTheFiltersRemove:
    """A tag on a commit holding only removed content goes with it."""

    def test_no_tag_keeps_the_removed_content(self, tmp_path: Path, tree: Path) -> None:
        upstream = tmp_path / "rooted"
        upstream.mkdir()
        _git("init", "-q", "-b", "main", str(upstream))
        _commit(upstream, "secret.txt", f"{SECRET}\n")  # The root: secret alone.
        _git("tag", "root-light", cwd=upstream)
        _git("tag", "-a", "root-annotated", "-m", "root", cwd=upstream)
        _commit(upstream, "file.txt", "one\n")

        result = _clone(upstream, tree, "--remove-files", "secret.txt")

        assert result.exit_code == 0, result.output
        checkout = tree / "proj"
        assert _git("tag", cwd=checkout) == ""
        assert SECRET not in _blobs(checkout)
        assert "secret.txt" not in _history(checkout)
        assert _stages_left(checkout) == []


@needs_filter_repo
class TestSingleTagClones:
    """A clone of one tag: detached, fetching that tag alone."""

    def test_a_filter_matching_nothing_keeps_it(
        self, upstream: Path, tree: Path
    ) -> None:
        result = _clone(
            upstream, tree, "--branch", "light", "--remove-files", "absent.txt"
        )

        assert result.exit_code == 0, result.output
        checkout = tree / "proj"
        assert _git("rev-parse", "HEAD", cwd=checkout) == _git(
            "rev-parse", "light^{commit}", cwd=upstream
        )
        assert stored_policy(checkout).empty

    def test_head_follows_its_filtered_tag(self, upstream: Path, tree: Path) -> None:
        result = _clone(
            upstream, tree, "--branch", "light", "--remove-files", "secret.txt"
        )

        assert result.exit_code == 0, result.output
        checkout = tree / "proj"
        assert _git("rev-parse", "HEAD", cwd=checkout) == _git(
            "rev-parse", "light^{commit}", cwd=checkout
        )
        assert not (checkout / "secret.txt").exists()
        assert (checkout / "config.txt").exists()
        assert SECRET not in _blobs(checkout)
        assert not stored_policy(checkout).empty
        assert _stages_left(checkout) == []


class TestInterruptedDeletion:
    def test_a_copy_left_behind_is_reported(self, upstream: Path, tree: Path) -> None:
        """An interrupt whose deletion fails names the checkout to delete."""
        with (
            patch.object(
                RefreshWorker, "_filter_fresh_checkout", side_effect=KeyboardInterrupt
            ),
            patch(
                "gerrit_clone.cli_clone_run._delete_working_copy",
                return_value="permission denied",
            ),
            patch("gerrit_clone.cli_clone_run.logger") as logger,
        ):
            result = _clone(upstream, tree, "--remove-files", "secret.txt")

        assert result.exit_code == 130, result.output
        logger.error.assert_called_once()
        logged = logger.error.call_args.args
        assert tree / "proj" in logged
        assert "permission denied" in logged
