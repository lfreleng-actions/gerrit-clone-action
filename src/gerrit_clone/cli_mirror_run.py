# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""Execution pipeline for the ``mirror`` command.

Resolves credentials and the target GitHub organization, discovers the Gerrit
projects to mirror, runs the mirror operation and reports the outcome.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import TYPE_CHECKING, NoReturn

import typer

from gerrit_clone import cli_hooks
from gerrit_clone.cli_app import format_version_string
from gerrit_clone.cli_mirror_setup import (
    apply_http_credentials,
    authenticate,
    build_config,
    resolve_discovery_method,
    resolve_org,
    resolve_project_filters,
    start_logging,
    validate_request,
)
from gerrit_clone.content_filter import normalize_file_patterns, parse_git_filter_spec
from gerrit_clone.content_intent import IntentError
from gerrit_clone.content_spec import manifest_entry
from gerrit_clone.error_codes import ExitCode
from gerrit_clone.mirror_manager import (
    MirrorBatchResult,
    MirrorManager,
    filter_projects_by_hierarchy,
)
from gerrit_clone.rich_status import show_error_summary

if TYPE_CHECKING:
    from pathlib import Path

    from rich.console import Console

    from gerrit_clone.cli_mirror_models import MirrorRequest
    from gerrit_clone.cli_session import CliSession
    from gerrit_clone.content_spec import ContentFilterSpec
    from gerrit_clone.mirror_models import MirrorResult
    from gerrit_clone.models import Config, Project


def run_mirror(request: MirrorRequest, session: CliSession) -> None:
    """Run the mirror pipeline for an already parsed command line."""
    console = session.console
    validate_request(request, console)

    if request.use_https:
        apply_http_credentials(request, console)

    # Show startup banner
    if not request.quiet:
        console.print(format_version_string(command="mirror"))

    start_logging(request, session)

    if request.verbose and session.log_file_path:
        console.print(f"📝 Logging to: [cyan]{session.log_file_path}[/cyan]")

    if not request.quiet:
        console.print("🔑 Authenticating with GitHub...")

    github_api = authenticate(request, console)
    org = resolve_org(request, console, github_api)
    project_filters, exclude_filters = resolve_project_filters(request, console)

    remove_file_patterns = (
        normalize_file_patterns([request.remove_files])
        if request.remove_files
        else None
    )
    git_filter_projects = (
        parse_git_filter_spec(request.git_filter) if request.git_filter else None
    )

    config = build_config(request, resolve_discovery_method(request, console))

    if not request.quiet:
        console.print(f"🌐 Connecting to Gerrit: [cyan]{request.server}[/cyan]")

    projects_to_mirror = _select_projects(
        request, console, config, project_filters, exclude_filters
    )

    mirror_manager = MirrorManager(
        config=config,
        github_api=github_api,
        github_org=org,
        recreate=request.recreate,
        overwrite=request.overwrite,
        github_token=request.github_token,
        set_default_branch=request.set_default_branch,
        fix_default_branch=request.fix_default_branch,
        remove_file_patterns=remove_file_patterns,
        git_filter_projects=git_filter_projects,
        redact_secrets=request.redact_secrets,
    )

    if not projects_to_mirror:
        # Nothing to mirror, but the run's filters still decide for the
        # projects a later run selects: the manager records them.
        _mirror(mirror_manager, [], console)
        raise typer.Exit(0)

    started_at = datetime.now(UTC)
    if not request.quiet:
        console.print("🚀 Starting mirror operation...")

    results = _mirror(mirror_manager, projects_to_mirror, console)

    completed_at = datetime.now(UTC)

    batch_result = MirrorBatchResult(
        results=results,
        started_at=started_at,
        completed_at=completed_at,
        github_org=org,
        gerrit_host=request.server,
    )

    _write_manifest(request, console, batch_result, mirror_manager.resolved_filters)

    # Show summary
    if not request.quiet:
        _show_summary(request, console, config, batch_result)

    # Close GitHub API client
    github_api.close()

    _finish(request, session, batch_result)


def _mirror(
    mirror_manager: MirrorManager, projects: list[Project], console: Console
) -> list[MirrorResult]:
    """Mirror *projects*; a tree whose filter intent is unreadable stops the run."""
    try:
        return mirror_manager.mirror_projects(projects)
    except IntentError as exc:
        console.print(f"[red]❌ {exc}[/red]")
        raise typer.Exit(ExitCode.CONFIGURATION_ERROR) from exc


