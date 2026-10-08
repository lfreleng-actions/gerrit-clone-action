# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""Each content-filtering rewrite leaves an entry in the tree's journal.

``<tree>/.gerrit-clone/filter-journal.jsonl`` gets one line before each
rewrite starts and another once it ends: which command and release ran
it, on which project, with which method and filters, and a digest of the
refs before and after.  The intent file stays the decision; the journal
is the audit trail of the runs that carried it out.  An entry a crash
left without its end is the exception: until a later run filters that
project again, its filters stay in force even if the intent lost them.
"""

from __future__ import annotations

import hashlib
import json
import stat
import subprocess
import sys
from typing import TYPE_CHECKING, Any
from unittest.mock import patch

import pytest
from typer.testing import CliRunner

from gerrit_clone.cli import app
from gerrit_clone.content_intent import IntentError, intent_path
from gerrit_clone.content_intent_resolve import resolve_filters
from gerrit_clone.content_journal import sources
from gerrit_clone.content_spec import ContentFilterSpec
from gerrit_clone.content_stage import UNJOURNALLED_REFUSAL, filter_repository

if TYPE_CHECKING:
    from pathlib import Path

#: Built at runtime so that no credential-shaped literal sits in the
#: source for secret scanners to flag.
TOKEN = "tok-" + "5e4d3c2b1a0f" * 2


def _git(repo: Path, *args: str) -> str:
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
        cwd=repo,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()


@pytest.fixture
def tree(tmp_path: Path) -> Path:
    """``com/parent``, a mirror of an upstream holding ``secret.txt``."""
    upstream = tmp_path / "up"
    upstream.mkdir()
    _git(upstream, "init", "-q", "-b", "main")
    (upstream / "secret.txt").write_text(f"{TOKEN}\n")
    (upstream / "file.txt").write_text("one\n")
    _git(upstream, "add", ".")
    _git(upstream, "commit", "-q", "-m", "one")
    root = tmp_path / "tree"
    _git(
        tmp_path,
        "clone",
        "-q",
        "--mirror",
        upstream.as_uri(),
        str(root / "com" / "parent"),
    )
    return root


def _journal(root: Path) -> list[dict[str, Any]]:
    path = root / ".gerrit-clone" / "filter-journal.jsonl"
    lines = path.read_text(encoding="utf-8").splitlines()
    return [json.loads(line) for line in lines if line.strip()]


def _refresh(root: Path, *options: str) -> Any:
    return CliRunner().invoke(
        app, ["refresh", "--output-path", str(root), "--all-repos", *options]
    )


def _spec(root: Path) -> ContentFilterSpec:
    options = ContentFilterSpec.from_options("secret.txt", None, False, root)
    spec = resolve_filters(root, options, persist=True)
    assert spec is not None
    return spec


class TestEntries:
    def test_a_rewrite_is_journalled_before_and_after(self, tree: Path) -> None:
        result = _refresh(tree, "--remove-files", "secret.txt")
        assert result.exit_code == 0, result.output

        start, end = _journal(tree)

        assert start["schema"] == 1
        assert start["event"] == "start"
        assert start["command"] == "refresh"
        assert start["project"] == "com/parent"
        assert start["method"] in ("filter-repo", "worktree")
        assert start["policy"] == {"remove": ["secret.txt"]}
        assert start["version"]
        assert start["time"]
        assert end == {
            "schema": 1,
            "event": "end",
            "id": start["id"],
            "time": end["time"],
            "ok": True,
            "refs_sha256": end["refs_sha256"],
        }
        assert start["refs_sha256"] != end["refs_sha256"], "the rewrite changed refs"

    def test_each_run_appends(self, tree: Path) -> None:
        for _ in range(2):
            assert _refresh(tree, "--remove-files", "secret.txt").exit_code == 0

        assert [entry["event"] for entry in _journal(tree)] == [
            "start",
            "end",
            "start",
            "end",
        ]

    def test_a_failed_rewrite_is_journalled_as_failed(self, tree: Path) -> None:
        spec = _spec(tree)
        repo = tree / "com" / "parent"

        reason = filter_repository(
            spec, repo, "com/parent", 60, apply=lambda *_a, **_k: (False, "broke")
        )

        assert reason is not None
        assert _journal(tree)[-1]["ok"] is False

    def test_no_token_reaches_the_journal(self, tree: Path) -> None:
        result = _refresh(tree, "--git-filter", f"com/*:{TOKEN}")
        assert result.exit_code == 0, result.output

        text = (tree / ".gerrit-clone" / "filter-journal.jsonl").read_text()
        digest = hashlib.sha256(TOKEN.encode()).hexdigest()
        assert TOKEN not in text
        assert _journal(tree)[0]["policy"] == {"token_sha256": [digest]}

    def test_a_dry_run_journals_nothing(self, tree: Path) -> None:
        _refresh(tree, "--dry-run", "--remove-files", "secret.txt")

        assert not (tree / ".gerrit-clone" / "filter-journal.jsonl").exists()


def _interrupted(*_args: object, **_kwargs: object) -> tuple[bool, str | None]:
    raise KeyboardInterrupt


class TestInterruptedRewrites:
    """A rewrite a crash cut short leaves a start with no end."""

    def _crash(self, tree: Path) -> None:
        with pytest.raises(KeyboardInterrupt):
            filter_repository(
                _spec(tree),
                tree / "com" / "parent",
                "com/parent",
                60,
                apply=_interrupted,
            )

    def test_it_leaves_a_start_without_an_end(self, tree: Path) -> None:
        self._crash(tree)

        assert [entry["event"] for entry in _journal(tree)] == ["start"]

    def test_its_filters_stay_in_force_without_the_intent(self, tree: Path) -> None:
        """Nothing else recorded them: the crash came before the rewrite
        could record them in the repository."""
        self._crash(tree)
        intent_path(tree).unlink()

        spec = resolve_filters(tree, None, persist=False)

        assert spec is not None
        assert spec.filters_for("com/parent").remove_patterns == ["secret.txt"]
        assert spec.filters_for("com/other").remove_patterns is None

    def test_a_later_completed_rewrite_releases_it(self, tree: Path) -> None:
        self._crash(tree)
        spec = _spec(tree)
        assert (
            filter_repository(
                spec, tree / "com" / "parent", "com/parent", 60, apply=_unchanged
            )
            is None
        )
        intent_path(tree).unlink()

        assert resolve_filters(tree, None, persist=False) is None

    def test_an_end_releases_no_start_that_came_after_it(self, tree: Path) -> None:
        """Run B started while run A filtered; A finishing says nothing of B."""
        self._crash(tree)
        journal = tree / ".gerrit-clone" / "filter-journal.jsonl"
        crashed = _journal(tree)[0]
        journal.unlink()
        first = {**crashed, "id": "a"}
        second = {**crashed, "id": "b"}
        end = {
            "schema": 1,
            "event": "end",
            "id": "a",
            "time": crashed["time"],
            "ok": True,
            "refs_sha256": crashed["refs_sha256"],
        }
        journal.write_text(
            "".join(json.dumps(entry) + "\n" for entry in (first, second, end))
        )
        intent_path(tree).unlink()

        spec = resolve_filters(tree, None, persist=False)

        assert spec is not None
        assert spec.filters_for("com/parent").remove_patterns == ["secret.txt"]

    def test_a_run_below_the_root_still_finds_them(self, tree: Path) -> None:
        """With the intent file gone, the journal marks the tree's root."""
        self._crash(tree)
        intent_path(tree).unlink()

        spec = resolve_filters(tree / "com", None, persist=False)

        assert spec is not None
        assert spec.base_path == tree.resolve()
        assert spec.filters_for("com/parent").remove_patterns == ["secret.txt"]

    def test_a_torn_last_line_is_ignored(self, tree: Path) -> None:
        """A crash while writing an entry: no rewrite followed it."""
        self._crash(tree)
        journal = tree / ".gerrit-clone" / "filter-journal.jsonl"
        torn = '{"event": "start", "id": "torn", "pro'
        with journal.open("a", encoding="utf-8") as stream:
            stream.write(torn)

        assert resolve_filters(tree, None, persist=False) is not None
        filter_repository(
            _spec(tree), tree / "com" / "parent", "com/parent", 60, apply=_unchanged
        )

        # The next entry replaced it, rather than following it on its line.
        assert torn not in journal.read_text(encoding="utf-8")
        assert [entry["event"] for entry in _journal(tree)] == [
            "start",
            "start",
            "end",
        ]

    def test_an_entry_from_another_release_stops_the_run(self, tree: Path) -> None:
        """Read by this release's rules, it could be misread."""
        self._crash(tree)
        journal = tree / ".gerrit-clone" / "filter-journal.jsonl"
        entry = _journal(tree)[0]
        with journal.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps({**entry, "schema": 2, "id": "newer"}) + "\n")

        with pytest.raises(IntentError, match="schema 2"):
            resolve_filters(tree, None, persist=False)

    @pytest.mark.parametrize("digest", [None, "", "not-a-digest", "A" * 64])
    def test_an_end_without_its_digest_stops_the_run(
        self, tree: Path, digest: str | None
    ) -> None:
        """Read as a completed rewrite, it would release the start's filters."""
        self._crash(tree)
        journal = tree / ".gerrit-clone" / "filter-journal.jsonl"
        start = _journal(tree)[0]
        end = {"schema": 1, "event": "end", "id": start["id"], "ok": True}
        if digest is not None:
            end["refs_sha256"] = digest
        with journal.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(end) + "\n")

        with pytest.raises(IntentError, match="refs_sha256"):
            resolve_filters(tree, None, persist=False)

    def test_a_garbled_entry_stops_the_run(self, tree: Path) -> None:
        self._crash(tree)
        journal = tree / ".gerrit-clone" / "filter-journal.jsonl"
        with journal.open("a", encoding="utf-8") as stream:
            stream.write("not json\n")

        with pytest.raises(IntentError, match="journal"):
            resolve_filters(tree, None, persist=False)


