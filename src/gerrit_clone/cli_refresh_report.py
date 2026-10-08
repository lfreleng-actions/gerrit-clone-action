# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""Result reporting for the ``refresh`` command.

Renders the end-of-run summary and writes the refresh manifest.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from gerrit_clone.logging import get_logger

if TYPE_CHECKING:
    from pathlib import Path

    from rich.console import Console

    from gerrit_clone.models import RefreshBatchResult

logger = get_logger(__name__)


def show_refresh_results(
    console: Console, result: RefreshBatchResult, dry_run: bool
) -> None:
    """Display refresh results summary.

    Args:
        console: Rich console
        result: Refresh batch result
        dry_run: Whether this was a dry run
    """
    console.print()
    console.print("[bold]Refresh Summary[/bold]")
    console.print("─" * 60)

    # Overall stats
    console.print(f"Total Repositories: [cyan]{result.total_count}[/cyan]")
    console.print(f"Duration: [cyan]{result.duration_seconds:.1f}s[/cyan]")
    console.print()

    # Status breakdown
    if dry_run:
        console.print("[bold]Dry Run Results:[/bold]")
    else:
        console.print("[bold]Results:[/bold]")

    console.print(f"  ✅ Successful: [green]{result.success_count}[/green]")
    console.print(f"  ✓  Up-to-date: [blue]{result.up_to_date_count}[/blue]")
    console.print(f"  🔄 Updated: [cyan]{result.updated_count}[/cyan]")
    console.print(f"  ❌ Failed: [red]{result.failed_count}[/red]")
    console.print(f"  ⊘  Skipped: [yellow]{result.skipped_count}[/yellow]")
    console.print(f"  ⚠️  Conflicts: [yellow]{result.conflicts_count}[/yellow]")
    console.print()

    if not dry_run and result.total_commits_pulled > 0:
        console.print(
            f"Repositories Updated: [cyan]{result.total_commits_pulled}[/cyan]"
        )
        console.print(f"Total Files Changed: [cyan]{result.total_files_changed}[/cyan]")
        console.print()

    # Show failed/conflict details
    failed_results = [r for r in result.results if r.failed or r.has_conflicts]
    if failed_results:
        console.print("[bold yellow]Issues:[/bold yellow]")
        for r in failed_results[:10]:  # Show first 10
            status_emoji = "❌" if r.failed else "⚠️"
            console.print(
                f"  {status_emoji} {r.project_name}: {r.error_message or r.status.value}"
            )

        if len(failed_results) > 10:
            console.print(
                f"  ... and {len(failed_results) - 10} more (see manifest for details)"
            )
        console.print()


def write_refresh_manifest(
    manifest_path: Path,
    result: RefreshBatchResult,
    content_filters: dict[str, Any] | None = None,
) -> None:
    """Write refresh manifest to JSON file.

    Args:
        manifest_path: Path to write manifest
        result: Refresh batch result
        content_filters: Summary of the tree's filter intent, if any
    """
    data = {**result.to_dict(), "content_filters": content_filters}
    try:
        with manifest_path.open("w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
    except Exception as e:
        logger.warning(f"Failed to write refresh manifest: {e}")
