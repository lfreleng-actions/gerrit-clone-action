# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""Every remote counts towards the Gerrit-only gate.

The refresh fetch is ``--all``, so a mirror cloned with ``git clone
--mirror --origin upstream`` from Gerrit is refreshed from Gerrit even
though it has no ``origin``.  Reading ``remote.origin.url`` alone
rejected it as "not a Gerrit repository" under
``filter_gerrit_only``.
"""

from __future__ import annotations

import subprocess
from typing import TYPE_CHECKING

import pytest

from source_clone.refresh_worker import RefreshWorker

if TYPE_CHECKING:
    from pathlib import Path

GERRIT_URL = "ssh://gerrit.example.org:29418/proj"
GITHUB_URL = "https://github.com/example/proj.git"


def _git(*args: str, cwd: Path | None = None) -> None:
    subprocess.run(
        ["git", "-c", "commit.gpgsign=false", *args],
        cwd=cwd,
        capture_output=True,
        check=True,
    )


def _bare(path: Path, *remotes: tuple[str, str]) -> Path:
    _git("init", "-q", "--bare", str(path))
    for name, url in remotes:
        _git("remote", "add", name, url, cwd=path)
    return path


@pytest.fixture
def worker() -> RefreshWorker:
    return RefreshWorker(ssh_jitter_seconds=0)


def test_a_mirror_whose_only_remote_is_upstream_is_gerrit(
    tmp_path: Path, worker: RefreshWorker
) -> None:
    source = tmp_path / "source"
    _git("init", "-q", "-b", "main", str(source))
    (source / "file.txt").write_text("one\n")
    _git("add", "file.txt", cwd=source)
    _git("commit", "-q", "-m", "one", cwd=source)
    mirror = tmp_path / "mirror.git"
    _git(
        "clone", "-q", "--mirror", "--origin", "upstream", source.as_uri(), str(mirror)
    )
    _git("remote", "set-url", "upstream", GERRIT_URL, cwd=mirror)

    assert worker._has_gerrit_remote(mirror) is True
    # Still something to report, though there is no origin to read.
    assert worker._get_remote_url(mirror) == GERRIT_URL


def test_only_non_gerrit_remotes_are_not_gerrit(
    tmp_path: Path, worker: RefreshWorker
) -> None:
    repo = _bare(tmp_path / "repo.git", ("upstream", GITHUB_URL))

    assert worker._has_gerrit_remote(repo) is False


def test_no_remotes_are_not_gerrit(tmp_path: Path, worker: RefreshWorker) -> None:
    repo = _bare(tmp_path / "repo.git")

    assert worker._has_gerrit_remote(repo) is False
    assert worker._get_remote_url(repo) is None


def test_any_gerrit_remote_counts_but_origin_is_reported(
    tmp_path: Path, worker: RefreshWorker
) -> None:
    """Configured first, the Gerrit remote still does not displace origin."""
    repo = _bare(
        tmp_path / "repo.git", ("upstream", GERRIT_URL), ("origin", GITHUB_URL)
    )

    assert worker._has_gerrit_remote(repo) is True
    assert worker._get_remote_url(repo) == GITHUB_URL


def test_remote_names_may_contain_dots(tmp_path: Path, worker: RefreshWorker) -> None:
    repo = _bare(tmp_path / "repo.git", ("team.review", GERRIT_URL))

    assert worker._remote_urls(repo) == [("team.review", GERRIT_URL)]
    assert worker._has_gerrit_remote(repo) is True