def _unchanged(*_args: object, **_kwargs: object) -> tuple[bool, str | None]:
    return True, None


class TestEveryCommand:
    def test_filtering_in_place_is_journalled(self, tree: Path) -> None:
        """Clone and mirror filter the repository itself, not a copy."""
        repo = tree / "com" / "parent"

        assert filter_repository(_spec(tree), repo, "com/parent", 60) is None

        start, end = _journal(tree)
        assert start["path"] == "com/parent"
        assert end["ok"] is True
        assert start["refs_sha256"] != end["refs_sha256"]


class TestUnreadableRefs:
    """The digests are the journal's evidence, never left out."""

    def test_without_refs_before_nothing_is_filtered(self, tree: Path) -> None:
        applied: list[object] = []

        def apply(*args: object, **kwargs: object) -> tuple[bool, str | None]:
            applied.append(args)
            return True, None

        with patch("gerrit_clone.content_journal.refs_digest", return_value=None):
            reason = filter_repository(
                _spec(tree), tree / "com" / "parent", "com/parent", 60, apply=apply
            )

        assert reason == UNJOURNALLED_REFUSAL
        assert applied == []
        assert not (tree / ".gerrit-clone" / "filter-journal.jsonl").exists()

    def test_without_refs_after_the_rewrite_stays_unfinished(self, tree: Path) -> None:
        spec = _spec(tree)
        with patch(
            "gerrit_clone.content_journal.refs_digest", side_effect=["a" * 64, None]
        ):
            filter_repository(
                spec, tree / "com" / "parent", "com/parent", 60, apply=_unchanged
            )
        intent_path(tree).unlink()

        assert [entry["event"] for entry in _journal(tree)] == ["start"]
        unfinished = resolve_filters(tree, None, persist=False)
        assert unfinished is not None
        assert unfinished.filters_for("com/parent").remove_patterns == ["secret.txt"]


