# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""Tests for a clone tree's recorded content-filter intent."""

from __future__ import annotations

import json
import os
import subprocess
from typing import TYPE_CHECKING
from unittest.mock import patch

import pytest

from source_clone.content_filter import apply_content_filters
from source_clone.content_intent import (
    ALL_PROJECTS,
    FilterIntent,
    IntentError,
    find_root,
    intent_path,
    load_intent,
    save_intent,
)
from source_clone.content_intent_resolve import gathered_intent, resolve_filters
from source_clone.content_policy import FilterPolicy, add_policy
from source_clone.content_spec import ContentFilterSpec
from source_clone.content_stage import SHALLOW_HISTORY_REFUSED, filter_repository
from source_clone.subprocess_tracking import run_tracked

if TYPE_CHECKING:
    from pathlib import Path

#: Built at runtime so that no credential-shaped literal sits in the
#: source for secret scanners to flag.
TOKEN = "tok-" + "9f8e7d6c5b4a" * 2


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


def _mirror(tmp_path: Path, name: str) -> Path:
    """A mirror at ``tree/<name>`` of an upstream holding ``secret.txt``."""
    upstream = tmp_path / "upstreams" / name
    upstream.mkdir(parents=True)
    _git(upstream, "init", "-q", "-b", "main")
    (upstream / "secret.txt").write_text("hunter2\n")
    (upstream / "file.txt").write_text("one\n")
    _git(upstream, "add", ".")
    _git(upstream, "commit", "-q", "-m", "one")
    mirror = tmp_path / "tree" / name
    mirror.parent.mkdir(parents=True, exist_ok=True)
    _git(tmp_path, "clone", "-q", "--mirror", upstream.as_uri(), str(mirror))
    return mirror


def _options(
    tree: Path, remove: str | None = None, git_filter: str | None = None
) -> ContentFilterSpec:
    spec = ContentFilterSpec.from_options(remove, git_filter, False, tree)
    assert spec is not None
    return spec


