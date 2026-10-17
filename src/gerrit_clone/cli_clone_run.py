# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""Execution pipeline and failure handling for the ``clone`` command.

Runs the prepared request (cloning, content filtering, result reporting and
optional cleanup) and owns the interrupt/crash handlers used by the command.
"""

from __future__ import annotations

import traceback
from contextlib import suppress
from datetime import UTC, datetime
from pathlib import Path
from shutil import rmtree
from typing import TYPE_CHECKING, NoReturn

import typer
from rich.console import Console
from rich.markup import escape
from rich.panel import Panel
from rich.text import Text

from gerrit_clone import __version__, cli_hooks
from gerrit_clone import cli_clone_setup as setup
from gerrit_clone.cli_app import is_github_actions_context
from gerrit_clone.cli_clone_banner import show_startup_banner
from gerrit_clone.content_stage import filter_repository
from gerrit_clone.error_codes import DiscoveryError, ExitCode
from gerrit_clone.logging import get_logger
from gerrit_clone.models import CloneStatus, RefreshResult, RefreshStatus
from gerrit_clone.refresh_worker import RefreshWorker
from gerrit_clone.rich_status import (
    handle_crash_display,
    show_error_summary,
    show_final_results,
)

if TYPE_CHECKING:
    from gerrit_clone.cli_clone_models import CloneRequest
    from gerrit_clone.cli_session import CliSession
    from gerrit_clone.content_spec import ContentFilterSpec
    from gerrit_clone.models import BatchResult, CloneResult, Config


logger = get_logger(__name__)


def run_clone(request: CloneRequest, session: CliSession) -> None:
    """Run the clone pipeline for an already parsed command line."""
    console = session.console
    source_type, github_org = setup.resolve_source(request, console)
    cli_args = setup.build_cli_args(request, source_type, github_org)
    file_logger, error_collector, log_file_path = setup.start_logging(request, cli_args)
    session.file_logger = file_logger
    session.error_collector = error_collector
    session.log_file_path = log_file_path

    if request.use_https:
        setup.apply_http_credentials(request, console, file_logger)

    # Log version to file in GitHub Actions environment (file only, no console)
    if is_github_actions_context():
        try:
            file_logger.debug("gerrit-clone version %s", __version__)
        except Exception:
            file_logger.warning("Version information not available")

    discovery_method = setup.resolve_discovery_method(request, console, source_type)
    config = setup.build_config(
        request, session, source_type, github_org, discovery_method
    )

    # Show startup banner if not quiet
    if not request.quiet:
        show_startup_banner(console, config)

    batch_result = _clone_repositories(config)
    _apply_content_filters(request, console, batch_result, config.content_filters)
    _report_results(request, session, batch_result)
    exit_code = _determine_exit_code(session, batch_result)

    # Optional cleanup
    if request.cleanup:
        _cleanup_clone_directory(session, config)

    # Close file logging and write summary
    session.write_summary()

    if exit_code != 0:
        raise typer.Exit(exit_code)


def _clone_repositories(config: Config) -> BatchResult:
    """Clone every discovered repository, reporting discovery failures."""
    try:
        return cli_hooks.clone_repositories(config)
    except DiscoveryError as e:
        console = Console()
        console.print(
            Panel(
                Text(
                    f"{e.message}\n{e.details}" if e.details else str(e.message),
                    style="bold red",
                ),
                title="Discovery Error",
                border_style="red",
            )
        )
        raise typer.Exit(ExitCode.DISCOVERY_ERROR) from e


def _apply_content_filters(
    request: CloneRequest,
    console: Console,
    batch_result: BatchResult,
    spec: ContentFilterSpec | None,
) -> None:
    """Filter every successful clone, as each project's filters decide."""
    if spec is None:
        return

    if not request.quiet:
        console.print("[cyan]🔧 Applying content filters...[/cyan]")
    filter_success = filter_fail = 0
    for cr in batch_result.results:
        if cr.skipped and cr.path and spec.missing_tokens(cr.path):
            # The refresh pass refused it for the tokens, before any
            # fetch.  Clone reports that as the filtering failure it is,
            # whether or not upstream had moved on.
            filter_fail += 1
            if not request.quiet:
                console.print(
                    f"[yellow]⚠️  Filter failed for {cr.project.name}: "
                    f"{cr.error_message}[/yellow]"
                )
            continue
        if not cr.success or not cr.path:
            continue
        if cr.content_filtered:
            # Re-filtered as part of its staged refresh already.
            filter_success += 1
            continue
        if cr.status is CloneStatus.SUCCESS and (cr.path / ".git").exists():
            reason = _filter_new_working_copy(batch_result.config, spec, cr)
        else:
            reason = filter_repository(
                spec, cr.path, spec.project_name(cr.path), request.clone_timeout
            )
        if reason is None:
            filter_success += 1
            continue
        filter_fail += 1
        if not request.quiet:
            console.print(
                f"[yellow]⚠️  Filter failed for {cr.project.name}: {reason}[/yellow]"
            )
    if not request.quiet:
        console.print(
            f"[cyan]Content filtering: {filter_success} succeeded, {filter_fail} failed[/cyan]"
        )
    if filter_fail > 0:
        raise typer.Exit(ExitCode.GENERAL_ERROR)


