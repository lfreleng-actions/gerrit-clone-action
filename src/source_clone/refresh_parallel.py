# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""Parallel refresh execution with a live progress display.

Owns the concurrency and terminal-reporting half of a bulk refresh: submitting
one :class:`~source_clone.refresh_worker.RefreshWorker` call per repository to
an interruptible thread pool, honouring ``exit_on_error`` by cancelling
outstanding work, and driving the two-line Rich progress display.

Kept separate from :mod:`source_clone.refresh_manager` so the scheduling and
display mechanics can be reviewed independently of the refresh policy that
decides what to run.
"""

from __future__ import annotations

from concurrent.futures import as_completed
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from rich.console import Console, Group
from rich.live import Live
from rich.progress import (
    BarColumn,
    MofNCompleteColumn,
    Progress,
    SpinnerColumn,
    TextColumn,
    TimeElapsedColumn,
)
from rich.text import Text

from source_clone.concurrent_utils import interruptible_executor
from source_clone.logging import get_logger
from source_clone.models import RefreshResult, RefreshStatus
from source_clone.refresh_worker import RefreshWorker

if TYPE_CHECKING:
    from concurrent.futures import Future
    from pathlib import Path

    from rich.progress import TaskID

    from source_clone.content_spec import ContentFilterSpec
    from source_clone.models import Config, RetryPolicy

logger = get_logger(__name__)


class ParallelRefreshMixin:
    """Thread-pool execution of per-repository refreshes with progress output."""

    # Supplied by RefreshManager.__init__; declared here because this layer
    # reads them.
    config: Config | None
    retry_policy: RetryPolicy
    timeout: int
    fetch_only: bool
    prune: bool
    skip_conflicts: bool
    auto_stash: bool
    strategy: str
    filter_gerrit_only: bool
    force: bool
    force_hard: bool
    content_filters: ContentFilterSpec | None
    threads: int
    exit_on_error: bool

    def _execute_parallel_refresh(self, repo_paths: list[Path]) -> list[RefreshResult]:
        """Execute refresh operations in parallel with progress tracking.

        Args:
            repo_paths: List of repository paths to refresh

        Returns:
            List of refresh results
        """
        results: list[RefreshResult] = []
        total = len(repo_paths)

        worker = RefreshWorker(
            config=self.config,
            retry_policy=self.retry_policy,
            timeout=self.timeout,
            fetch_only=self.fetch_only,
            prune=self.prune,
            skip_conflicts=self.skip_conflicts,
            auto_stash=self.auto_stash,
            strategy=self.strategy,
            filter_gerrit_only=self.filter_gerrit_only,
            force=self.force,
            force_hard=self.force_hard,
            content_filters=self.content_filters,
        )

        # Create progress display with two-line layout
        # Line 1: Current repository being processed
        # Line 2: Progress bar + count + time
        current_repo = Text("", style="bold blue")

        progress_bar = Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            BarColumn(bar_width=40),
            MofNCompleteColumn(),
            TextColumn("•"),
            TimeElapsedColumn(),
            transient=False,
        )
        task = progress_bar.add_task("Refreshing repositories", total=total)

        # Combine current repo and progress bar into a two-line display
        display_group = Group(current_repo, progress_bar)

        with (
            Live(
                display_group,
                console=Console(stderr=True),
                refresh_per_second=4,
                transient=False,
            ),
            interruptible_executor(
                max_workers=self.threads,
                thread_name_prefix="refresh",
            ) as executor,
        ):
            # Submit all tasks
            future_to_repo = {
                executor.submit(worker.refresh_repository, repo): repo
                for repo in repo_paths
            }

            for future in as_completed(future_to_repo):
                repo = future_to_repo[future]

                try:
                    result = future.result()
                    results.append(result)

                    self._update_progress(progress_bar, task, result, current_repo)

                    if self.exit_on_error and result.failed:
                        logger.error(
                            f"❌ Exiting due to error in {result.project_name}"
                        )
                        self._cancel_pending(future_to_repo)
                        break

                except Exception as e:
                    # This shouldn't happen as worker catches all exceptions
                    # But just in case...
                    logger.error(f"❌ Unexpected error processing {repo.name}: {e}")
                    failure_result = RefreshResult(
                        path=repo,
                        project_name=repo.name,
                        status=RefreshStatus.FAILED,
                        error_message=f"Unexpected error: {e}",
                        started_at=datetime.now(UTC),
                        completed_at=datetime.now(UTC),
                    )
                    results.append(failure_result)
                    progress_bar.update(task, advance=1)

                    if self.exit_on_error:
                        logger.error("❌ Exiting due to unexpected error")
                        self._cancel_pending(future_to_repo)
                        break

        return results

    @staticmethod
    def _cancel_pending(future_to_repo: dict[Future[RefreshResult], Path]) -> None:
        """Cancel remaining tasks (only those not yet completed).

        Args:
            future_to_repo: Mapping of submitted futures to their repository
        """
        for f in future_to_repo:
            if not f.done():
                f.cancel()

    def _update_progress(
        self,
        progress: Progress,
        task: TaskID,
        result: RefreshResult,
        current_repo: Text,
    ) -> None:
        """Update progress display based on result.

        Args:
            progress: Progress instance
            task: Task ID
            result: Refresh result
            current_repo: Text object for current repo display
        """
        status_emoji = self._get_status_emoji(result.status)

        current_repo.plain = f"{status_emoji} {result.project_name}"

        progress.update(task, advance=1)

    def _get_status_emoji(self, status: RefreshStatus) -> str:
        """Get emoji for refresh status.

        Args:
            status: Refresh status

        Returns:
            Emoji string
        """
        emoji_map = {
            RefreshStatus.SUCCESS: "✅",
            RefreshStatus.UP_TO_DATE: "✓",
            RefreshStatus.FAILED: "❌",
            RefreshStatus.SKIPPED: "⊘",
            RefreshStatus.CONFLICTS: "⚠️",
            RefreshStatus.NOT_GIT_REPO: "⊘",
            RefreshStatus.NOT_GERRIT_REPO: "⊘",
            RefreshStatus.UNCOMMITTED_CHANGES: "⚠️",
            RefreshStatus.DETACHED_HEAD: "⚠️",
            RefreshStatus.PENDING: "⏳",
            RefreshStatus.REFRESHING: "🔄",
        }

        return emoji_map.get(status, "•")