class TestTheFile:
    def test_it_round_trips(self, tmp_path: Path) -> None:
        intent = FilterIntent.of_options(["secret.txt"], {"com/*": [TOKEN]}, True)

        save_intent(tmp_path, intent)

        assert load_intent(tmp_path) == intent

    def test_it_never_holds_a_token(self, tmp_path: Path) -> None:
        save_intent(tmp_path, FilterIntent.of_options(None, {"p": [TOKEN]}, False))

        assert TOKEN not in intent_path(tmp_path).read_text()

    def test_no_file_is_no_intent(self, tmp_path: Path) -> None:
        assert load_intent(tmp_path).empty

    @pytest.mark.parametrize(
        "content",
        [
            "{not json",
            '{"schema": 2, "entries": []}',
            '{"entries": []}',
            '{"schema": 1, "entries": [{"remove": ["x"]}]}',
            '{"schema": 1, "entries": [{"projects": "*", "remove": "x"}]}',
            '{"schema": 1, "entries": [{"projects": "*", "redact_secrets": "true"}]}',
            '{"schema": true, "entries": []}',
            '{"schema": 1.0, "entries": []}',
            '{"schema": 1, "entries": {}}',
            '{"schema": 1, "entries": ""}',
            '{"schema": 1, "entries": ["projects"]}',
            "[]",
        ],
        ids=[
            "corrupt",
            "newer-schema",
            "no-schema",
            "no-scope",
            "bad-list",
            "redact-not-boolean",
            "schema-boolean",
            "schema-float",
            "entries-object",
            "entries-string",
            "entry-not-object",
            "not-object",
        ],
    )
    def test_one_that_cannot_be_understood_fails_closed(
        self, tmp_path: Path, content: str
    ) -> None:
        """Never read as "nothing decided"."""
        intent_path(tmp_path).parent.mkdir()
        intent_path(tmp_path).write_text(content)

        with pytest.raises(IntentError):
            load_intent(tmp_path)

    def test_one_not_in_utf8_fails_closed(self, tmp_path: Path) -> None:
        """An IntentError like any other unreadable file, not a crash."""
        intent_path(tmp_path).parent.mkdir()
        intent_path(tmp_path).write_bytes(b'{"schema": 1, "entries": [\xff]}')

        with pytest.raises(IntentError):
            load_intent(tmp_path)

    @pytest.mark.skipif(os.geteuid() == 0, reason="root reads any file")
    def test_one_that_cannot_be_read_fails_closed(self, tmp_path: Path) -> None:
        save_intent(tmp_path, FilterIntent.of_options(["x"], None, False))
        intent_path(tmp_path).chmod(0)
        try:
            with pytest.raises(IntentError):
                load_intent(tmp_path)
        finally:
            intent_path(tmp_path).chmod(0o600)

    def test_a_failed_write_leaves_the_old_one(self, tmp_path: Path) -> None:
        """All of it or none of it: a half-written decision is no decision."""
        old = FilterIntent.of_options(["old.txt"], None, False)
        save_intent(tmp_path, old)

        with (
            patch("source_clone.content_intent.json.dump", side_effect=OSError("full")),
            pytest.raises(IntentError),
        ):
            save_intent(tmp_path, FilterIntent.of_options(["new.txt"], None, False))

        assert load_intent(tmp_path) == old
        assert [p.name for p in intent_path(tmp_path).parent.iterdir()] == [
            intent_path(tmp_path).name
        ]

    def test_its_root_is_found_from_beneath_it(self, tmp_path: Path) -> None:
        save_intent(tmp_path, FilterIntent.of_options(["x"], None, False))
        deeper = tmp_path / "com" / "parent"
        deeper.mkdir(parents=True)

        assert find_root(deeper) == tmp_path.resolve()

    def test_a_directory_in_its_place_is_found_and_fails_closed(
        self, tmp_path: Path
    ) -> None:
        """Not passed over: a run in a subtree would choose a weaker root."""
        intent_path(tmp_path).mkdir(parents=True)
        deeper = tmp_path / "com"
        deeper.mkdir()

        with pytest.raises(IntentError):
            resolve_filters(deeper, None, persist=False)

    def test_a_link_to_nothing_in_its_place_fails_closed(self, tmp_path: Path) -> None:
        """A broken intent, not an absent one."""
        intent_path(tmp_path).parent.mkdir()
        intent_path(tmp_path).symlink_to(tmp_path / "gone.json")
        deeper = tmp_path / "com"
        deeper.mkdir()

        with pytest.raises(IntentError):
            resolve_filters(deeper, None, persist=False)


class TestScopes:
    def test_every_project_scope_covers_nested_projects(self) -> None:
        intent = FilterIntent.of_options(["secret.txt"], None, False)

        assert intent.for_project("com/parent/child").remove_patterns == {"secret.txt"}

    def test_a_token_pattern_matches_as_git_filter_does(self) -> None:
        intent = FilterIntent.of_options(None, {"com": [TOKEN]}, False)

        assert intent.for_project("com/child").token_digests
        assert not intent.for_project("org/other").token_digests

    def test_a_project_scope_matches_that_project_alone(self) -> None:
        intent = FilterIntent({("project", "com"): FilterPolicy.of(["x"], [], False)})

        assert intent.for_project("com").remove_patterns == {"x"}
        assert not intent.for_project("com/child").remove_patterns

    def test_union_only_ever_adds(self) -> None:
        first = FilterIntent.of_options(["a"], None, True)
        second = FilterIntent.of_options(["b"], None, False)

        both = first.union(second)

        assert both.scopes[ALL_PROJECTS].remove_patterns == {"a", "b"}
        assert both.scopes[ALL_PROJECTS].redact_secrets


