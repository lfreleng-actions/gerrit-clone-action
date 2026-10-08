# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""Tests for recording which content filters rewrote a repository."""

from __future__ import annotations

import subprocess
from typing import TYPE_CHECKING
from unittest.mock import patch

import pytest

from gerrit_clone import content_policy
from gerrit_clone.content_filter import apply_content_filters
from gerrit_clone.content_origin import NO_PUSH_URL, git_config
from gerrit_clone.content_policy import (
    FilterPolicy,
    PolicyReadError,
    PolicyRecordError,
    add_policy,
    content_filtering,
    record_and_block,
    recorded_policy,
)
from gerrit_clone.content_spec import ContentFilterSpec
from gerrit_clone.models import RefreshStatus
from gerrit_clone.refresh_manager import RefreshManager
from gerrit_clone.refresh_worker import RefreshWorker

if TYPE_CHECKING:
    from pathlib import Path

TOKEN = "sekrit-token-4f9a2c"


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
def mirror(tmp_path: Path) -> Path:
    """A mirror of an upstream holding a file and a token to filter."""
    upstream = tmp_path / "upstream"
    upstream.mkdir()
    _git(upstream, "init", "-q", "-b", "main")
    (upstream / "secret.txt").write_text("hunter2\n")
    (upstream / "file.txt").write_text(f"key={TOKEN}\n")
    _git(upstream, "add", ".")
    _git(upstream, "commit", "-q", "-m", "one")
    repo = tmp_path / "mirror"
    subprocess.run(
        ["git", "clone", "-q", "--mirror", upstream.as_uri(), str(repo)], check=True
    )
    return repo


def _new_commit(repo: Path) -> str:
    """A new commit object, standing in for one a filter rewrote."""
    tree = _git(repo, "rev-parse", "main^{tree}")
    return _git(repo, "commit-tree", tree, "-m", "rewritten")


class TestFilterPolicy:
    def test_a_policy_covers_its_own_subset(self) -> None:
        recorded = FilterPolicy.of(["secret.txt"], [TOKEN], redact_secrets=True)
        wider = FilterPolicy.of(["secret.txt", "*.jar"], [TOKEN, "other"], True)

        assert wider.covers(recorded)
        assert not recorded.covers(wider)

    @pytest.mark.parametrize(
        "requested",
        [
            FilterPolicy.of(["unrelated.txt"], [TOKEN], redact_secrets=True),
            FilterPolicy.of(["secret.txt"], ["another-token"], redact_secrets=True),
            FilterPolicy.of(["secret.txt"], [TOKEN], redact_secrets=False),
        ],
        ids=["other-file", "other-token", "no-redaction"],
    )
    def test_a_different_filter_does_not_cover(self, requested: FilterPolicy) -> None:
        """Any filter at all is not enough; it must be the same one."""
        recorded = FilterPolicy.of(["secret.txt"], [TOKEN], redact_secrets=True)

        assert not requested.covers(recorded)

    def test_what_is_missing_is_described_without_the_token(self) -> None:
        recorded = FilterPolicy.of(["secret.txt"], [TOKEN], redact_secrets=True)

        missing = FilterPolicy().describe_missing(recorded)

        assert "--remove-files secret.txt" in missing
        assert "--git-filter for 1 token(s)" in missing
        assert "--redact-secrets" in missing
        assert TOKEN not in missing


class TestRecordedPolicy:
    def test_tokens_are_recorded_only_as_digests(self, mirror: Path) -> None:
        policy = FilterPolicy.of(["a file.txt"], [TOKEN], redact_secrets=True)

        assert add_policy(mirror, policy)

        assert recorded_policy(mirror) == policy
        assert TOKEN not in (mirror / "config").read_text()

    def test_a_repository_never_filtered_has_an_empty_policy(
        self, mirror: Path
    ) -> None:
        assert recorded_policy(mirror).empty


