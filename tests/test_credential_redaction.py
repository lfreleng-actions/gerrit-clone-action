# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""No credential in a remote URL reaches what the tool writes.

A refresh records each repository's remote in its manifest, which often
ends up as a public CI artifact, and quotes it when it refuses a
repository; a push reports git's output, which can quote the URL it
used.  Each keeps the credential out, whoever put it in the URL.
"""

from __future__ import annotations

import subprocess
from typing import TYPE_CHECKING
from unittest.mock import Mock

import pytest
from typer.testing import CliRunner

from gerrit_clone.cli import app
from gerrit_clone.mirror_manager import MirrorManager
from gerrit_clone.models import Config

if TYPE_CHECKING:
    from pathlib import Path

#: Built at runtime so that no credential-shaped literal sits in the
#: source for secret scanners to flag.
TOKEN = "tok-" + "7c6b5a4d3e2f" * 2


def _git(*args: str, cwd: Path | None = None) -> None:
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
    """A mirror at ``tree/proj`` whose remote URL carries a credential."""
    upstream = tmp_path / "up"
    upstream.mkdir()
    _git("init", "-q", "-b", "main", str(upstream))
    (upstream / "file.txt").write_text("one\n")
    _git("add", "file.txt", cwd=upstream)
    _git("commit", "-q", "-m", "one", cwd=upstream)
    root = tmp_path / "tree"
    mirror = root / "proj"
    _git("clone", "-q", "--mirror", upstream.as_uri(), str(mirror))
    _git(
        "config",
        "remote.origin.url",
        f"https://user:{TOKEN}@example.org/proj.git",
        cwd=mirror,
    )
    return root


class TestRefreshManifests:
    def test_a_manifest_names_the_remote_without_its_credential(
        self, tree: Path
    ) -> None:
        CliRunner().invoke(
            app,
            [
                "refresh",
                "--output-path",
                str(tree),
                "--all-repos",
                "--dry-run",
                "--manifest-filename",
                "manifest.json",
            ],
        )

        text = (tree / "manifest.json").read_text()
        assert TOKEN not in text
        assert "https://example.org/proj.git" in text

    def test_a_refusal_quotes_the_remote_without_its_credential(
        self, tree: Path
    ) -> None:
        """Gerrit-only by default: example.org is refused, and quoted."""
        CliRunner().invoke(
            app,
            ["refresh", "--output-path", str(tree), "--manifest-filename", "m.json"],
        )

        text = (tree / "m.json").read_text()
        assert "Not a Gerrit repository" in text
        assert TOKEN not in text


class TestPushOutput:
    def test_git_output_quoting_a_credentialed_url_is_redacted(
        self, tmp_path: Path
    ) -> None:
        """The configured token is not the only credential it can quote."""
        configured = "ghp_" + "configured0" * 3
        manager = MirrorManager(
            config=Config(host="gerrit.example.org", path=tmp_path),
            github_api=Mock(),
            github_org="org",
            github_token=configured,
        )
        output = (
            f"fatal: unable to access 'https://user:{TOKEN}@github.com/org/r.git/': "
            f"denied for {configured}"
        )

        redacted = manager._sanitize_token(output)

        assert TOKEN not in redacted
        assert configured not in redacted
        assert "github.com/org/r.git" in redacted