class TestResolve:
    def test_options_are_written_down(self, tmp_path: Path) -> None:
        tree = tmp_path / "tree"

        spec = resolve_filters(tree, _options(tree, "secret.txt"), persist=True)

        assert spec is not None
        assert load_intent(tree).for_project("any").remove_patterns == {"secret.txt"}

    def test_a_later_run_without_options_inherits_them(self, tmp_path: Path) -> None:
        tree = tmp_path / "tree"
        resolve_filters(tree, _options(tree, "secret.txt"), persist=True)

        spec = resolve_filters(tree, None, persist=True)

        assert spec is not None
        assert spec.filters_for("new/project").remove_patterns == ["secret.txt"]

    def test_options_only_add(self, tmp_path: Path) -> None:
        tree = tmp_path / "tree"
        resolve_filters(tree, _options(tree, "a.txt"), persist=True)

        spec = resolve_filters(tree, _options(tree, "b.txt"), persist=True)

        assert spec is not None
        assert spec.filters_for("p").remove_patterns == ["b.txt", "a.txt"]
        assert load_intent(tree).for_project("p").remove_patterns == {"a.txt", "b.txt"}

    def test_nothing_decided_creates_nothing(self, tmp_path: Path) -> None:
        tree = tmp_path / "tree"
        _mirror(tmp_path, "plain")

        assert resolve_filters(tree, None, persist=True) is None
        assert not (tree / ".gerrit-clone").exists()

    def test_a_dry_run_writes_nothing(self, tmp_path: Path) -> None:
        tree = tmp_path / "tree"

        spec = resolve_filters(tree, _options(tree, "secret.txt"), persist=False)

        assert spec is not None
        assert not intent_path(tree).exists()

    def test_it_is_found_from_a_subdirectory(self, tmp_path: Path) -> None:
        """Project names stay those of the tree, for --git-filter patterns."""
        tree = tmp_path / "tree"
        resolve_filters(
            tree, _options(tree, git_filter=f"com/parent:{TOKEN}"), persist=True
        )
        (tree / "com").mkdir()

        spec = resolve_filters(tree / "com", None, persist=False)

        assert spec is not None
        assert spec.base_path == tree.resolve()
        assert spec.missing_tokens(tree / "com" / "parent") == 1

    def test_an_unreadable_intent_stops_the_run(self, tmp_path: Path) -> None:
        tree = tmp_path / "tree"
        intent_path(tree).parent.mkdir(parents=True)
        intent_path(tree).write_text("{not json")

        with pytest.raises(IntentError):
            resolve_filters(tree, _options(tree, "x"), persist=True)


class TestGatheringRecords:
    """A tree an earlier release filtered kept only per-repository records."""

    def test_each_repository_record_is_gathered_for_its_project(
        self, tmp_path: Path
    ) -> None:
        mirror = _mirror(tmp_path, "com/filtered")
        _mirror(tmp_path, "com/plain")
        ok, error = apply_content_filters(
            mirror, "com/filtered", remove_patterns=["secret.txt"]
        )
        assert ok, error

        intent = gathered_intent(tmp_path / "tree")

        assert list(intent.scopes) == [("project", "com/filtered")]
        assert intent.for_project("com/filtered").remove_patterns == {"secret.txt"}

    def test_they_are_written_down_before_anything_else(self, tmp_path: Path) -> None:
        """So deleting the repository afterwards loses nothing."""
        mirror = _mirror(tmp_path, "com/filtered")
        assert add_policy(mirror, FilterPolicy.of(["secret.txt"], [], False))
        tree = tmp_path / "tree"

        resolve_filters(tree, None, persist=True)

        assert load_intent(tree).for_project("com/filtered").remove_patterns == {
            "secret.txt"
        }

    def test_each_repository_costs_one_git_call(self, tmp_path: Path) -> None:
        """Every run gathers from every repository in the tree, before
        anything else, so the cost is one process each."""
        for name in ("com/a", "com/b", "com/c"):
            mirror = _mirror(tmp_path, name)
            if name != "com/c":
                assert add_policy(mirror, FilterPolicy.of(["x"], [], False))

        with patch(
            "source_clone.content_origin.run_tracked",
            wraps=run_tracked,
        ) as launched:
            intent = gathered_intent(tmp_path / "tree")

        assert launched.call_count == 3
        assert len(intent.scopes) == 2

    def test_an_earlier_release_finding_is_not_gathered(self, tmp_path: Path) -> None:
        """What it filtered is unknown; the repository is refused on its own."""
        mirror = _mirror(tmp_path, "com/legacy")
        assert add_policy(mirror, FilterPolicy(earlier_release=True))

        assert gathered_intent(tmp_path / "tree").empty

    def test_an_unreadable_record_stops_the_run(self, tmp_path: Path) -> None:
        mirror = _mirror(tmp_path, "com/broken")
        with (mirror / "config").open("a") as config:
            config.write("[broken\n")

        with pytest.raises(IntentError):
            gathered_intent(tmp_path / "tree")