class TestDurability:
    def test_creating_the_journal_syncs_its_directory(self, tree: Path) -> None:
        """Else a crash could lose the file, start and all; the first run
        may have made .gerrit-clone/ itself just before."""
        synced: list[Path] = []
        spec = _spec(tree)

        with patch(
            "gerrit_clone.content_journal._sync_directory", side_effect=synced.append
        ):
            for _ in range(2):
                filter_repository(
                    spec, tree / "com" / "parent", "com/parent", 60, apply=_unchanged
                )

        assert synced == [tree / ".gerrit-clone", tree]


class TestSources:
    """A staging copy's path is temporary: the entry names the upstream."""

    def test_a_refreshed_mirror_names_its_upstream(self, tree: Path) -> None:
        result = _refresh(tree, "--remove-files", "secret.txt")
        assert result.exit_code == 0, result.output

        start = _journal(tree)[0]

        assert start["sources"] == {"origin": (tree.parent / "up").as_uri()}

    def test_credentials_never_reach_the_journal(self, tree: Path) -> None:
        repo = tree / "com" / "parent"
        _git(
            repo,
            "config",
            "remote.origin.url",
            f"https://user:{TOKEN}@gerrit.example.org/com/parent?t={TOKEN}#{TOKEN}",
        )
        _git(repo, "remote", "add", "ssh", "builder@gerrit.example.org:com/parent")
        _git(repo, "remote", "add", "local", "/srv/git/com/parent.git")
        _git(repo, "remote", "add", "v6", f"{TOKEN}@[2001:db8::1]:com/parent")
        _git(repo, "remote", "add", "pair", f"user:{TOKEN}@gerrit.example.org:x")

        filter_repository(_spec(tree), repo, "com/parent", 60, apply=_unchanged)

        text = (tree / ".gerrit-clone" / "filter-journal.jsonl").read_text()
        assert TOKEN not in text
        assert "builder@" not in text
        assert _journal(tree)[0]["sources"] == {
            "local": "/srv/git/com/parent.git",
            "origin": "https://gerrit.example.org/com/parent",
            "pair": "gerrit.example.org:x",
            "ssh": "gerrit.example.org:com/parent",
            "v6": "[2001:db8::1]:com/parent",
        }

    def test_a_remote_helper_url_is_redacted_too(self, tree: Path) -> None:
        """Its address is a URL in its own right, credentials and all."""
        repo = tree / "com" / "parent"
        _git(
            repo,
            "remote",
            "add",
            "relay",
            f"remote-x::https://user:{TOKEN}@gerrit.example.org/com/parent",
        )

        filter_repository(_spec(tree), repo, "com/parent", 60, apply=_unchanged)

        text = (tree / ".gerrit-clone" / "filter-journal.jsonl").read_text()
        assert TOKEN not in text
        assert _journal(tree)[0]["sources"]["relay"] == (
            "remote-x::https://gerrit.example.org/com/parent"
        )

    def test_a_remote_is_named_by_the_url_git_fetches_from(self, tree: Path) -> None:
        """Git fetches from a remote's first URL; later ones only push."""
        repo = tree / "com" / "parent"
        first = _git(repo, "config", "remote.origin.url")
        _git(
            repo, "config", "--add", "remote.origin.url", "https://elsewhere.example/x"
        )

        assert sources(repo) == {"origin": first}

    def test_remotes_it_cannot_read_refuse_the_rewrite(self, tree: Path) -> None:
        with patch("gerrit_clone.content_journal.sources", return_value=None):
            reason = filter_repository(
                _spec(tree), tree / "com" / "parent", "com/parent", 60, apply=_unchanged
            )

        assert reason == UNJOURNALLED_REFUSAL

    @pytest.mark.parametrize(("status", "found"), [(1, {}), (128, None), (None, None)])
    def test_no_remotes_differs_from_unreadable_ones(
        self, tree: Path, status: int | None, found: dict[str, str] | None
    ) -> None:
        """git exits 1 for no match; anything else, or not running, fails."""
        ran = (
            None
            if status is None
            else subprocess.CompletedProcess([], status, stdout="", stderr="")
        )
        with patch("gerrit_clone.content_journal.git", return_value=ran):
            assert sources(tree / "com" / "parent") == found

    @pytest.mark.skipif(sys.platform == "win32", reason="POSIX permissions")
    def test_one_already_there_is_made_owner_only(self, tree: Path) -> None:
        """A checkout can track it, as 0644; its mode is not kept."""
        journal = tree / ".gerrit-clone" / "filter-journal.jsonl"
        journal.parent.mkdir()
        journal.write_text("")
        journal.chmod(0o644)

        filter_repository(
            _spec(tree), tree / "com" / "parent", "com/parent", 60, apply=_unchanged
        )

        assert stat.S_IMODE(journal.stat().st_mode) & 0o077 == 0

    @pytest.mark.skipif(sys.platform == "win32", reason="POSIX permissions")
    def test_only_its_owner_can_read_it(self, tree: Path) -> None:
        """It holds token digests, which confirm a guessed token offline."""
        filter_repository(
            _spec(tree), tree / "com" / "parent", "com/parent", 60, apply=_unchanged
        )

        mode = (tree / ".gerrit-clone" / "filter-journal.jsonl").stat().st_mode
        assert stat.S_IMODE(mode) & 0o077 == 0


@pytest.mark.skipif(sys.platform == "win32", reason="symbolic links need privileges")
def test_a_journal_linked_to_nothing_stops_the_run(tree: Path) -> None:
    """It marks the root; read as no journal, a parent's intent would be
    set aside by a run in that subtree."""
    (tree / ".gerrit-clone").mkdir()
    (tree / ".gerrit-clone" / "filter-journal.jsonl").symlink_to(tree / "missing")

    with pytest.raises(IntentError, match="link to nothing"):
        resolve_filters(tree / "com", None, persist=False)
