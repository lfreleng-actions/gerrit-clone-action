# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""Each command's manifest summarises the tree's filter intent.

An operator, or CI, can then see what a tree is filtered with without
opening every repository.  Manifests often end up as public artifacts,
and a token's digest lets anyone confirm a guessed token offline, so
the summary counts tokens and names no digest.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, Mock, patch

import pytest
from typer.testing import CliRunner

from gerrit_clone.cli import app
from gerrit_clone.clone_reporting import write_manifest
from gerrit_clone.content_intent import intent_path
from gerrit_clone.content_intent_resolve import resolve_filters
from gerrit_clone.content_spec import ContentFilterSpec
from gerrit_clone.models import BatchResult, Config, Project, ProjectState

if TYPE_CHECKING:
    from pathlib import Path


#: Built at runtime so that no credential-shaped literal sits in the
#: source for secret scanners to flag.
TOKEN = "tok-" + "0a1b2c3d4e5f" * 2


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(
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
        check=True,
    )


@pytest.fixture
def tree(tmp_path: Path) -> Path:
    """``com/parent``, a mirror of an upstream holding ``secret.txt``."""
    upstream = tmp_path / "up"
    upstream.mkdir()
    _git(upstream, "init", "-q", "-b", "main")
    (upstream / "secret.txt").write_text(f"{TOKEN}\n")
    _git(upstream, "add", ".")
    _git(upstream, "commit", "-q", "-m", "one")
    root = tmp_path / "tree"
    target = root / "com" / "parent"
    _git(tmp_path, "clone", "-q", "--mirror", upstream.as_uri(), str(target))
    return root


def _summary_file(root: Path) -> str:
    return str(intent_path(root.resolve()))


def _assert_no_token(text: str) -> None:
    assert TOKEN not in text
    assert hashlib.sha256(TOKEN.encode()).hexdigest() not in text


class TestRefreshManifest:
    def test_it_summarises_the_intent(self, tree: Path) -> None:
        result = CliRunner().invoke(
            app,
            [
                "refresh",
                "--output-path",
                str(tree),
                "--all-repos",
                "--manifest-filename",
                "refresh.json",
                "--remove-files",
                "secret.txt",
                "--git-filter",
                f"com/*:{TOKEN}",
            ],
        )
        assert result.exit_code == 0, result.output

        text = (tree / "refresh.json").read_text()

        assert json.loads(text)["content_filters"] == {
            "intent_file": _summary_file(tree),
            "scopes": [
                {"projects": "*", "remove": ["secret.txt"]},
                {"projects": "com/*", "tokens": 1},
            ],
        }
        _assert_no_token(text)

    def test_a_tree_without_filters_says_so(self, tree: Path) -> None:
        result = CliRunner().invoke(
            app,
            [
                "refresh",
                "--output-path",
                str(tree),
                "--all-repos",
                "--manifest-filename",
                "refresh.json",
            ],
        )
        assert result.exit_code == 0, result.output

        manifest = json.loads((tree / "refresh.json").read_text())

        assert manifest["content_filters"] is None

    def test_a_dry_run_shows_what_it_would_record(self, tree: Path) -> None:
        CliRunner().invoke(
            app,
            [
                "refresh",
                "--output-path",
                str(tree),
                "--all-repos",
                "--dry-run",
                "--manifest-filename",
                "refresh.json",
                "--redact-secrets",
            ],
        )

        manifest = json.loads((tree / "refresh.json").read_text())

        assert manifest["content_filters"]["scopes"] == [
            {"projects": "*", "redact_secrets": True}
        ]


class TestCloneManifest:
    def test_it_summarises_the_intent(self, tree: Path) -> None:
        options = ContentFilterSpec.from_options(
            "secret.txt", f"com/*:{TOKEN}", False, tree
        )
        config = Config(host="gerrit.example.org", path=tree)
        config.content_filters = resolve_filters(tree, options, persist=True)
        now = datetime.now(UTC)

        write_manifest(BatchResult(config, [], now, now), config)

        text = (tree / config.manifest_filename).read_text()
        assert json.loads(text)["content_filters"] == {
            "intent_file": _summary_file(tree),
            "scopes": [
                {"projects": "*", "remove": ["secret.txt"]},
                {"projects": "com/*", "tokens": 1},
            ],
        }
        _assert_no_token(text)


def _github_api() -> Mock:
    api = Mock()
    api.list_all_repos_graphql = Mock(return_value={})
    api.list_repos = Mock(return_value=[])
    api.batch_delete_repos = AsyncMock(return_value={})
    return api


class TestMirrorManifest:
    def test_it_summarises_the_intent(self, tree: Path) -> None:
        with (
            patch(
                "gerrit_clone.cli_mirror_run.authenticate", return_value=_github_api()
            ),
            patch("gerrit_clone.cli_mirror_run.resolve_org", return_value="org"),
            patch(
                "gerrit_clone.cli_hooks.discover_projects",
                return_value=([Project("wanted", ProjectState.ACTIVE)], {}),
            ),
            patch(
                "gerrit_clone.clone_orchestrator.CloneManager.clone_projects",
                return_value=[],
            ),
        ):
            result: Any = CliRunner().invoke(
                app,
                [
                    "mirror",
                    "--server",
                    "gerrit.example.org",
                    "--org",
                    "org",
                    "--output-path",
                    str(tree),
                    "--git-filter",
                    f"wanted:{TOKEN}",
                ],
            )
        assert result.exit_code == 0, result.output

        text = (tree / "mirror-manifest.json").read_text()

        assert json.loads(text)["content_filters"] == {
            "intent_file": _summary_file(tree),
            "scopes": [{"projects": "wanted", "tokens": 1}],
        }
        _assert_no_token(text)
