# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""Definition of the ``refresh`` command.

Declares the command line surface and hands the parsed arguments to
:mod:`gerrit_clone.cli_refresh_run`, which performs the work.

Unlike the rest of the package this module deliberately omits
``from __future__ import annotations``: Typer resolves the command
signature's annotations at runtime to derive each parameter type.
"""

import traceback
from pathlib import Path

import typer
from rich.console import Console

from gerrit_clone import cli_options_refresh as opts
from gerrit_clone.cli_refresh_run import RefreshRequest, prepare_refresh, run_refresh
from gerrit_clone.concurrent_utils import handle_sigint_gracefully
from gerrit_clone.error_codes import ExitCode


def refresh(
    output_path: Path = opts.OUTPUT_PATH,
    include_projects: list[str] | None = opts.INCLUDE_PROJECTS,
    exclude_projects: list[str] | None = opts.EXCLUDE_PROJECTS,
    threads: int | None = opts.THREADS,
    fetch_only: bool = opts.FETCH_ONLY,
    prune: bool = opts.PRUNE,
    timeout: int = opts.TIMEOUT,
    skip_conflicts: bool = opts.SKIP_CONFLICTS,
    auto_stash: bool = opts.AUTO_STASH,
    strategy: str = opts.STRATEGY,
    filter_gerrit_only: bool = opts.FILTER_GERRIT_ONLY,
    exit_on_error: bool = opts.EXIT_ON_ERROR,
    dry_run: bool = opts.DRY_RUN,
    force: bool = opts.FORCE,
    force_hard: bool = opts.FORCE_HARD,
    recursive: bool = opts.RECURSIVE,
    verbose: bool = opts.VERBOSE,
    quiet: bool = opts.QUIET,
    manifest_filename: str | None = opts.MANIFEST_FILENAME,
    remove_files: str | None = opts.REMOVE_FILES,
    git_filter: str | None = opts.GIT_FILTER,
    redact_secrets: bool = opts.REDACT_SECRETS,
) -> None:
    """Refresh the repositories under a path.

    Scans the specified directory for Git repositories and updates each one from
    its remotes, wherever it was cloned from. Supports parallel updates, automatic
    stash handling, and various safety features.

    Examples:

        # Refresh all repos in current directory
        source-clone refresh

        # Refresh ONAP repositories
        source-clone refresh --output-path ~/repos/onap

        # Refresh only specific projects (with wildcard)
        source-clone refresh --output-path ~/repos --include-projects "ccsdk*"

        # Refresh all except a problematic repo
        source-clone refresh --exclude-projects "testsuite/pythonsdk-tests"

        # Fetch only (don't merge)
        source-clone refresh --output-path ~/repos --fetch-only

        # Use 16 threads for faster refresh
        source-clone refresh --output-path ~/repos --threads 16

        # Auto-stash uncommitted changes
        source-clone refresh --output-path ~/repos --auto-stash

        # Hard-reset local content to exactly match the remote
        # (discards local-only commits and divergence)
        source-clone refresh --output-path ~/repos --force-hard

        # Dry run (show what would be updated)
        source-clone refresh --output-path ~/repos --dry-run

        # Refresh only repositories cloned from Gerrit
        source-clone refresh --output-path ~/repos --gerrit-only
    """
    # Configure graceful interrupt handling for multi-threaded operations
    handle_sigint_gracefully()

    console = Console(stderr=True)

    request = RefreshRequest(
        output_path=output_path,
        include_projects=include_projects,
        exclude_projects=exclude_projects,
        threads=threads,
        fetch_only=fetch_only,
        prune=prune,
        timeout=timeout,
        skip_conflicts=skip_conflicts,
        auto_stash=auto_stash,
        strategy=strategy,
        filter_gerrit_only=filter_gerrit_only,
        exit_on_error=exit_on_error,
        dry_run=dry_run,
        force=force,
        force_hard=force_hard,
        recursive=recursive,
        verbose=verbose,
        quiet=quiet,
        manifest_filename=manifest_filename,
        remove_files=remove_files,
        git_filter=git_filter,
        redact_secrets=redact_secrets,
    )

    prepare_refresh(request, console)

    try:
        run_refresh(request, console)
    except KeyboardInterrupt:
        console.print("\n[yellow]⚠️  Refresh cancelled by user[/yellow]")
        # Flush console to ensure message is displayed before exit
        if hasattr(console.file, "flush"):
            console.file.flush()
        raise typer.Exit(ExitCode.INTERRUPT.value) from None
    except typer.Exit:
        # Re-raise typer.Exit without catching it
        raise
    except Exception as e:
        console.print(f"[red]❌ Refresh failed: {e}[/red]")
        if verbose:
            console.print(traceback.format_exc())
        raise typer.Exit(ExitCode.GENERAL_ERROR.value) from e