class TestContentFiltering:
    def test_a_rewrite_records_the_policy_and_blocks_pushing(
        self, mirror: Path
    ) -> None:
        policy = FilterPolicy.of(["secret.txt"], [], redact_secrets=False)

        with content_filtering(mirror, policy) as errors:
            _git(mirror, "update-ref", "refs/heads/main", _new_commit(mirror))

        assert errors == []
        assert recorded_policy(mirror) == policy
        assert _git(mirror, "config", "remote.origin.pushurl") == NO_PUSH_URL

    def test_the_policy_is_recorded_before_any_rewrite(self, mirror: Path) -> None:
        """So a run interrupted part-way still leaves the record."""
        policy = FilterPolicy.of(["secret.txt"], [], redact_secrets=False)

        with content_filtering(mirror, policy):
            assert recorded_policy(mirror) == policy

    def test_a_filter_that_changed_nothing_leaves_no_record(self, mirror: Path) -> None:
        """``--remove-files`` runs everywhere, matching or not."""
        with content_filtering(mirror, FilterPolicy.of(["absent.txt"], [], False)):
            pass

        assert recorded_policy(mirror).empty

    def test_an_earlier_record_survives_a_filter_that_changed_nothing(
        self, mirror: Path
    ) -> None:
        earlier = FilterPolicy.of(["secret.txt"], [], redact_secrets=False)
        assert add_policy(mirror, earlier)

        with content_filtering(mirror, FilterPolicy.of(["absent.txt"], [], False)):
            pass

        assert recorded_policy(mirror) == earlier

    def test_a_failed_write_never_shrinks_an_earlier_record(self, mirror: Path) -> None:
        """A full disk fails every write, but not the removals.

        Rewriting the record by removing it first would leave the mirror
        recording nothing -- and an unfiltered refresh would then force
        its original content back.
        """
        earlier = FilterPolicy.of(["secret.txt"], [TOKEN], redact_secrets=True)
        assert add_policy(mirror, earlier)
        real = git_config

        def disk_full(
            repo: Path, *args: str
        ) -> subprocess.CompletedProcess[str] | None:
            if "--add" in args:
                return subprocess.CompletedProcess(args, 1, "", "No space left")
            return real(repo, *args)

        refused = False
        with patch.object(content_policy, "git_config", disk_full):
            try:
                with content_filtering(
                    mirror, FilterPolicy.of(["a.txt", "z.txt"], [], False)
                ):
                    pass
            except PolicyRecordError:
                refused = True

        assert refused
        assert recorded_policy(mirror).covers(earlier)

    def test_a_rewrite_that_raises_part_way_keeps_the_record(
        self, mirror: Path
    ) -> None:
        policy = FilterPolicy.of(["secret.txt"], [], redact_secrets=False)
        raised = False
        try:
            with content_filtering(mirror, policy):
                _git(mirror, "update-ref", "refs/heads/main", _new_commit(mirror))
                raise RuntimeError("filter-repo failed part-way")
        except RuntimeError:
            raised = True

        assert raised
        assert recorded_policy(mirror) == policy

    def test_a_policy_that_cannot_be_recorded_stops_the_filtering(
        self, mirror: Path
    ) -> None:
        """Fail closed: unrecorded, a later refresh could not refuse it."""
        before = _git(mirror, "for-each-ref")
        with patch.object(content_policy, "add_policy", return_value=False):
            outcome = apply_content_filters(
                mirror, "mirror", remove_patterns=["secret.txt"]
            )

        assert outcome[0] is False
        assert "not filtering it" in (outcome[1] or "")
        assert _git(mirror, "for-each-ref") == before

    def test_raising_policy_record_error_directly(self, mirror: Path) -> None:
        with (
            patch.object(content_policy, "add_policy", return_value=False),
            pytest.raises(PolicyRecordError),
            content_filtering(mirror, FilterPolicy.of(["x"], [], False)),
        ):
            pass


class TestUnreadablePolicy:
    """A record that cannot be read must not read as "never filtered"."""

    @staticmethod
    def _unreadable(repo: Path, *args: str) -> subprocess.CompletedProcess[str] | None:
        if "--get-regexp" in args:
            return subprocess.CompletedProcess(args, 128, "", "bad config line")
        return git_config(repo, *args)

    def test_a_read_error_raises(self, mirror: Path) -> None:
        with (
            patch.object(content_policy, "git_config", self._unreadable),
            pytest.raises(PolicyReadError),
        ):
            recorded_policy(mirror)

    def test_an_unset_key_still_reads_as_empty(self, mirror: Path) -> None:
        assert recorded_policy(mirror).empty

    def test_a_refresh_fails_closed_on_it(self, mirror: Path) -> None:
        """Not a fetch of every upstream ref over what may be filtered."""
        before = _git(mirror, "for-each-ref")
        upstream = mirror.parent / "upstream"
        (upstream / "file.txt").write_text("two\n")
        _git(upstream, "commit", "-q", "-am", "two")
        worker = RefreshWorker(filter_gerrit_only=False, ssh_jitter_seconds=0)

        with patch.object(content_policy, "git_config", self._unreadable):
            result = worker.refresh_repository(mirror)

        assert result.status == RefreshStatus.FAILED
        assert "content-filter policy" in (result.error_message or "")
        assert _git(mirror, "for-each-ref") == before

    def test_the_dry_run_predicts_the_failure(self, mirror: Path) -> None:
        manager = RefreshManager(dry_run=True, filter_gerrit_only=False)

        with patch.object(content_policy, "git_config", self._unreadable):
            batch = manager.refresh_repositories(mirror.parent, [mirror])

        [prediction] = batch.results
        assert prediction.status == RefreshStatus.FAILED
        assert "content-filter policy" in (prediction.error_message or "")

    def test_a_corrupt_config_fails_before_the_gerrit_gate(self, mirror: Path) -> None:
        """It hides the remotes too: a failure, not "not Gerrit".

        The default, Gerrit-only refresh and its dry run alike.
        """
        with (mirror / "config").open("a") as config:
            config.write("[broken\n")

        result = RefreshWorker(ssh_jitter_seconds=0).refresh_repository(mirror)
        batch = RefreshManager(dry_run=True).refresh_repositories(
            mirror.parent, [mirror]
        )

        [prediction] = batch.results
        for outcome in (result, prediction):
            assert outcome.status == RefreshStatus.FAILED
            assert "content-filter policy" in (outcome.error_message or "")

    def test_filtering_is_refused_on_it(self, mirror: Path) -> None:
        before = _git(mirror, "for-each-ref")

        with patch.object(content_policy, "git_config", self._unreadable):
            ok, error = apply_content_filters(
                mirror, "mirror", remove_patterns=["secret.txt"]
            )

        assert not ok
        assert "content-filter policy" in (error or "")
        assert _git(mirror, "for-each-ref") == before