def _filter_new_working_copy(
    config: Config, spec: ContentFilterSpec, cr: CloneResult
) -> str | None:
    """Filter a working copy this run cloned, through a staged copy.

    Filtered where it stands, it would lose the remote-tracking refs a
    later refresh follows (see
    :meth:`~gerrit_clone.refresh_checkout_fresh.FreshCheckoutMixin._filter_fresh_checkout`).
    One whose filtering fails, or is refused, may hold unfiltered
    content, so it is deleted.

    Returns:
        ``None`` if it was filtered, or needed no filtering; else why not.
    """
    filters = spec.filters_for(spec.project_name(cr.path))
    if filters.empty and not filters.missing_tokens:
        return None
    worker = RefreshWorker(
        config=config,
        timeout=config.clone_timeout,
        filter_gerrit_only=False,
        content_filters=spec,
    )
    result = RefreshResult(
        path=cr.path,
        project_name=cr.project.name,
        status=RefreshStatus.REFRESHING,
        started_at=datetime.now(UTC),
    )
    try:
        filtered = worker._filter_fresh_checkout(cr.path, result)
    except Exception as exc:
        filtered = False
        result.error_message = "; ".join(
            part for part in (result.error_message, str(exc) or repr(exc)) if part
        )
    except BaseException:
        # Interrupted: delete it, and say so if that fails, then go on.
        failure = _delete_working_copy(cr.path)
        if failure is not None:
            logger.error(
                "Interrupted while filtering %s; the working copy, which may "
                "hold unfiltered content, could not be deleted; delete %s by "
                "hand: %s",
                cr.project.name,
                cr.path,
                failure,
            )
        raise
    if filtered:
        cr.content_filtered = True
        return None
    reason = result.error_message or "content filtering failed"
    failure = _delete_working_copy(cr.path)
    if failure is None:
        return f"{reason}; the working copy was deleted, so none is left unfiltered"
    return (
        f"{reason}; the working copy, which may hold unfiltered content, "
        f"could not be deleted; delete {cr.path} by hand: {failure}"
    )


def _delete_working_copy(path: Path) -> str | None:
    """Delete working copy *path*, but no repository nested inside it.

    Returns:
        Why some of it could not be deleted, or ``None``.
    """
    failures: list[str] = []
    _delete_sparing_nested(path, failures)
    with suppress(OSError):
        path.rmdir()  # Only once nothing nested in it is left.
    return "; ".join(failures) or None


def _delete_sparing_nested(directory: Path, failures: list[str]) -> None:
    """Delete everything in *directory* but other repositories' checkouts."""
    try:
        entries = list(directory.iterdir())
    except OSError as exc:
        failures.append(str(exc))
        return
    for entry in entries:
        try:
            if entry.is_symlink() or not entry.is_dir():
                entry.unlink()
            elif entry.name == ".git":
                rmtree(entry)
            elif not (entry / ".git").exists():  # Not a nested repository.
                _delete_sparing_nested(entry, failures)
                if not any(entry.iterdir()):
                    entry.rmdir()
        except OSError as exc:
            failures.append(f"{entry}: {exc}")


def _report_results(
    request: CloneRequest, session: CliSession, batch_result: BatchResult
) -> None:
    """Show the final results summary and any collected errors."""
    console = session.console
    log_file_path = session.log_file_path

    # Show final results summary using Rich
    if not request.quiet:
        show_final_results(
            console, batch_result, str(log_file_path) if log_file_path else None
        )

    # Show error summary if there were issues
    error_collector = session.error_collector
    if error_collector and not request.quiet:
        errors = [
            record.message
            for record in error_collector.errors + error_collector.critical_errors
        ]
        warnings = [record.message for record in error_collector.warnings]
        if errors or warnings:
            show_error_summary(console, errors, warnings)