class TestProjectFilters:
    def test_a_token_the_run_lacks_is_missing(self, tmp_path: Path) -> None:
        intent = FilterIntent.of_options(None, {"p": [TOKEN]}, False)
        spec = ContentFilterSpec(None, None, False, tmp_path, intent)

        assert spec.filters_for("p").missing_tokens == 1

    def test_a_token_given_again_is_not(self, tmp_path: Path) -> None:
        intent = FilterIntent.of_options(None, {"p": [TOKEN]}, False)
        spec = ContentFilterSpec(None, {"p": [TOKEN]}, False, tmp_path, intent)

        assert spec.filters_for("p").missing_tokens == 0

    def test_a_different_token_does_not_stand_in(self, tmp_path: Path) -> None:
        intent = FilterIntent.of_options(None, {"p": [TOKEN]}, False)
        spec = ContentFilterSpec(None, {"p": ["other-token"]}, False, tmp_path, intent)

        assert spec.filters_for("p").missing_tokens == 1


class TestFilterRepository:
    def test_missing_tokens_refuse_before_anything_runs(self, tmp_path: Path) -> None:
        intent = FilterIntent.of_options(["x"], {"p": [TOKEN]}, False)
        spec = ContentFilterSpec(None, None, False, tmp_path, intent)

        with patch("source_clone.content_stage.apply_content_filters") as apply:
            reason = filter_repository(spec, tmp_path, "p", 30, apply=apply)

        assert reason is not None
        assert "1 --git-filter token(s)" in reason
        assert not apply.called

    def test_a_shallow_repository_still_has_files_removed(self, tmp_path: Path) -> None:
        spec = ContentFilterSpec(["x"], None, True, tmp_path)
        calls: list[dict[str, object]] = []

        def apply(*_: object, **kwargs: object) -> tuple[bool, str | None]:
            calls.append(kwargs)
            return True, None

        reason = filter_repository(
            spec, tmp_path, "p", 30, is_shallow=lambda _: True, apply=apply
        )

        assert reason == SHALLOW_HISTORY_REFUSED
        assert calls == [
            {
                "remove_patterns": ["x"],
                "git_filter_projects": None,
                "redact_secrets": False,
                "timeout": 30,
            }
        ]

    def test_an_intent_without_tokens_filters_as_decided(self, tmp_path: Path) -> None:
        mirror = _mirror(tmp_path, "proj")
        intent = FilterIntent.of_options(["secret.txt"], None, False)
        spec = ContentFilterSpec(None, None, False, tmp_path / "tree", intent)

        assert filter_repository(spec, mirror, "proj", 60) is None
        assert "secret.txt" not in _git(
            mirror, "log", "--all", "--name-only", "--format="
        )

    def test_its_record_is_json_a_reader_can_audit(self, tmp_path: Path) -> None:
        save_intent(
            tmp_path, FilterIntent.of_options(["secret.txt"], {"p": [TOKEN]}, True)
        )

        data = json.loads(intent_path(tmp_path).read_text())

        assert data["schema"] == 1
        assert {
            "projects": "*",
            "remove": ["secret.txt"],
            "redact_secrets": True,
        } in data["entries"]