class TestPushBlockedFirst:
    """Rewritten history must never be pushable, not even for a moment."""

    def test_pushing_is_blocked_before_any_filter_runs(self, mirror: Path) -> None:
        with content_filtering(mirror, FilterPolicy.of(["secret.txt"], [], False)):
            pushurl = _git(mirror, "config", "remote.origin.pushurl")

        assert pushurl == NO_PUSH_URL

    def test_filtering_is_refused_if_pushing_cannot_be_blocked(
        self, mirror: Path
    ) -> None:
        before = _git(mirror, "for-each-ref")

        with patch.object(content_policy, "block_pushes", return_value=False):
            ok, error = apply_content_filters(
                mirror, "mirror", remove_patterns=["secret.txt"]
            )

        assert not ok
        assert "block pushing" in (error or "")
        assert _git(mirror, "for-each-ref") == before
        assert recorded_policy(mirror).empty

    def test_a_filter_that_changed_nothing_restores_the_push_urls(
        self, mirror: Path
    ) -> None:
        _git(mirror, "config", "remote.origin.pushurl", "https://example.org/push")

        with content_filtering(mirror, FilterPolicy.of(["absent.txt"], [], False)):
            pass

        assert _git(mirror, "config", "remote.origin.pushurl") == (
            "https://example.org/push"
        )


class TestContentFilterSpec:
    def test_no_options_means_no_spec(self, tmp_path: Path) -> None:
        assert ContentFilterSpec.from_options(None, None, False, tmp_path) is None

    def test_tokens_are_matched_by_hierarchical_project_name(
        self, tmp_path: Path
    ) -> None:
        spec = ContentFilterSpec.from_options(
            "secret.txt", f"com/parent/child:{TOKEN}", False, tmp_path
        )
        assert spec is not None

        child = spec.policy_for(tmp_path / "com/parent/child")
        other = spec.policy_for(tmp_path / "com/other")

        assert child.covers(FilterPolicy.of(["secret.txt"], [TOKEN], False))
        assert not other.covers(FilterPolicy.of([], [TOKEN], False))

    def test_the_project_is_named_alike_through_a_symlinked_base(
        self, tmp_path: Path
    ) -> None:
        """Unresolved, the paths never compare and only the leaf is left."""
        real = tmp_path / "real"
        (real / "com/parent/child").mkdir(parents=True)
        link = tmp_path / "link"
        link.symlink_to(real)
        spec = ContentFilterSpec.from_options("x", None, False, link)
        assert spec is not None

        assert spec.project_name(real / "com/parent/child") == "com/parent/child"
        assert spec.project_name(link / "com/parent/child") == "com/parent/child"

    def test_tokens_are_kept_out_of_its_repr(self, tmp_path: Path) -> None:
        spec = ContentFilterSpec.from_options(None, f"proj:{TOKEN}", False, tmp_path)

        assert TOKEN not in repr(spec)


class TestRecordAndBlock:
    def test_unreadable_push_urls_refuse_it_before_anything_changes(
        self, tmp_path: Path
    ) -> None:
        """Blocking could fail part-way, with nothing to put back."""
        repo = tmp_path / "repo"
        subprocess.run(["git", "init", "-q", "--bare", str(repo)], check=True)
        before = (repo / "config").read_text()

        with (
            patch("gerrit_clone.content_policy.push_urls", return_value=None),
            pytest.raises(PolicyRecordError, match="push URLs"),
        ):
            record_and_block(repo, FilterPolicy.of(["secret.txt"], [], False))

        assert (repo / "config").read_text() == before
