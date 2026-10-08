# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""Runs on one tree take turns extending its filter intent.

Each run reads the intent, adds to it and writes it back.  Two processes
doing that at once would each write what they read plus their own
additions, and the last write would win, undoing the filtering the
other had just decided.  A lock file beside the intent serialises them.
"""

from __future__ import annotations

import subprocess
import sys
import time
from contextlib import contextmanager, suppress
from typing import TYPE_CHECKING

import pytest

from gerrit_clone.content_intent import (
    FilterIntent,
    IntentError,
    load_intent,
    save_intent,
)
from gerrit_clone.content_intent_resolve import resolve_filters
from gerrit_clone.content_spec import ContentFilterSpec

if TYPE_CHECKING:
    from collections.abc import Generator
    from pathlib import Path

#: Another run adding ``b.txt`` to the tree given as its argument.
_OTHER_RUN = """
import sys
from pathlib import Path

from gerrit_clone.content_intent_resolve import resolve_filters
from gerrit_clone.content_spec import ContentFilterSpec

root = Path(sys.argv[1])
resolve_filters(root, ContentFilterSpec(["b.txt"], None, False, root), persist=True)
"""


def _options(root: Path, pattern: str) -> ContentFilterSpec:
    return ContentFilterSpec([pattern], None, False, root)


def _patterns(root: Path) -> set[str]:
    return set(load_intent(root).for_project("any").remove_patterns)


@pytest.fixture
def tree(tmp_path: Path) -> Path:
    root = tmp_path / "tree"
    root.mkdir()
    return root


@contextmanager
def _holding(tree: Path) -> Generator[None, None, None]:
    """The tree's intent lock, held with ``fcntl`` as another run holds it.

    Independent of the code under test; there is no ``fcntl`` on Windows,
    which :class:`TestAnyPlatform` covers instead.
    """
    fcntl = pytest.importorskip("fcntl")
    lock = tree / ".gerrit-clone" / "filter-policy.lock"
    lock.parent.mkdir(exist_ok=True)
    with lock.open("a") as stream:
        fcntl.flock(stream, fcntl.LOCK_EX)
        yield


@pytest.fixture
def held(tree: Path) -> Generator[None, None, None]:
    with _holding(tree):
        yield


class TestConcurrentRuns:
    def test_both_runs_additions_survive(self, tree: Path) -> None:
        """This run read the intent, then another started before it wrote."""
        with _holding(tree):
            read = load_intent(tree)
            other = subprocess.Popen([sys.executable, "-c", _OTHER_RUN, str(tree)])
            # Unserialised, the other run finishes in this time; serialised,
            # it waits for the lock this test holds.
            with suppress(subprocess.TimeoutExpired):
                other.wait(timeout=10)
            save_intent(
                tree, read.union(FilterIntent.of_options(["a.txt"], None, False))
            )
        assert other.wait(timeout=60) == 0

        assert _patterns(tree) == {"a.txt", "b.txt"}

    def test_the_lock_is_released_for_the_next_run(self, tree: Path) -> None:
        resolve_filters(tree, _options(tree, "a.txt"), persist=True)
        resolve_filters(tree, _options(tree, "b.txt"), persist=True)

        assert _patterns(tree) == {"a.txt", "b.txt"}

    def test_a_waiting_run_adds_to_what_the_other_wrote(self, tree: Path) -> None:
        """The run re-reads the intent once it holds the lock."""
        other = subprocess.Popen([sys.executable, "-c", _OTHER_RUN, str(tree)])
        resolve_filters(tree, _options(tree, "a.txt"), persist=True)
        assert other.wait(timeout=60) == 0

        assert _patterns(tree) == {"a.txt", "b.txt"}


class TestWaiting:
    def test_a_run_gives_up_with_a_clear_error(
        self, tree: Path, held: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr("gerrit_clone.content_intent.LOCK_WAIT", 0.3)
        started = time.monotonic()

        with pytest.raises(IntentError, match="another gerrit-clone run"):
            resolve_filters(tree, _options(tree, "a.txt"), persist=True)

        assert time.monotonic() - started < 10
        assert load_intent(tree).empty

    def test_a_run_adding_nothing_does_not_wait(self, tree: Path, held: None) -> None:
        """Nothing to write, nothing to serialise."""
        started = time.monotonic()

        resolve_filters(tree, None, persist=True)

        assert time.monotonic() - started < 5

    def test_a_dry_run_does_not_wait(self, tree: Path, held: None) -> None:
        started = time.monotonic()

        resolve_filters(tree, _options(tree, "a.txt"), persist=False)

        assert time.monotonic() - started < 5

    def test_a_tree_without_filters_gets_no_lock_file(self, tree: Path) -> None:
        resolve_filters(tree, None, persist=True)

        assert not (tree / ".gerrit-clone").exists()


#: Holds the tree's lock through intent_locked until told to let go.
_HOLDER = """
import sys
import time
from pathlib import Path

from gerrit_clone.content_intent import intent_locked

root, ready, release = map(Path, sys.argv[1:])
with intent_locked(root):
    ready.touch()
    deadline = time.monotonic() + 60
    while not release.exists() and time.monotonic() < deadline:
        time.sleep(0.05)
"""


class TestAnyPlatform:
    """Through intent_locked itself, so msvcrt on Windows is covered too."""

    def test_a_run_waits_for_another_process_and_then_proceeds(
        self, tree: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        ready, release = tree.parent / "ready", tree.parent / "release"
        holder = subprocess.Popen(
            [sys.executable, "-c", _HOLDER, str(tree), str(ready), str(release)]
        )
        try:
            deadline = time.monotonic() + 60
            while not ready.exists() and time.monotonic() < deadline:
                time.sleep(0.05)
            assert ready.exists(), "the other process never took the lock"
            monkeypatch.setattr("gerrit_clone.content_intent.LOCK_WAIT", 0.3)

            with pytest.raises(IntentError, match="another gerrit-clone run"):
                resolve_filters(tree, _options(tree, "a.txt"), persist=True)
        finally:
            release.touch()
            assert holder.wait(timeout=60) == 0
        resolve_filters(tree, _options(tree, "a.txt"), persist=True)

        assert _patterns(tree) == {"a.txt"}
