# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""Execution pipeline and failure handling for the ``reset`` command.

Validates the parsed command line, deletes the organization's repositories via
:class:`~source_clone.reset_manager.ResetManager` and reports the outcome.
"""

from __future__ import annotations

import asyncio
import traceback
from dataclasses import dataclass
from typing import TYPE_CHECKING, NoReturn

import typer

from source_clone.cli_app import format_version_string
from source_clone.error_codes import ExitCode
from source_clone.file_logging import (
    cli_args_to_dict,
    file_logging_active,
    get_default_log_path,
    init_logging,
)
from source_clone.reset_manager import ResetManager

if TYPE_CHECKING:
    from pathlib import Path

    from rich.console import Console

    from source_clone.cli_session import CliSession
    from source_clone.reset_models import ResetResult


@dataclass(frozen=True)
class ResetRequest:
    """Command line arguments supplied to the ``reset`` command."""

    org: str
    path: Path
    compare: bool
    github_token: str | None
    no_confirm: bool
    include_automation_prs: bool
    verbose: bool
    quiet: bool


def run_reset(request: ResetRequest, session: CliSession) -> NoReturn:
    """Run the reset pipeline for an already parsed command line."""
    console = session.console
    github_token = _validate_request(request, console)

    # Show banner
    if not request.quiet:
        console.print(format_version_string(command="reset"))
        console.print()

    _start_logging(request, session)

    if request.verbose and session.log_file_path:
        console.print(f"📝 Logging to: [cyan]{session.log_file_path}[/cyan]")

    manager = ResetManager(
        org=request.org,
        github_token=github_token,
        local_path=request.path,
        console=console,
        include_automation_prs=request.include_automation_prs,
    )

    has_permissions = asyncio.run(manager.check_token_permissions())
    if not has_permissions:
        console.print(
            "[red]❌ Insufficient permissions. "
            "Ensure your GitHub token has 'delete_repo' scope.[/red]"
        )
        raise typer.Exit(ExitCode.CONFIGURATION_ERROR)

    result = asyncio.run(
        manager.execute_reset(
            compare=request.compare,
            no_confirm=request.no_confirm,
        )
    )

    _report_result(request, session, result)


def _validate_request(request: ResetRequest, console: Console) -> str:
    """Reject unusable option combinations before contacting GitHub.

    Returns:
        The validated GitHub token, narrowed to ``str``.
    """
    if request.verbose and request.quiet:
        console.print("[red]Error:[/red] --verbose and --quiet cannot be used together")
        raise typer.Exit(ExitCode.CONFIGURATION_ERROR)

    if not request.github_token:
        console.print(
            "[red]❌ GitHub token required. "
            "Set GITHUB_TOKEN environment variable or use --github-token[/red]"
        )
        raise typer.Exit(ExitCode.CONFIGURATION_ERROR)

    return request.github_token


def _start_logging(request: ResetRequest, session: CliSession) -> None:
    """Initialize unified logging (file + console), consistent with
    clone/refresh/mirror subcommands.
    """
    log_file_path = get_default_log_path(f"reset-{request.org}", request.path)
    file_logger, error_collector = init_logging(
        log_file=log_file_path,
        disable_file=False,
        log_level="DEBUG",
        console_level="DEBUG" if request.verbose else "WARNING",
        quiet=request.quiet,
        verbose=request.verbose,
        cli_args=cli_args_to_dict(
            org=request.org,
            path=str(request.path),
            compare=request.compare,
            no_confirm=request.no_confirm,
            include_automation_prs=request.include_automation_prs,
            verbose=request.verbose,
            quiet=request.quiet,
        ),
        command="reset",
    )
    session.log_file_path = log_file_path if file_logging_active() else None
    session.file_logger = file_logger
    session.error_collector = error_collector


def _report_result(
    request: ResetRequest, session: CliSession, result: ResetResult
) -> NoReturn:
    """Display final summary."""
    console = session.console
    file_logger = session.file_logger
    if result.deleted_repos > 0:
        if file_logger:
            file_logger.debug(
                "Reset complete: %d/%d repositories deleted",
                result.deleted_repos,
                result.total_repos,
            )
        if not request.quiet:
            console.print(
                f"\n🎉 Reset complete: {result.deleted_repos}/{result.total_repos} "
                "repositories deleted"
            )

            if result.failed_deletions:
                console.print(f"⚠️  {len(result.failed_deletions)} deletions failed")

            if result.had_unsynchronized and request.compare:
                console.print(
                    f"⚠️  Note: {len(result.unsynchronized_repos)} repositories "
                    "had local/remote differences"
                )

        session.write_summary()
        raise typer.Exit(0)
    else:
        if file_logger:
            file_logger.debug("Reset: no repositories deleted")
        if not request.quiet:
            if result.total_repos == 0:
                console.print(
                    "\n✅ Organization is already empty — no repositories to delete"
                )
            else:
                console.print("\n❌ No repositories were deleted")
        session.write_summary()
        raise typer.Exit(0)


def handle_command_error(
    session: CliSession,
    error: Exception,
    *,
    label: str,
    log_format: str,
    exit_code: ExitCode,
) -> NoReturn:
    """Report a known failure mode and exit with its status code."""
    session.console.print(f"[red]❌ {label}:[/red] {error}")
    if session.file_logger:
        session.file_logger.error(log_format, str(error))
    session.write_summary()
    raise typer.Exit(exit_code) from error


def handle_interrupt(session: CliSession) -> NoReturn:
    """Report a user interrupt and exit."""
    session.console.print("\n❌ Reset cancelled by user")
    if session.file_logger:
        session.file_logger.warning("Reset cancelled by user (KeyboardInterrupt)")
    session.write_summary()
    raise typer.Exit(1) from None


def handle_crash(session: CliSession, error: Exception, *, verbose: bool) -> NoReturn:
    """Report an unexpected failure and exit."""
    session.console.print(f"\n[red]Error:[/red] {error}")
    if session.file_logger:
        session.file_logger.critical("Reset crashed: %s", str(error), exc_info=True)
    if session.error_collector:
        session.error_collector.add_critical_error(
            f"Reset crashed: {type(error).__name__}: {error!s}",
            context="function: reset",
            exception=error,
        )
    session.write_summary()
    if verbose:
        session.console.print(traceback.format_exc())
    raise typer.Exit(ExitCode.GENERAL_ERROR) from error