def _determine_exit_code(session: CliSession, batch_result: BatchResult) -> int:
    """Determine exit code based on results."""
    file_logger = session.file_logger
    if batch_result.failed_count > 0:
        if file_logger:
            file_logger.debug(
                "Clone completed with %d failures", batch_result.failed_count
            )
        return int(ExitCode.CLONE_ERROR)
    if file_logger:
        file_logger.debug("Clone completed successfully")
    return int(ExitCode.SUCCESS)


def _cleanup_clone_directory(session: CliSession, config: Config) -> None:
    """Remove the cloned directory once the run has finished.

    The run's log file is spared when it lies inside the directory, so
    the closing error summary can still be written to it.
    """
    file_logger = session.file_logger
    try:
        if file_logger:
            file_logger.debug(
                "Cleanup enabled - removing cloned directory: %s",
                config.path,
            )
        session.console.print(
            f"[yellow]🧹 Cleanup enabled - removing cloned directory: {config.path}[/yellow]"
        )
        kept = _log_file_within(session.log_file_path, Path(config.path))
        if kept is None:
            rmtree(config.path, ignore_errors=True)
        else:
            _remove_all_except(Path(config.path).resolve(), kept)
        if file_logger:
            file_logger.debug("Cleanup completed successfully")
        if kept is None:
            session.console.print("[green]Cleanup complete.[/green]")
        else:
            session.console.print(
                "[green]Cleanup complete (kept log file "
                f"{escape(str(session.log_file_path))}).[/green]"
            )
    except Exception as e:
        if file_logger:
            file_logger.debug("Cleanup failed: %s", str(e))
        session.console.print(f"[red]Cleanup failed:[/red] {e}")


def _log_file_within(log_file: Path | None, directory: Path) -> Path | None:
    """Return the log file's path relative to ``directory``, if inside it.

    Both paths are resolved first, so a relative or linked spelling of
    either is compared by the file it names.
    """
    if log_file is None:
        return None
    resolved_log = log_file.resolve()
    resolved_dir = directory.resolve()
    if resolved_log == resolved_dir or not resolved_log.is_relative_to(resolved_dir):
        return None
    return resolved_log.relative_to(resolved_dir)


def _remove_all_except(directory: Path, kept: Path) -> None:
    """Remove every entry under ``directory`` except the ``kept`` path.

    ``kept`` is relative to ``directory``; the directories leading down
    to it stay. Symbolic links are removed, never followed, and a failure
    to remove one entry does not stop the rest.
    """
    first, *rest = kept.parts
    try:
        entries = list(directory.iterdir())
    except OSError:
        return
    for entry in entries:
        if entry.name == first:
            if rest and entry.is_dir() and not entry.is_symlink():
                _remove_all_except(entry, Path(*rest))
            continue
        if entry.is_dir() and not entry.is_symlink():
            rmtree(entry, ignore_errors=True)
        else:
            with suppress(OSError):
                entry.unlink()


def handle_interrupt(session: CliSession) -> NoReturn:
    """Report a user interrupt and exit."""
    if session.file_logger:
        session.file_logger.warning("Operation cancelled by user (KeyboardInterrupt)")
    session.write_summary()
    session.console.print("\n[yellow]Operation cancelled by user[/yellow]")
    # Flush console to ensure message is displayed before exit
    if hasattr(session.console.file, "flush"):
        session.console.file.flush()
    raise typer.Exit(int(ExitCode.INTERRUPT)) from None


def handle_crash(session: CliSession, error: Exception, *, verbose: bool) -> NoReturn:
    """Record an unexpected failure, display it, and exit."""
    tb = traceback.extract_tb(error.__traceback__)
    crash_context = "unknown"
    crash_file = "unknown"
    crash_line = 0

    if tb:
        # Get the last frame (where the crash occurred)
        last_frame = tb[-1]
        crash_file = last_frame.filename
        crash_line = last_frame.lineno or 0
        crash_context = (
            f"{last_frame.name}() at {crash_file.split('/')[-1]}:{crash_line}"
        )

    if session.file_logger:
        session.file_logger.critical(
            "Tool crashed in %s: %s", crash_context, str(error), exc_info=True
        )
    if session.error_collector:
        session.error_collector.add_critical_error(
            f"Tool crashed: {type(error).__name__}: {error!s}",
            context=f"function: {crash_context}",
            exception=error,
        )
    session.write_summary()

    # Use Rich status system for crash display
    log_file_path = session.log_file_path
    handle_crash_display(
        session.console, error, str(log_file_path) if log_file_path else None
    )

    if verbose:
        session.console.print_exception()
    raise typer.Exit(ExitCode.GENERAL_ERROR) from None
