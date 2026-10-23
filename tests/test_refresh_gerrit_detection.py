# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""Which repositories ``refresh`` treats as Gerrit, and which it refreshes.

``refresh`` refreshes every repository under its path unless told
``--gerrit-only``.  Then a repository is Gerrit if the
``clone-manifest.json`` nearest above it says the tree was cloned from
Gerrit; with no usable manifest, if a remote's host is a Gerrit host.
A repository's name or path never decides it.

Remotes carry their real-looking URLs in the git configuration, and
``url.<local>.insteadOf`` sends every fetch to a local upstream, so
nothing here reaches the network.
"""

from __future__ import annotations

import inspect
import json
import subprocess
from typing import TYPE_CHECKING, Any

import pytest
from typer.testing import CliRunner

from source_clone import refresh_git_env
from source_clone.cli import app
from source_clone.models import RefreshStatus, RetryPolicy
from source_clone.refresh_manager import RefreshManager, refresh_repositories
from source_clone.refresh_worker import RefreshWorker

if TYPE_CHECKING:
    from pathlib import Path

GITHUB_URL = "https://github.com/org/app"
GITHUB_REVIEW_URL = "git@github.com:org/code-review-tool.git"
GITLAB_URL = "ssh://git@gitlab.example.com:2222/team/app.git"
GERRIT_SSH_URL = "ssh://user@gerrit.onap.org:29418/ccsdk/apps"
GERRIT_HTTPS_URL = "https://gerrit.onap.org/r/ccsdk/parent"

REFRESHED = {RefreshStatus.SUCCESS.value, RefreshStatus.UP_TO_DATE.value}


def _git(*args: str, cwd: Path | None = None) -> str:
    """Run git with an identity and no signing, returning its stdout."""
    result = subprocess.run(
        [
            "git",
            "-c",
            "user.email=test@example.com",
            "-c",
            "user.name=Test",
            "-c",
            "commit.gpgsign=false",
            *args,
        ],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


@pytest.fixture
def upstream(tmp_path: Path) -> Path:
    """A local upstream with one commit, standing in for every server."""
    path = tmp_path / "upstream"
    _git("init", "-q", "-b", "main", str(path))
    (path / "file.txt").write_text("one\n")
    _git("add", "file.txt", cwd=path)
    _git("commit", "-q", "-m", "one", cwd=path)
    return path


def _mirror(upstream: Path, path: Path, url: str) -> Path:
    """A bare mirror of *upstream* at *path* whose origin claims to be *url*."""
    _git("clone", "-q", "--mirror", upstream.as_uri(), str(path))
    _git("remote", "set-url", "origin", url, cwd=path)
    _git("config", f"url.{upstream.as_uri()}.insteadOf", url, cwd=path)
    return path


def _manifest(directory: Path, content: Any) -> None:
    text = content if isinstance(content, str) else json.dumps(content)
    (directory / "clone-manifest.json").write_text(text)


def _statuses(tree: Path) -> dict[str, str]:
    """Each project's status, from the refresh manifest the command wrote."""
    [manifest] = tree.glob("refresh-manifest-*.json")
    return {
        r["project"]: r["status"] for r in json.loads(manifest.read_text())["results"]
    }


@pytest.fixture
def worker() -> RefreshWorker:
    return RefreshWorker(filter_gerrit_only=True, ssh_jitter_seconds=0)


class TestHostRule:
    """With no clone manifest, only a remote's host makes it Gerrit."""

    @pytest.mark.parametrize(
        ("url", "expected"),
        [
            (GITHUB_REVIEW_URL, False),
            ("https://github.com/org/reviewdog-action", False),
            (GITLAB_URL, False),
            ("git@github.com:org/gerrit-clone-action.git", False),
            ("https://github.com/org/plain-repo", False),
            (GERRIT_SSH_URL, True),
            ("https://gerrit.onap.org/r/ccsdk/apps", True),
            ("https://review.opendev.org/openstack/nova", True),
            ("https://gerrit-staging.example.org/project", True),
            ("ssh://review-ssh.example.org/project", True),
            ("review.example.org:project", True),
            ("https://GERRIT.Example.org/project", True),
            ("https://git.example.org/gerrit/project", True),
            ("https://git.example.org/r/project", True),
            ("ssh://git.example.org:29418/project", True),
            ("ssh://git@[2001:db8::1]:29418/project", True),
            ("https://github.example.com/org/review-app", False),
            ("https://code.github.com/org/r/app", False),
            ("ssh://git@gitlab.com:29418/team/app", False),
            ("https://gitlab.com/gerrit/app", False),
            ("https://bitbucket.org/r/app", False),
            ("https://git.example.org/team/r/app", False),
            ("https://git.example.org/team/gerrit/app", False),
            ("https://reviewboard.example.org/app", False),
            ("https://gerritlike.example.org/app", False),
            ("ssh://git.example.org:2222/r/app", False),
            ("/srv/gerrit/app.git", False),
            ("file:///srv/review/app.git", False),
            ("https://git.example.org/app", False),
            ("", False),
            (None, False),
        ],
    )
    def test_classification(
        self, worker: RefreshWorker, url: str | None, expected: bool
    ) -> None:
        assert worker._is_gerrit_repository(url) is expected