def _select_projects(
    request: MirrorRequest,
    console: Console,
    config: Config,
    project_filters: list[str],
    exclude_filters: list[str],
) -> list[Project]:
    """Discover projects and apply the include/exclude filters.

    Returns:
        The projects to mirror; empty, after saying so, if none are.
    """
    all_projects, _discovery_stats = cli_hooks.discover_projects(config)

    if not all_projects:
        console.print("[yellow]No projects found on Gerrit server[/yellow]")
        return []

    # Filter projects by include/exclude patterns
    if project_filters or exclude_filters:
        projects_to_mirror = filter_projects_by_hierarchy(
            all_projects,
            project_filters,
            exclude_patterns=exclude_filters or None,
        )
    else:
        projects_to_mirror = all_projects

    if not projects_to_mirror:
        console.print("[yellow]No projects matched the specified filters[/yellow]")
        return []

    if not request.quiet:
        console.print(
            f"📦 Found [cyan]{len(projects_to_mirror)}[/cyan] projects to mirror"
        )
    return projects_to_mirror


def _write_manifest(
    request: MirrorRequest,
    console: Console,
    batch_result: MirrorBatchResult,
    spec: ContentFilterSpec | None,
) -> None:
    """Write the mirror manifest to the output path."""
    manifest_path: Path = request.output_path / request.manifest_filename
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    data = {**batch_result.to_dict(), "content_filters": manifest_entry(spec)}
    with manifest_path.open("w") as f:
        json.dump(data, f, indent=2)

    if not request.quiet:
        console.print(f"✓ Manifest written to: [cyan]{manifest_path}[/cyan]")


def _show_summary(
    request: MirrorRequest,
    console: Console,
    config: Config,
    batch_result: MirrorBatchResult,
) -> None:
    """Print the end-of-run mirror summary."""
    resolved_discovery = config.discovery_method
    discovery_label = resolved_discovery.value.upper() if resolved_discovery else "SSH"
    console.print("[bold]Mirror Summary[/bold]")
    console.print(f"  Discovery Method: [cyan]{discovery_label}[/cyan]")
    console.print(
        f"  Clone Protocol: [cyan]{'HTTPS' if request.use_https else 'SSH'}[/cyan]"
    )
    console.print(f"  Skip Archived: [cyan]{request.skip_archived}[/cyan]")
    console.print(f"  Total: {batch_result.total_count}")
    console.print(f"  [green]Succeeded: {batch_result.success_count}[/green]")
    if batch_result.total_count != batch_result.success_count:
        console.print(f"  [red]Failed: {batch_result.failed_count}[/red]")
        console.print(f"  [yellow]Skipped: {batch_result.skipped_count}[/yellow]")
    console.print(f"  Duration: {batch_result.duration_seconds:.1f}s")


def _finish(
    request: MirrorRequest, session: CliSession, batch_result: MirrorBatchResult
) -> NoReturn:
    """Report collected errors and exit with the appropriate code."""
    # Show error summary if there were issues
    error_collector = session.error_collector
    if error_collector and not request.quiet:
        errors = [
            record.message
            for record in error_collector.errors + error_collector.critical_errors
        ]
        warnings = [record.message for record in error_collector.warnings]
        if errors or warnings:
            show_error_summary(session.console, errors, warnings)

    session.write_summary()

    # Exit with appropriate code
    file_logger = session.file_logger
    if batch_result.failed_count > 0:
        if file_logger:
            file_logger.debug(
                "Mirror completed with %d failures",
                batch_result.failed_count,
            )
        raise typer.Exit(ExitCode.CLONE_ERROR)
    else:
        if file_logger:
            file_logger.debug("Mirror completed successfully")
        raise typer.Exit(0)


def handle_command_error(
    session: CliSession,
    error: Exception,
    *,
    label: str,
    log_format: str,
    exit_code: ExitCode,
    verbose: bool,
) -> NoReturn:
    """Report a known failure mode and exit with its status code."""
    session.console.print(f"[red]{label}:[/red] {error}")
    if session.file_logger:
        session.file_logger.error(log_format, str(error))
    session.write_summary()
    if verbose:
        session.console.print_exception()
    raise typer.Exit(exit_code) from error


def handle_interrupt(session: CliSession) -> NoReturn:
    """Report a user interrupt and exit."""
    session.console.print("\n[yellow]Mirror operation cancelled by user[/yellow]")
    if session.file_logger:
        session.file_logger.warning("Operation cancelled by user (KeyboardInterrupt)")
    session.write_summary()
    # Flush console to ensure message is displayed before exit
    if hasattr(session.console.file, "flush"):
        session.console.file.flush()
    raise typer.Exit(ExitCode.INTERRUPT) from None


def handle_crash(session: CliSession, error: Exception, *, verbose: bool) -> NoReturn:
    """Report an unexpected failure and exit."""
    session.console.print(f"[red]Unexpected Error:[/red] {error}")
    if session.file_logger:
        session.file_logger.critical("Mirror crashed: %s", str(error), exc_info=True)
    if session.error_collector:
        session.error_collector.add_critical_error(
            f"Mirror crashed: {type(error).__name__}: {error!s}",
            context="function: mirror",
            exception=error,
        )
    session.write_summary()
    if verbose:
        session.console.print_exception()
    raise typer.Exit(ExitCode.GENERAL_ERROR) from error
