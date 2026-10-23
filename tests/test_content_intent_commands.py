# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""Later runs filter a tree as the runs before them decided.

Each test filters a tree once, the way an operator would, and then runs
a command again *without* the filter options: what was removed stays
removed, in repositories the filter left alone the first time, in ones
deleted and cloned again, and in trees an earlier release filtered.
"""

from __future__ import annotations

import subprocess
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, Mock, patch

import pytest
import typer
from typer.testing import CliRunner

from source_clone.cli import app
from source_clone.cli_clone_run import _apply_content_filters
from source_clone.clone_manager import _refresh_repositories
from source_clone.content_filter import apply_content_filters
from source_clone.content_intent import load_intent
from source_clone.content_intent_resolve import gathered_intent
from source_clone.content_spec import ContentFilterSpec
from source_clone.github_api import GitHubRepo
from source_clone.mirror_manager import MirrorManager
from source_clone.models import (
    BatchResult,
    CloneResult,
    CloneStatus,
    Config,
    Project,
    ProjectState,
)

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

#: Built at runtime so that no credential-shaped literal sits in the
#: source for secret scanners to flag.
TOKEN = "tok-" + "1a2b3c4d5e6f" * 2


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


def _commit(upstream: Path, name: str, content: str) -> None:
    (upstream / name).write_text(content)
    _git("add", name, cwd=upstream)
    _git("commit", "-q", "-m", f"add {name}", cwd=upstream)


def _upstream(path: Path) -> Path:
    path.mkdir(parents=True)
    _git("init", "-q", "-b", "main", str(path))
    _commit(path, "file.txt", "one\n")
    return path


def _history(repo: Path) -> list[str]:
    return _git("log", "--all", "--name-only", "--format=", cwd=repo).split()


def _contents(repo: Path) -> str:
    return _git("log", "--all", "-p", cwd=repo)


@pytest.fixture
def tree(tmp_path: Path) -> Path:
    """Mirrors ``com/parent`` and ``com/child`` of two upstreams."""
    base = tmp_path / "tree"
    for name in ("parent", "child"):
        upstream = _upstream(tmp_path / "up" / name)
        _git("clone", "-q", "--mirror", upstream.as_uri(), str(base / "com" / name))
    return base


def _refresh(tree: Path, *options: str) -> Any:
    return CliRunner().invoke(
        app, ["refresh", "--output-path", str(tree), "--all-repos", *options]
    )


class TestRefresh:
    def test_content_arriving_later_is_filtered_without_the_options(
        self, tree: Path
    ) -> None:
        """The child had no secret.txt when the tree was filtered."""
        first = _refresh(tree, "--remove-files", "secret.txt")
        assert first.exit_code == 0, first.output
        _commit(tree.parent / "up" / "child", "secret.txt", "hunter2\n")

        again = _refresh(tree)

        assert again.exit_code == 0, again.output
        assert "secret.txt" not in _history(tree / "com" / "child")

    def test_a_tree_filtered_by_an_earlier_release_is_migrated(
        self, tree: Path
    ) -> None:
        """Per-repository records alone, as the previous release left them."""
        parent = tree.parent / "up" / "parent"
        _commit(parent, "secret.txt", "hunter2\n")
        assert _refresh(tree).exit_code == 0
        ok, error = apply_content_filters(
            tree / "com" / "parent", "com/parent", remove_patterns=["secret.txt"]
        )
        assert ok, error
        _commit(parent, "file.txt", "two\n")

        again = _refresh(tree)

        assert again.exit_code == 0, again.output
        mirror = tree / "com" / "parent"
        assert _git("show", "main:file.txt", cwd=mirror) == "two"
        assert "secret.txt" not in _history(mirror)

    def test_a_token_the_run_does_not_supply_refuses_the_project(
        self, tree: Path
    ) -> None:
        """Kept only as a digest, it cannot be replaced in what arrives."""
        first = _refresh(tree, "--git-filter", f"com/child:{TOKEN}")
        assert first.exit_code == 0, first.output
        _commit(tree.parent / "up" / "child", "settings.py", f'KEY = "{TOKEN}"\n')

        again = _refresh(tree)

        assert "--git-filter token" in again.output
        assert TOKEN not in _contents(tree / "com" / "child")

    def test_the_token_given_again_lets_it_through_replaced(self, tree: Path) -> None:
        first = _refresh(tree, "--git-filter", f"com/child:{TOKEN}")
        assert first.exit_code == 0, first.output
        _commit(tree.parent / "up" / "child", "settings.py", f'KEY = "{TOKEN}"\n')

        again = _refresh(tree, "--git-filter", f"com/child:{TOKEN}")

        assert again.exit_code == 0, again.output
        assert "settings.py" in _history(tree / "com" / "child")
        assert TOKEN not in _contents(tree / "com" / "child")

    def test_an_unreadable_intent_refreshes_nothing(self, tree: Path) -> None:
        (tree / ".gerrit-clone").mkdir()
        (tree / ".gerrit-clone" / "filter-policy.json").write_text("{not json")
        before = _git("rev-parse", "main", cwd=tree / "com" / "child")
        _commit(tree.parent / "up" / "child", "new.txt", "new\n")

        result = _refresh(tree)

        assert result.exit_code == 2, result.output
        assert _git("rev-parse", "main", cwd=tree / "com" / "child") == before

    def test_a_mirror_the_intent_covers_is_left_alone_when_filtering_fails(
        self, tree: Path
    ) -> None:
        """Staged like one the filters rewrote, never fetched in place.

        The child had no secret.txt when the tree was filtered, so it
        holds no record of its own -- only the tree's intent covers it.
        """
        first = _refresh(tree, "--remove-files", "secret.txt")
        assert first.exit_code == 0, first.output
        child = tree / "com" / "child"
        before = _git("for-each-ref", cwd=child)
        _commit(tree.parent / "up" / "child", "secret.txt", "hunter2\n")

        with patch(
            "source_clone.refresh_filtered.apply_content_filters",
            return_value=(False, "filter-repo failed"),
        ):
            again = _refresh(tree)

        assert again.exit_code != 0, again.output
        assert _git("for-each-ref", cwd=child) == before
        assert "secret.txt" not in _history(child)


def _github_api(names: list[str]) -> Mock:
    api = Mock()
    api.list_all_repos_graphql = Mock(return_value={})
    api.list_repos = Mock(return_value=[])
    api.batch_delete_repos = AsyncMock(return_value={})
    repos = {
        name: (
            GitHubRepo(
                name=name,
                full_name=f"org/{name}",
                ssh_url=f"git@github.com:org/{name}.git",
                clone_url=f"https://github.com/org/{name}.git",
                html_url=f"https://github.com/org/{name}",
                private=False,
            ),
            None,
        )
        for name in names
    }
    api.batch_create_repos = AsyncMock(return_value=repos)
    return api


def _cloning_from(
    upstream: Path,
) -> Callable[[Any, list[Project]], list[CloneResult]]:
    """``clone_projects``, cloning each project from *upstream* for real."""

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

    return clone


class TestMirrorOverwrite:
    def test_a_repository_cloned_again_is_filtered_before_it_is_pushed(
        self, tmp_path: Path
    ) -> None:
        """``--overwrite`` discards the clone, not what it was filtered with.

        Its push is ``git push --mirror``: unfiltered, it would force the
        removed file back onto the GitHub repository.
        """
        upstream = _upstream(tmp_path / "up")
        _commit(upstream, "secret.txt", "hunter2\n")
        config = Config(host="gerrit.example.org", port=29418, path=tmp_path / "tree")
        projects = [Project("proj", ProjectState.ACTIVE)]
        pushed: list[list[str]] = []

        def push(_self: Any, local: Path, _repo: GitHubRepo) -> tuple[bool, None]:
            pushed.append(_history(local))
            return True, None

        with (
            patch(
                "source_clone.clone_orchestrator.CloneManager.clone_projects",
                _cloning_from(upstream),
            ),
            patch.object(MirrorManager, "_push_to_github", push),
        ):
            MirrorManager(
                config=config,
                github_api=_github_api(["proj"]),
                github_org="org",
                remove_file_patterns=["secret.txt"],
            ).mirror_projects(projects)
            MirrorManager(
                config=config,
                github_api=_github_api(["proj"]),
                github_org="org",
                overwrite=True,
            ).mirror_projects(projects)

        assert len(pushed) == 2
        assert all("secret.txt" not in history for history in pushed)

    def test_a_run_selecting_nothing_still_records_its_filters(
        self, tmp_path: Path
    ) -> None:
        """A project selected later is filtered before its first push."""
        upstream = _upstream(tmp_path / "up")
        _commit(upstream, "secret.txt", "hunter2\n")
        config = Config(host="gerrit.example.org", port=29418, path=tmp_path / "tree")
        pushed: list[list[str]] = []

        def push(_self: Any, local: Path, _repo: GitHubRepo) -> tuple[bool, None]:
            pushed.append(_history(local))
            return True, None

        with (
            patch(
                "source_clone.clone_orchestrator.CloneManager.clone_projects",
                _cloning_from(upstream),
            ),
            patch.object(MirrorManager, "_push_to_github", push),
        ):
            MirrorManager(
                config=config,
                github_api=_github_api([]),
                github_org="org",
                remove_file_patterns=["secret.txt"],
            ).mirror_projects([])
            MirrorManager(
                config=config, github_api=_github_api(["proj"]), github_org="org"
            ).mirror_projects([Project("proj", ProjectState.ACTIVE)])

        assert len(pushed) == 1
        assert "secret.txt" not in pushed[0]

    @pytest.mark.parametrize(
        "selection",
        [
            [],
            [Project("other", ProjectState.ACTIVE)],
            [Project("wanted", ProjectState.ACTIVE)],
        ],
        ids=["none-on-server", "none-matching", "selected"],
    )
    def test_the_command_records_its_filters_once(
        self, tmp_path: Path, selection: list[Project]
    ) -> None:
        """Even selecting nothing, and once: the scan behind it visits
        every repository in the tree."""
        tree = tmp_path / "tree"

        result, resolved = _mirror_command(tree, selection)

        assert result.exit_code == 0, result.output
        intent = (tree / ".gerrit-clone" / "filter-policy.json").read_text()
        assert "secret.txt" in intent
        assert resolved == 1

    def test_an_unreadable_intent_is_a_configuration_error(
        self, tmp_path: Path
    ) -> None:
        tree = tmp_path / "tree"
        (tree / ".gerrit-clone").mkdir(parents=True)
        (tree / ".gerrit-clone" / "filter-policy.json").write_text("{not json")

        result, _ = _mirror_command(tree, [])

        assert result.exit_code == 2, result.output


def _mirror_command(tree: Path, selection: list[Project]) -> tuple[Any, int]:
    """Run ``mirror --remove-files secret.txt`` selecting *selection*.

    Returns:
        Its result, and how many times it scanned the tree's repositories
        for their filter records.
    """
    resolved = 0

    def counting(*args: Any, **kwargs: Any) -> Any:
        nonlocal resolved
        resolved += 1
        return gathered_intent(*args, **kwargs)

    with (
        patch(
            "source_clone.cli_mirror_run.authenticate",
            return_value=_github_api([]),
        ),
        patch("source_clone.cli_mirror_run.resolve_org", return_value="org"),
        patch(
            "source_clone.cli_hooks.discover_projects",
            return_value=(selection, {}),
        ),
        patch(
            "source_clone.clone_orchestrator.CloneManager.clone_projects",
            return_value=[],
        ),
        patch("source_clone.content_intent_resolve.gathered_intent", counting),
    ):
        result = CliRunner().invoke(
            app,
            [
                "mirror",
                "--server",
                "gerrit.example.org",
                "--org",
                "org",
                "--output-path",
                str(tree),
                "--projects",
                "wanted",
                "--remove-files",
                "secret.txt",
            ],
        )
    return result, resolved


class TestCloneAgain:
    """Re-running ``clone`` over a tree whose intent needs tokens."""

    @pytest.mark.parametrize("upstream_moved", [True, False], ids=["behind", "current"])
    def test_a_missing_token_fails_the_run_either_way(
        self, tree: Path, upstream_moved: bool
    ) -> None:
        """Not a pass when upstream moved on and the refresh skipped it."""
        first = _refresh(tree, "--git-filter", f"com/child:{TOKEN}")
        assert first.exit_code == 0, first.output
        if upstream_moved:
            _commit(tree.parent / "up" / "child", "new.txt", "new\n")
        spec = ContentFilterSpec(None, None, False, tree, load_intent(tree))
        config = Config(host="gerrit.example.org", path=tree, quiet=True)
        config.content_filters = spec
        results = _refresh_repositories(
            config, [Project(name="com/child", state=ProjectState.ACTIVE)]
        )
        batch = BatchResult(
            config=config,
            results=results,
            started_at=datetime.now(UTC),
            completed_at=datetime.now(UTC),
        )
        request = Mock(quiet=True, clone_timeout=60)

        with pytest.raises(typer.Exit) as stopped:
            _apply_content_filters(request, Mock(), batch, spec)

        assert stopped.value.exit_code != 0


class TestRefreshAtAWorkingCopy:
    def test_the_intent_does_not_dirty_the_checkout(self, tmp_path: Path) -> None:
        """Refresh skips a working copy with uncommitted changes.

        Recorded at the checkout's own top level, the intent must not be
        one of them.
        """
        upstream = _upstream(tmp_path / "up")
        checkout = tmp_path / "checkout"
        _git("clone", "-q", upstream.as_uri(), str(checkout))

        result = CliRunner().invoke(
            app,
            [
                "refresh",
                "--output-path",
                str(checkout),
                "--all-repos",
                "--remove-files",
                "secret.txt",
            ],
        )

        assert (checkout / ".gerrit-clone" / "filter-policy.json").is_file()
        assert ".gerrit-clone" not in _git("status", "--porcelain", cwd=checkout)
        assert result.exit_code == 0, result.output
