# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""Manifest handling and human-facing reporting for clone runs.

Reads the manifest left by a previous run (warning about configuration
changes), writes the manifest for the current run, logs per-project
outcomes and renders the final summary.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from rich.console import Console

from gerrit_clone.content_spec import manifest_entry
from gerrit_clone.logging import get_logger
from gerrit_clone.models import CloneStatus
from gerrit_clone.rich_status import clone_completed
from gerrit_clone.rich_status import (
    success_rate as show_success_rate,
)

if TYPE_CHECKING:
    from gerrit_clone.models import BatchResult, CloneResult, Config

logger = get_logger(__name__)


def log_project_result(result: CloneResult) -> None:
    """Log the result of a project clone operation.

    Args:
        result: Clone result to log
    """
    if result.status == CloneStatus.SUCCESS:
        logger.debug(f"✓ Successfully cloned {result.project.name}")
    elif result.status == CloneStatus.ALREADY_EXISTS:
        logger.debug(f"≈ Already exists {result.project.name}")
    elif result.status == CloneStatus.FAILED:
        error_summary = (
            result.error_message[:100] + "..."
            if result.error_message and len(result.error_message) > 100
            else result.error_message
        )
        # Log at error level to ensure failures are visible in summaries and external monitoring
        logger.error(
            f"✗ Failed to clone {result.project.name} after {result.attempts} attempts: {error_summary}"
        )
    elif result.status == CloneStatus.SKIPPED:
        logger.debug(f"↷ Skipped {result.project.name}")


def _collect_config_change_warnings(
    manifest: dict[str, Any], config: Config
) -> list[str]:
    """Describe how *config* differs from the manifest's recorded config."""
    warnings: list[str] = []
    if "clone_config" not in manifest:
        return warnings

    old_config = manifest["clone_config"]

    if old_config.get("use_gh_cli") != config.use_gh_cli:
        warnings.append(
            f"Clone method changed: was {'gh CLI' if old_config.get('use_gh_cli') else 'git'}, "
            f"now {'gh CLI' if config.use_gh_cli else 'git'}"
        )

    if old_config.get("use_https") != config.use_https:
        warnings.append(
            f"Protocol changed: was {'HTTPS' if old_config.get('use_https') else 'SSH'}, "
            f"now {'HTTPS' if config.use_https else 'SSH'}"
        )

    if old_config.get("depth") != config.depth:
        warnings.append(
            f"Depth changed: was {old_config.get('depth') or 'full'}, "
            f"now {config.depth or 'full'}"
        )

    return warnings


def check_existing_manifest(
    config: Config, console: Any | None = None
) -> dict[str, Any] | None:
    """Check for existing manifest and warn about configuration changes.

    Args:
        config: Current configuration
        console: Optional Rich console instance for display (created if None)

    Returns:
        Existing manifest data if found, None otherwise
    """
    manifest_path = config.path / config.manifest_filename

    if not manifest_path.exists():
        return None

    try:
        with manifest_path.open() as f:
            manifest: dict[str, Any] = json.load(f)

        warnings = _collect_config_change_warnings(manifest, config)

        if warnings:
            # Create console if not provided (ensures safe display timing)
            if console is None:
                console = Console(stderr=True)
            console.print("\n[yellow]⚠️  Configuration Changes Detected:[/yellow]")
            for warning in warnings:
                console.print(f"  • {warning}")
            console.print(
                "[yellow]Existing repositories will be skipped. "
                "To re-clone with new settings, remove the existing directory first.[/yellow]\n"
            )

        # Show summary of existing clone
        if manifest.get("already_exists", 0) > 0 or manifest.get("succeeded", 0) > 0:
            # Create console if not provided (ensures safe display timing)
            if console is None:
                console = Console(stderr=True)
            total_existing = manifest.get("succeeded", 0) + manifest.get(
                "already_exists", 0
            )
            console.print(
                f"[cyan]ℹ️  Found {total_existing} existing repositories from previous clone[/cyan]\n"  # noqa: RUF001
            )

        return manifest

    except Exception as e:
        logger.debug(f"Could not read existing manifest: {e}")
        return None


def write_manifest(batch_result: BatchResult, config: Config) -> None:
    """Write clone manifest to file.

    Args:
        batch_result: Batch result to write
        config: Configuration with manifest filename
    """
    manifest_path = config.path / config.manifest_filename

    try:
        manifest_data = batch_result.to_dict()
        manifest_data["content_filters"] = manifest_entry(config.content_filters)

        with manifest_path.open("w", encoding="utf-8") as f:
            json.dump(manifest_data, f, indent=2, sort_keys=True)

        logger.debug(f"Wrote clone manifest to [path]{manifest_path}[/path]")

    except Exception as e:
        logger.error(f"Failed to write manifest file: {e}")


def _log_failure_breakdown(batch_result: BatchResult, duration_str: str) -> None:
    """Log the detailed per-failure breakdown of a batch result."""
    failed_results = [r for r in batch_result.results if r.failed]
    logger.debug(
        "Failed projects: %s", ", ".join([r.project.name for r in failed_results])
    )

    logger.debug("=== Clone Summary ===")
    logger.debug("Duration: %s", duration_str)
    logger.debug("Total: %d", batch_result.total_count)
    logger.debug("Success: %d", batch_result.success_count)
    logger.debug("Failed: %d", batch_result.failed_count)
    logger.debug("Skipped: %d", batch_result.skipped_count)

    if failed_results:
        logger.debug("Failed projects:")
        for result in failed_results:
            logger.debug(
                "  - %s: %s",
                result.project.name,
                result.error_message or "Unknown error",
            )

    # Set appropriate exit code for CI/CD
    if batch_result.failed_count > 0:
        logger.debug("Clone completed with %d failures", batch_result.failed_count)


def log_final_summary(batch_result: BatchResult, config: Config) -> None:
    """Log final summary of clone operations.

    Args:
        batch_result: Batch result to summarize
        config: Configuration for quiet flag
    """
    duration_str = f"{batch_result.duration_seconds:.1f}s"

    if batch_result.failed_count == 0:
        # All successful
        logger.debug(
            "Clone completed successfully! %d repositories cloned in %s",
            batch_result.success_count,
            duration_str,
        )
    else:
        # Some failures
        logger.debug(
            "Clone completed with errors: %d succeeded, %d failed in %s",
            batch_result.success_count,
            batch_result.failed_count,
            duration_str,
        )
    clone_completed(batch_result.success_count, batch_result.failed_count, duration_str)

    # Show success rate with Rich status
    if batch_result.total_count > 0:
        success_rate_val = batch_result.success_rate
        logger.debug("Success rate: %.1f%%", success_rate_val)
        show_success_rate(success_rate_val, batch_result.failed_count)

    if batch_result.failed_count > 0 and not config.quiet:
        _log_failure_breakdown(batch_result, duration_str)
