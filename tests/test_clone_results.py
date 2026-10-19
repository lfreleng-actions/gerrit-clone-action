# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""Tests for the RefreshStatus -> CloneStatus mapping.

A refresh that did not run to completion must not be reported as a
repository confirmed up to date.  The mapping is therefore exhaustive:
every ``RefreshStatus`` member has an explicit ``CloneStatus``, and a
member added without one defaults to ``FAILED`` rather than inheriting a
success status.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from source_clone.clone_results import (
    _REFRESH_TO_CLONE_STATUS,
    build_refresh_result,
)
from source_clone.models import (
    CloneResult,
    CloneStatus,
    Project,
    ProjectState,
    RefreshResult,
    RefreshStatus,
)

PROJECT = Project(name="example/repo", state=ProjectState.ACTIVE)
TARGET = Path("/tmp/example/repo")

#: Statuses the clone pipeline must report as failures.
UNSUCCESSFUL_STATUSES = [
    RefreshStatus.FAILED,
    RefreshStatus.CONFLICTS,
    RefreshStatus.NOT_GIT_REPO,
    RefreshStatus.NOT_GERRIT_REPO,
    RefreshStatus.UNCOMMITTED_CHANGES,
    RefreshStatus.DETACHED_HEAD,
    RefreshStatus.PENDING,
    RefreshStatus.REFRESHING,
]


def _refresh(
    status: RefreshStatus,
    *,
    was_behind: bool = False,
    error_message: str | None = None,
) -> RefreshResult:
    return RefreshResult(
        path=TARGET,
        project_name=PROJECT.name,
        status=status,
        started_at=datetime.now(UTC),
        was_behind=was_behind,
        error_message=error_message,
    )


def _build(refresh_result: RefreshResult) -> CloneResult:
    return build_refresh_result(PROJECT, TARGET, refresh_result, datetime.now(UTC))


class TestMappingCompleteness:
    """The table must cover the enum, not a subset of it."""

    def test_every_refresh_status_is_mapped(self) -> None:
        """A new RefreshStatus member must be given an explicit mapping."""
        assert set(_REFRESH_TO_CLONE_STATUS) == set(RefreshStatus)

    def test_no_unsuccessful_status_maps_to_a_success_status(self) -> None:
        """Only SUCCESS and UP_TO_DATE may map to a success status."""
        successful = {CloneStatus.VERIFIED, CloneStatus.REFRESHED}
        for status in UNSUCCESSFUL_STATUSES:
            assert _REFRESH_TO_CLONE_STATUS[status] not in successful


class TestSuccessfulRefresh:
    """Completed refreshes keep their previous reporting."""

    @pytest.mark.parametrize(
        "status", [RefreshStatus.SUCCESS, RefreshStatus.UP_TO_DATE]
    )
    def test_unchanged_repository_is_verified(self, status: RefreshStatus) -> None:
        result = _build(_refresh(status))

        assert result.status == CloneStatus.VERIFIED
        assert result.error_message is None
        assert result.was_refreshed is False

    @pytest.mark.parametrize(
        "status", [RefreshStatus.SUCCESS, RefreshStatus.UP_TO_DATE]
    )
    def test_updated_repository_is_refreshed(self, status: RefreshStatus) -> None:
        result = _build(_refresh(status, was_behind=True))

        assert result.status == CloneStatus.REFRESHED
        assert result.error_message is None
        assert result.was_refreshed is True
        assert result.refresh_had_updates is True


class TestUnsuccessfulRefresh:
    """The regression: these used to report as VERIFIED."""

    @pytest.mark.parametrize("status", UNSUCCESSFUL_STATUSES)
    def test_status_is_reported_as_failed(self, status: RefreshStatus) -> None:
        result = _build(_refresh(status))

        assert result.status == CloneStatus.FAILED

    @pytest.mark.parametrize("status", UNSUCCESSFUL_STATUSES)
    def test_error_message_names_the_refresh_status(
        self, status: RefreshStatus
    ) -> None:
        """An operator must be able to tell which status stopped the refresh."""
        result = _build(_refresh(status, error_message="git said no"))

        assert result.error_message is not None
        assert "git said no" in result.error_message
        if status is not RefreshStatus.FAILED:
            assert status.value in result.error_message

    @pytest.mark.parametrize("status", UNSUCCESSFUL_STATUSES)
    def test_failure_does_not_claim_to_have_refreshed(
        self, status: RefreshStatus
    ) -> None:
        """``was_behind`` records intent, so a failure must not report success."""
        result = _build(_refresh(status, was_behind=True))

        assert result.was_refreshed is False
        assert result.refresh_had_updates is False

    def test_conflicts_are_distinguishable_from_up_to_date(self) -> None:
        conflicted = _build(_refresh(RefreshStatus.CONFLICTS))
        up_to_date = _build(_refresh(RefreshStatus.UP_TO_DATE))

        assert conflicted.status != up_to_date.status
        assert conflicted.status == CloneStatus.FAILED

    def test_failed_keeps_its_original_wording(self) -> None:
        """The FAILED message predates the mapping and is unchanged."""
        result = _build(_refresh(RefreshStatus.FAILED, error_message="boom"))

        assert result.error_message == "Refresh failed: boom"


class TestSkippedRefresh:
    """A skipped repository is neither verified nor failed."""

    def test_skipped_maps_to_skipped(self) -> None:
        result = _build(_refresh(RefreshStatus.SKIPPED))

        assert result.status == CloneStatus.SKIPPED
        assert result.success is False
        assert result.failed is False

    def test_skip_reason_is_carried_through(self) -> None:
        result = _build(
            _refresh(RefreshStatus.SKIPPED, error_message="no remote configured")
        )

        assert result.error_message == "no remote configured"
