# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""Execution pipeline for the ``refresh`` command.

Validates the parsed command line, refreshes the repositories found beneath
the output path, applies any requested content filters and reports results.
"""

from __future__ import annotations

import contextlib
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, NoReturn

import typer

from gerrit_clone.cli_app import format_version_string
from gerrit_clone.cli_refresh_report import (
    show_refresh_results,
    write_refresh_manifest,
)
from gerrit_clone.content_intent import IntentError
from gerrit_clone.content_intent_resolve import resolve_filters
from gerrit_clone.content_spec import ContentFilterSpec, manifest_entry
from gerrit_clone.content_stage import filter_repository
from gerrit_clone.error_codes import ExitCode
from gerrit_clone.file_logging import (
    cli_args_to_dict,
    get_default_log_path,
    init_logging,
)
from gerrit_clone.models import normalize_project_list
from gerrit_clone.refresh_manager import refresh_repositories
from gerrit_clone.worktree_exclude import (
    hide_from_checkout,
    is_checkout_top,
    literal_pattern,
    tracked_names,
)

if TYPE_CHECKING:
    from pathlib import Path

    from rich.console import Console

    from gerrit_clone.models import RefreshBatchResult


#: Default manifest name, before its timestamp and ``.json`` suffix.
MANIFEST_PREFIX = "refresh-manifest-"


@dataclass(frozen=True)
class RefreshRequest:
    """Command line arguments supplied to the ``refresh`` command."""

    output_path: Path
    include_projects: list[str] | None
    exclude_projects: list[str] | None
    threads: int | None
    fetch_only: bool
    prune: bool
    timeout: int
    skip_conflicts: bool
    auto_stash: bool
    strategy: str
    filter_gerrit_only: bool
    exit_on_error: bool
    dry_run: bool
    force: bool
    force_hard: bool
    recursive: bool
    verbose: bool
    quiet: bool
    manifest_filename: str | None
    remove_files: str | None
    git_filter: str | None
    redact_secrets: bool


def prepare_refresh(request: RefreshRequest, console: Console) -> None:
    """Validate the request and set up logging before any repository work."""
    _validate_request(request, console)

    # Display version
    if not request.quiet:
        console.print(format_version_string("refresh"))
        console.print()

    _hide_run_files(request, console)
    _start_logging(request, console)

    # Display configuration summary
    if not request.quiet:
        _show_configuration(request, console)


def run_refresh(request: RefreshRequest, console: Console) -> None:
    """Refresh the repositories found beneath the requested output path."""
    spec = _resolve_filters(request, console)
    result = refresh_repositories(
        base_path=request.output_path,
        config=None,
        timeout=request.timeout,
        fetch_only=request.fetch_only,
        prune=request.prune,
        skip_conflicts=request.skip_conflicts,
        auto_stash=request.auto_stash,
        strategy=request.strategy,
        filter_gerrit_only=request.filter_gerrit_only,
        threads=request.threads,
        exit_on_error=request.exit_on_error,
        dry_run=request.dry_run,
        force=request.force,
        force_hard=request.force_hard,
        recursive=request.recursive,
        include_projects=request.include_projects if request.include_projects else None,
        exclude_projects=request.exclude_projects if request.exclude_projects else None,
        # Judged by what a real run with these options would do, a dry
        # run included: it predicts that run, filters and all.
        content_filters=spec,
    )

    _apply_content_filters(request, console, result, spec)

    # Display results
    show_refresh_results(console, result, request.dry_run)

    _write_manifest(request, console, result, spec)
    _exit_with_result_status(request, console, result)


def _validate_request(request: RefreshRequest, console: Console) -> None:
    """Reject mutually exclusive or unsupported option combinations."""
    if request.verbose and request.quiet:
        console.print("[red]Error:[/red] --verbose and --quiet cannot be used together")
        raise typer.Exit(ExitCode.CONFIGURATION_ERROR)

    if request.strategy not in ("merge", "rebase"):
        console.print(
            f"[red]❌ Invalid pull strategy: {request.strategy}. Must be 'merge' or 'rebase'.[/red]"
        )
        raise typer.Exit(ExitCode.VALIDATION_ERROR.value)


def _hide_run_files(request: RefreshRequest, console: Console) -> None:
    """Keep the log and manifest out of a checkout at the output path.

    Written there, they are untracked changes in it, and refresh skips a
    working copy with uncommitted changes (#310).  A tracked file with
    one of their names cannot be hidden, and writing it would overwrite
    committed content, so the run stops before writing anything; so it
    does for a symbolic link there, which could aim the write at one.
    """
    root = request.output_path
    names = [get_default_log_path("refresh", root).name]
    if request.manifest_filename:
        # One written outside the checkout cannot dirty it.  Its own name
        # is kept, not resolved: a link there is refused below.
        manifest = root / request.manifest_filename
        with contextlib.suppress(ValueError):
            names.append(
                (manifest.parent.resolve() / manifest.name)
                .relative_to(root.resolve())
                .as_posix()
            )
    try:
        checkout = is_checkout_top(root)
        clashes = tracked_names(root, names) if checkout else []
    except OSError as exc:
        _stop(console, f"{exc}; refresh stopped before writing into it")
    if not checkout:
        return
    linked = [name for name in names if (root / name).is_symlink()]
    if linked:
        _stop(
            console,
            f"{root} is a checkout where {', '.join(linked)} is a symbolic "
            f"link, which refresh would write its log or manifest through. "
            f"Remove it, or pass another --manifest-filename",
        )
    if clashes:
        _stop(
            console,
            f"{root} is a checkout that tracks {', '.join(clashes)}, which "
            f"refresh would overwrite with its own log or manifest. Point "
            f"--output-path at its parent directory, or pass another "
            f"--manifest-filename",
        )
    patterns = [literal_pattern(name) for name in names]
    if None in patterns:
        _stop(
            console,
            "A --manifest-filename with a line break cannot be hidden from "
            "the checkout's git status, and refresh would then skip the "
            "checkout every time; choose another name",
        )
    if not request.manifest_filename:
        patterns.append(f"/{MANIFEST_PREFIX}*.json")
    try:
        hide_from_checkout(root, [pattern for pattern in patterns if pattern])
    except OSError as exc:
        _stop(console, f"{exc}; refresh would then skip the checkout")


def _stop(console: Console, message: str) -> NoReturn:
    """Stop the run with a configuration error, before it writes anything."""
    console.print(f"[red]❌ {message}[/red]")
    raise typer.Exit(ExitCode.CONFIGURATION_ERROR.value)


def _start_logging(request: RefreshRequest, console: Console) -> None:
    """Initialise file and console logging for the refresh run."""
    cli_args = cli_args_to_dict(**vars(request))

    log_file_path = get_default_log_path("refresh", request.output_path)

    init_logging(
        log_file=log_file_path,
        disable_file=False,
        log_level="DEBUG",
        console_level="DEBUG" if request.verbose else "WARNING",
        quiet=request.quiet,
        verbose=request.verbose,
        cli_args=cli_args,
        host=None,
        command="refresh",
    )

    if log_file_path and request.verbose:
        console.print(f"📝 Logging to: [cyan]{log_file_path}[/cyan]")
        console.print()


def _show_configuration(request: RefreshRequest, console: Console) -> None:
    """Print the effective refresh configuration."""
    console.print("[bold blue]Refresh Configuration[/bold blue]")
    console.print(f"Base Path: [cyan]{request.output_path}[/cyan]")
    console.print(f"Threads: [cyan]{request.threads or 'auto-detect'}[/cyan]")
    console.print(
        f"Mode: [cyan]{'Fetch Only' if request.fetch_only else f'Pull ({request.strategy})'}[/cyan]"
    )
    console.print(f"Prune: [cyan]{request.prune}[/cyan]")
    console.print(f"Timeout: [cyan]{request.timeout}s[/cyan]")
    console.print(f"Skip Conflicts: [cyan]{request.skip_conflicts}[/cyan]")
    console.print(f"Auto Stash: [cyan]{request.auto_stash}[/cyan]")
    console.print(
        f"Filter: [cyan]{'Gerrit only' if request.filter_gerrit_only else 'All repos'}[/cyan]"
    )
    inc_display = (
        normalize_project_list(list(request.include_projects))
        if request.include_projects
        else []
    )
    exc_display = (
        normalize_project_list(list(request.exclude_projects))
        if request.exclude_projects
        else []
    )
    console.print(
        f"Include Filter: [cyan]{', '.join(inc_display) if inc_display else '—'}[/cyan]"
    )
    console.print(
        f"Exclude Filter: [cyan]{', '.join(exc_display) if exc_display else '—'}[/cyan]"
    )
    console.print(f"Dry Run: [cyan]{request.dry_run}[/cyan]")
    # A dry run disables all modifications (see RefreshManager), so report
    # the *effective* force settings rather than the user-supplied flags to
    # avoid implying a destructive run when nothing will be changed.
    effective_force = (request.force or request.force_hard) and not request.dry_run
    effective_force_hard = request.force_hard and not request.dry_run
    console.print(f"Force: [cyan]{effective_force}[/cyan]")
    console.print(f"Force Hard: [cyan]{effective_force_hard}[/cyan]")
    console.print(f"Recursive: [cyan]{request.recursive}[/cyan]")
    console.print()


def _resolve_filters(
    request: RefreshRequest, console: Console
) -> ContentFilterSpec | None:
    """The run's filters: its options and the tree's recorded intent.

    A dry run records nothing, and neither does a run whose output path
    does not exist -- it has no tree to record for.
    """
    try:
        return resolve_filters(
            request.output_path,
            ContentFilterSpec.from_options(
                request.remove_files,
                request.git_filter,
                request.redact_secrets,
                request.output_path,
            ),
            persist=not request.dry_run and request.output_path.is_dir(),
            command="refresh",
        )
    except IntentError as exc:
        console.print(f"[red]❌ {exc}[/red]")
        raise typer.Exit(ExitCode.CONFIGURATION_ERROR.value) from exc


def _apply_content_filters(
    request: RefreshRequest,
    console: Console,
    result: RefreshBatchResult,
    spec: ContentFilterSpec | None,
) -> None:
    """Filter what refreshed cleanly, as each project's filters decide."""
    if spec is None:
        return
    if request.dry_run:
        # A dry run changes nothing, and filtering rewrites history.
        if not request.quiet:
            console.print("[cyan]Dry run: content filters not applied[/cyan]")
        return

    if not request.quiet:
        console.print("[cyan]🔧 Applying content filters...[/cyan]")
    filter_success = filter_fail = 0
    for rr in result.results:
        # Only what refreshed cleanly (SUCCESS / UP_TO_DATE): every other
        # status leaves the repository in a state where rewriting its
        # history or removing files would be unsafe or raise.
        if not rr.success:
            continue
        if rr.content_filtered:
            # Re-filtered as part of its staged refresh already.
            filter_success += 1
            continue
        reason = filter_repository(
            spec, rr.path, spec.project_name(rr.path), request.timeout
        )
        if reason is None:
            filter_success += 1
            continue
        filter_fail += 1
        if not request.quiet:
            console.print(
                f"[yellow]⚠️  Filter failed for {rr.project_name}: {reason}[/yellow]"
            )
    if not request.quiet:
        console.print(
            f"[cyan]Content filtering: {filter_success} succeeded, {filter_fail} failed[/cyan]"
        )
    if filter_fail > 0:
        raise typer.Exit(ExitCode.GENERAL_ERROR)


def _write_manifest(
    request: RefreshRequest,
    console: Console,
    result: RefreshBatchResult,
    spec: ContentFilterSpec | None,
) -> None:
    """Write the refresh manifest and report its location."""
    # Write manifest with timestamp by default, or use specified filename
    manifest_file: Path
    if request.manifest_filename:
        manifest_file = request.output_path / request.manifest_filename
    else:
        timestamp = datetime.now(UTC).strftime("%Y-%m-%dT%H-%M-%SZ")
        manifest_file = request.output_path / f"{MANIFEST_PREFIX}{timestamp}.json"
    write_refresh_manifest(manifest_file, result, manifest_entry(spec))
    if not request.quiet:
        console.print(f"📄 Manifest: [cyan]{manifest_file}[/cyan]")
        console.print()


def _exit_with_result_status(
    request: RefreshRequest, console: Console, result: RefreshBatchResult
) -> None:
    """Determine exit code."""
    if result.total_count == 0:
        # Nothing was refreshed, so nothing can have succeeded: an empty
        # or mistyped output path, or filters that match nothing, would
        # otherwise pass as a clean run.
        if not request.quiet:
            filtered = request.include_projects or request.exclude_projects
            console.print(
                f"[yellow]⚠️  No repositories found to refresh under "
                f"{request.output_path}"
                f"{' matching the project filters' if filtered else ''}; "
                f"nothing was refreshed[/yellow]"
            )
        raise typer.Exit(ExitCode.GENERAL_ERROR.value)
    if result.failed_count > 0:
        if not request.quiet:
            console.print(
                f"[yellow]⚠️  {result.failed_count} repositories failed to refresh[/yellow]"
            )
        raise typer.Exit(ExitCode.GENERAL_ERROR.value)
    elif result.conflicts_count > 0:
        if not request.quiet:
            console.print(
                f"[yellow]⚠️  {result.conflicts_count} repositories have conflicts[/yellow]"
            )
        raise typer.Exit(ExitCode.GENERAL_ERROR.value)
    else:
        if not request.quiet:
            not_refreshed = result.total_count - result.success_count
            if not_refreshed:
                console.print(
                    f"[yellow]⚠️  {not_refreshed} of {result.total_count} "
                    f"repositories were not refreshed; see the summary "
                    f"above[/yellow]"
                )
            else:
                console.print(
                    "[green]✅ All repositories refreshed successfully![/green]"
                )
        raise typer.Exit(ExitCode.SUCCESS.value)