class TestManifestRule:
    """The ``clone-manifest.json`` nearest above a repository decides."""

    def test_a_gerrit_manifest_makes_every_repository_gerrit(
        self, tmp_path: Path, upstream: Path, worker: RefreshWorker
    ) -> None:
        tree = tmp_path / "tree"
        (tree / "team").mkdir(parents=True)
        _manifest(tree, {"source_type": "gerrit"})
        repo = _mirror(upstream, tree / "team/app", GITHUB_URL)

        assert worker._has_gerrit_remote(repo) is True

    def test_a_github_manifest_makes_no_repository_gerrit(
        self, tmp_path: Path, upstream: Path, worker: RefreshWorker
    ) -> None:
        tree = tmp_path / "tree"
        tree.mkdir()
        _manifest(tree, {"source_type": "github"})
        named = _mirror(upstream, tree / "code-review-tool", GITHUB_REVIEW_URL)
        hosted = _mirror(upstream, tree / "apps", GERRIT_SSH_URL)

        assert worker._has_gerrit_remote(named) is False
        assert worker._has_gerrit_remote(hosted) is False

    def test_the_nearest_manifest_wins(
        self, tmp_path: Path, upstream: Path, worker: RefreshWorker
    ) -> None:
        tree = tmp_path / "tree"
        (tree / "mirrored").mkdir(parents=True)
        _manifest(tree, {"source_type": "gerrit"})
        _manifest(tree / "mirrored", {"source_type": "github"})
        near = _mirror(upstream, tree / "mirrored/app", GERRIT_SSH_URL)
        far = _mirror(upstream, tree / "other", GITHUB_URL)

        assert worker._has_gerrit_remote(near) is False
        assert worker._has_gerrit_remote(far) is True

    @pytest.mark.parametrize(
        "content",
        [
            "{not json",
            "",
            "[]",
            {"version": "1.0"},
            {"source_type": "gitlab"},
            {"source_type": None},
            {"source_type": ["gerrit"]},
        ],
    )
    def test_an_unusable_manifest_falls_through_to_the_host(
        self, tmp_path: Path, upstream: Path, worker: RefreshWorker, content: Any
    ) -> None:
        """The first manifest found gives no answer: the host decides.

        One higher up is not consulted, so the gerrit manifest at the
        root does not make the GitHub remote Gerrit.
        """
        tree = tmp_path / "tree"
        (tree / "team").mkdir(parents=True)
        _manifest(tree, {"source_type": "gerrit"})
        _manifest(tree / "team", content)
        gerrit = _mirror(upstream, tree / "team/gerrit", GERRIT_SSH_URL)
        github = _mirror(upstream, tree / "team/github", GITHUB_URL)

        assert worker._has_gerrit_remote(gerrit) is True
        assert worker._has_gerrit_remote(github) is False

    def test_an_unreadable_manifest_falls_through_to_the_host(
        self, tmp_path: Path, upstream: Path, worker: RefreshWorker
    ) -> None:
        tree = tmp_path / "tree"
        (tree / "clone-manifest.json").mkdir(parents=True)
        gerrit = _mirror(upstream, tree / "gerrit", GERRIT_SSH_URL)
        github = _mirror(upstream, tree / "github", GITHUB_URL)

        assert worker._has_gerrit_remote(gerrit) is True
        assert worker._has_gerrit_remote(github) is False

    def test_each_manifest_is_read_once_per_run(
        self, tmp_path: Path, upstream: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        tree = tmp_path / "tree"
        for team in ("a", "b"):
            (tree / team).mkdir(parents=True)
            for n in range(3):
                _mirror(upstream, tree / team / f"repo{n}", GERRIT_SSH_URL)
        _manifest(tree, {"source_type": "github"})
        _manifest(tree / "b", {"source_type": "github"})

        reads: list[Path] = []
        read = vars(refresh_git_env)["_read_clone_manifest"]

        def counting(path: Path) -> Any:
            reads.append(path)
            return read(path)

        monkeypatch.setattr(refresh_git_env, "_read_clone_manifest", counting)

        batch = refresh_repositories(tree, filter_gerrit_only=True, threads=4)

        assert {r.status for r in batch.results} == {RefreshStatus.NOT_GERRIT_REPO}
        assert sorted(reads) == sorted(
            [tree / "clone-manifest.json", tree / "b/clone-manifest.json"]
        )


class TestDefault:
    """``refresh`` refreshes every repository unless told ``--gerrit-only``."""

    @pytest.fixture
    def tree(self, tmp_path: Path, upstream: Path) -> Path:
        tree = tmp_path / "tree"
        tree.mkdir()
        _mirror(upstream, tree / "github", GITHUB_URL)
        _mirror(upstream, tree / "code-review-tool", GITHUB_REVIEW_URL)
        _mirror(upstream, tree / "gitlab", GITLAB_URL)
        _mirror(upstream, tree / "gerrit", GERRIT_SSH_URL)
        return tree

    def test_no_flag_refreshes_every_repository(self, tree: Path) -> None:
        outcome = CliRunner().invoke(app, ["refresh", "--output-path", str(tree)])

        assert outcome.exit_code == 0, outcome.output
        assert set(_statuses(tree).values()) <= REFRESHED

    def test_all_repos_refreshes_every_repository(self, tree: Path) -> None:
        outcome = CliRunner().invoke(
            app, ["refresh", "--output-path", str(tree), "--all-repos"]
        )

        assert outcome.exit_code == 0, outcome.output
        assert set(_statuses(tree).values()) <= REFRESHED

    def test_gerrit_only_refreshes_only_gerrit(self, tree: Path) -> None:
        CliRunner().invoke(
            app, ["refresh", "--output-path", str(tree), "--gerrit-only"]
        )

        statuses = _statuses(tree)

        assert statuses.pop("gerrit") in REFRESHED
        assert statuses == {
            "github": RefreshStatus.NOT_GERRIT_REPO.value,
            "code-review-tool": RefreshStatus.NOT_GERRIT_REPO.value,
            "gitlab": RefreshStatus.NOT_GERRIT_REPO.value,
        }

    def test_the_library_defaults_to_every_repository(self) -> None:
        assert RefreshWorker().filter_gerrit_only is False
        assert RefreshManager().filter_gerrit_only is False
        default = inspect.signature(refresh_repositories).parameters[
            "filter_gerrit_only"
        ]
        assert default.default is False


def test_the_dry_run_predicts_the_real_run(tmp_path: Path, upstream: Path) -> None:
    tree = tmp_path / "tree"
    (tree / "gerrit-tree").mkdir(parents=True)
    _manifest(tree / "gerrit-tree", {"source_type": "gerrit"})
    _mirror(upstream, tree / "github", GITHUB_REVIEW_URL)
    _mirror(upstream, tree / "gitlab", GITLAB_URL)
    _mirror(upstream, tree / "gerrit-ssh", GERRIT_SSH_URL)
    _mirror(upstream, tree / "gerrit-https", GERRIT_HTTPS_URL)
    _mirror(upstream, tree / "gerrit-tree/from-manifest", GITHUB_URL)

    def skipped(**kwargs: Any) -> set[str]:
        manager = RefreshManager(
            filter_gerrit_only=True,
            retry_policy=RetryPolicy(max_attempts=1),
            threads=2,
            **kwargs,
        )
        batch = manager.refresh_repositories(tree)
        assert {r.status.value for r in batch.results} <= {
            *REFRESHED,
            RefreshStatus.NOT_GERRIT_REPO.value,
        }
        return {
            r.project_name
            for r in batch.results
            if r.status == RefreshStatus.NOT_GERRIT_REPO
        }

    predicted = skipped(dry_run=True)
    actual = skipped()

    assert predicted == actual == {"github", "gitlab"}
