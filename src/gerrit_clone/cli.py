# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2025 Matthew Watkins <mwatkins@linuxfoundation.org>

"""Typer-based CLI for gerrit-clone tool."""

from __future__ import annotations

import asyncio
import json
import os
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import typer
from rich.console import Console
from rich.panel import Panel
from rich.text import Text

from gerrit_clone import __version__
from gerrit_clone.clone_manager import clone_repositories
from gerrit_clone.concurrent_utils import handle_sigint_gracefully
from gerrit_clone.config import ConfigurationError, load_config
from gerrit_clone.error_codes import (
    DiscoveryError,
    ExitCode,
)
from gerrit_clone.netrc import (
    NetrcParseError,
    resolve_gerrit_credentials,
)
from gerrit_clone.file_logging import cli_args_to_dict, init_logging
from gerrit_clone.github_api import (
    GitHubAPI,
    GitHubAPIError,
    GitHubAuthError,
    get_default_org_or_user,
)
from gerrit_clone.logging import get_logger
from gerrit_clone.mirror_manager import (
    MirrorBatchResult,
    MirrorManager,
    filter_projects_by_hierarchy,
)
from gerrit_clone.models import DiscoveryMethod, RefreshBatchResult, RetryPolicy, SourceType
from gerrit_clone.refresh_manager import refresh_repositories
from gerrit_clone.reset_manager import ResetManager
from gerrit_clone.rich_status import (
    handle_crash_display,
    show_error_summary,
    show_final_results,
)
from gerrit_clone.unified_discovery import discover_projects

logger = get_logger(__name__)


def _is_github_actions_context() -> bool:
    """Detect if running in GitHub Actions environment."""
    return (
        os.getenv("GITHUB_ACTIONS") == "true"
        or os.getenv("GITHUB_EVENT_NAME", "").strip() != ""
    )


def _format_version_string(command: str = "", styled: bool = True) -> str:
    """Format version string with consistent styling.

    Args:
        command: Optional command name to include (e.g., "mirror")
        styled: Whether to include Rich markup styling

    Returns:
        Formatted version string
    """
    if styled:
        if command:
            return f"🏷️  [bold]gerrit-clone {command}[/bold] version [cyan]{__version__}[/cyan]"
        return f"🏷️  gerrit-clone version [cyan]{__version__}[/cyan]"
    else:
        if command:
            return f"🏷️  gerrit-clone {command} version {__version__}"
        return f"🏷️  gerrit-clone version {__version__}"


# Show version information when --help is used
if "--help" in sys.argv:
    try:
        print(_format_version_string(styled=False))
    except Exception:
        print("⚠️ gerrit-clone version information not available")


def version_callback(value: bool) -> None:
    """Show version information."""
    if value:
        console = Console()
        console.print(_format_version_string())
        raise typer.Exit()


app = typer.Typer(
    name="gerrit-clone",
    help="A multi-threaded CLI tool for bulk cloning repositories from Gerrit servers.",
    no_args_is_help=True,
    rich_markup_mode="rich",
    add_completion=True,
)


@app.callback()
def main(
    version: bool | None = typer.Option(
        None,
        "--version",
        callback=version_callback,
        is_eager=True,
        help="Show version information",
    ),
) -> None:
    """Main CLI entry point with top-level options."""
    pass


@app.command()
def clone(
    host: str = typer.Option(
        ...,
        "--host",
        "-h",
        help="Source hostname (Gerrit server or GitHub URL like github.com/ORG)",
        envvar="GERRIT_HOST",
    ),
    source_type: str | None = typer.Option(
        None,
        "--source-type",
        help="Source type: gerrit or github (auto-detected from host if not specified)",
        envvar="SOURCE_TYPE",
    ),
    github_token: str | None = typer.Option(
        None,
        "--github-token",
        help="GitHub personal access token (or set GERRIT_CLONE_TOKEN/GITHUB_TOKEN env var)",
        envvar="GERRIT_CLONE_TOKEN",
    ),
    github_org: str | None = typer.Option(
        None,
        "--github-org",
        help="GitHub organization or user name (auto-detected from host if not specified)",
        envvar="GITHUB_ORG",
    ),
    use_gh_cli: bool = typer.Option(
        False,
        "--use-gh-cli",
        help="Use GitHub CLI (gh) for cloning instead of git (preserves upstream/origin)",
        envvar="USE_GH_CLI",
    ),
    port: int | None = typer.Option(
        None,
        "--port",
        "-p",
        help=(
            "Gerrit SSH/HTTP port (default: 29418 for SSH, 443 for HTTPS). "
            "Note: Only used for Gerrit sources; ignored for GitHub sources."
        ),
        envvar="GERRIT_PORT",
        min=1,
        max=65535,
    ),
    base_url: str | None = typer.Option(
        None,
        "--base-url",
        help="Base URL for Gerrit API (defaults to https://HOST)",
        envvar="GERRIT_BASE_URL",
    ),
    ssh_user: str | None = typer.Option(
        None,
        "--ssh-user",
        "-u",
        help="SSH username for clone operations",
        envvar="GERRIT_SSH_USER",
    ),
    ssh_identity_file: Path | None = typer.Option(
        None,
        "--ssh-private-key",
        "-i",
        help="SSH private key file for authentication",
        envvar="GERRIT_SSH_PRIVATE_KEY",
        exists=True,
        file_okay=True,
        dir_okay=False,
        readable=True,
        resolve_path=True,
    ),
    output_path: Path = typer.Option(
        Path(),
        "--output-path",
        help="Clone destination (default: ./{SERVER}/ or ./github.com/{ORG}/)",
        envvar="OUTPUT_PATH",
        file_okay=False,
        resolve_path=False,
    ),
    skip_archived: bool = typer.Option(
        True,
        "--skip-archived/--include-archived",
        help="Skip archived/read-only repositories",
        envvar="GERRIT_SKIP_ARCHIVED",
    ),
    include_project: list[str] = typer.Option(
        None,
        "--include-project",
        help="Restrict cloning to specific project(s). Repeat for multiple. Exact match required.",
        envvar=None,
    ),
    ssh_debug: bool = typer.Option(
        False,
        "--ssh-debug",
        help="Enable verbose SSH (-vvv) for troubleshooting authentication (single or few projects).",
        envvar="GERRIT_SSH_DEBUG",
    ),
    discovery_method: str = typer.Option(
        "ssh",
        "--discovery-method",
        help="Method for discovering projects: ssh (default for Gerrit), http (REST API), both (union of both), or github_api (for GitHub)",
        envvar="GERRIT_DISCOVERY_METHOD",
    ),
    allow_nested_git: bool = typer.Option(
        True,
        "--allow-nested-git/--no-allow-nested-git",
        help="Allow nested git working trees when cloning both parent and child repositories",
        envvar="GERRIT_ALLOW_NESTED_GIT",
    ),
    nested_protection: bool = typer.Option(
        True,
        "--nested-protection/--no-nested-protection",
        help="Auto-add nested child repo paths to parent .git/info/exclude",
        envvar="GERRIT_NESTED_PROTECTION",
    ),
    move_conflicting: bool = typer.Option(
        True,
        "--move-conflicting/--no-move-conflicting",
        help="Move conflicting files/directories in parent repos to [NAME].parent to allow nested cloning",
        envvar="GERRIT_MOVE_CONFLICTING",
    ),
    threads: int | None = typer.Option(
        None,
        "--threads",
        "-t",
        help="Number of concurrent clone threads (default: auto)",
        envvar="GERRIT_THREADS",
        min=1,
    ),
    depth: int | None = typer.Option(
        None,
        "--depth",
        "-d",
        help="Create shallow clone with given depth",
        envvar="GERRIT_CLONE_DEPTH",
        min=1,
    ),
    branch: str | None = typer.Option(
        None,
        "--branch",
        "-b",
        help="Clone specific branch instead of default",
        envvar="GERRIT_BRANCH",
    ),
    mirror: bool | None = typer.Option(
        None,
        "--mirror/--no-mirror",
        help="Use git clone --mirror for complete repository metadata (all refs, tags, branches). Creates bare repository. Incompatible with --depth and --branch.",
        envvar="GERRIT_MIRROR",
    ),
    use_https: bool = typer.Option(
        False,
        "--https/--ssh",
        help="Use HTTPS for cloning instead of SSH",
        envvar="GERRIT_USE_HTTPS",
    ),
    keep_remote_protocol: bool = typer.Option(
        False,
        "--keep-remote-protocol",
        help="Keep original clone protocol for remote (default: always set SSH)",
        envvar="GERRIT_KEEP_REMOTE_PROTOCOL",
    ),
    strict_host_checking: bool = typer.Option(
        True,
        "--strict-host/--accept-unknown-host",
        help="SSH strict host key checking",
        envvar="GERRIT_STRICT_HOST",
    ),
    clone_timeout: int = typer.Option(
        600,
        "--clone-timeout",
        help="Timeout per clone operation in seconds (min: 30, max: 1800)",
        envvar="GERRIT_CLONE_TIMEOUT",
        min=30,
        max=1800,
    ),
    retry_attempts: int = typer.Option(
        3,
        "--retry-attempts",
        help="Maximum retry attempts per repository",
        envvar="GERRIT_RETRY_ATTEMPTS",
        min=1,
        max=10,
    ),
    retry_base_delay: float = typer.Option(
        2.0,
        "--retry-base-delay",
        help="Base delay for retry backoff in seconds",
        envvar="GERRIT_RETRY_BASE_DELAY",
        min=0.1,
    ),
    retry_factor: float = typer.Option(
        2.0,
        "--retry-factor",
        help="Exponential backoff factor for retries",
        envvar="GERRIT_RETRY_FACTOR",
        min=1.0,
    ),
    retry_max_delay: float = typer.Option(
        30.0,
        "--retry-max-delay",
        help="Maximum retry delay in seconds",
        envvar="GERRIT_RETRY_MAX_DELAY",
        min=1.0,
    ),
    manifest_filename: str = typer.Option(
        "clone-manifest.json",
        "--manifest-filename",
        help="Output manifest filename",
        envvar="GERRIT_MANIFEST_FILENAME",
    ),
    config_file: Path | None = typer.Option(
        None,
        "--config-file",
        "-c",
        help="Configuration file path (YAML or JSON)",
        exists=True,
        file_okay=True,
        dir_okay=False,
        readable=True,
    ),
    verbose: bool = typer.Option(
        False,
        "--verbose",
        "-v",
        help="Enable verbose/debug output",
        envvar="GERRIT_VERBOSE",
    ),
    quiet: bool = typer.Option(
        False,
        "--quiet",
        "-q",
        help="Suppress all output except errors",
        envvar="GERRIT_QUIET",
    ),
    cleanup: bool = typer.Option(
        False,
        "--cleanup/--no-cleanup",
        help="Remove cloned repositories (output-path) after run completes (success or failure)",
        envvar="GERRIT_CLEANUP",
    ),
    no_refresh: bool = typer.Option(
        False,
        "--no-refresh",
        help="Skip refreshing existing repositories (default: auto-refresh existing repos)",
        envvar="NO_REFRESH",
    ),
    force: bool = typer.Option(
        False,
        "--force",
        "-f",
        help=(
            "Force refresh of all existing repositories. Automatically stashes any local "
            "uncommitted changes (without prompting), attempts to fix detached HEAD states, "
            "and then updates all repos. This can be disruptive to your working copies; "
            "use with care and recover changes from `git stash` if needed."
        ),
        envvar="FORCE_REFRESH",
    ),
    fetch_only: bool = typer.Option(
        False,
        "--fetch-only",
        help="Only fetch changes without merging (for existing repos)",
        envvar="FETCH_ONLY",
    ),
    skip_conflicts: bool = typer.Option(
        True,
        "--skip-conflicts/--no-skip-conflicts",
        help="Skip repositories with uncommitted changes during refresh",
        envvar="SKIP_CONFLICTS",
    ),
    exit_on_error: bool = typer.Option(
        False,
        "--exit-on-error",
        "--stop-on-first-error",  # Backward compatibility
        help="Exit cloning immediately when the first error occurs (for debugging)",
        envvar="GERRIT_EXIT_ON_ERROR",
    ),
    log_file: Path | None = typer.Option(
        None,
        "--log-file",
        help="Custom log file path (default: gerrit-clone.log in current directory)",
        envvar="GERRIT_LOG_FILE",
        file_okay=True,
        dir_okay=False,
        resolve_path=True,
    ),
    disable_log_file: bool = typer.Option(
        False,
        "--disable-log-file",
        help="Disable creation of log file",
        envvar="GERRIT_DISABLE_LOG_FILE",
    ),
    log_level: str = typer.Option(
        "DEBUG",
        "--log-level",
        help="File logging level (DEBUG, INFO, WARNING, ERROR, CRITICAL)",
        envvar="GERRIT_LOG_LEVEL",
    ),
    http_user: str | None = typer.Option(
        None,
        "--http-user",
        help="HTTP username for Gerrit authentication (highest priority)",
        envvar="GERRIT_HTTP_USER",
    ),
    http_password: str | None = typer.Option(
        None,
        "--http-password",
        help="HTTP password for Gerrit authentication (highest priority)",
        envvar="GERRIT_HTTP_PASSWORD",
    ),
    no_netrc: bool = typer.Option(
        False,
        "--no-netrc",
        help="Disable .netrc credential lookup for HTTP authentication",
        envvar="GERRIT_NO_NETRC",
    ),
    netrc_file: Path | None = typer.Option(
        None,
        "--netrc-file",
        help="Explicit path to .netrc file for HTTP credentials",
        envvar="GERRIT_NETRC_FILE",
        exists=True,
        file_okay=True,
        dir_okay=False,
        readable=True,
        resolve_path=True,
    ),
    netrc_optional: bool = typer.Option(
        True,
        "--netrc-optional/--netrc-required",
        help="Whether to fail if .netrc file is not found (default: optional)",
        envvar="GERRIT_NETRC_OPTIONAL",
    ),
) -> None:
    """Clone all repositories from a Gerrit server or GitHub organization.

    This command discovers all projects/repositories from the specified source and clones
    them in parallel while preserving the project hierarchy. For Gerrit, repositories are
    cloned over SSH by default. For GitHub, SSH is also default (HTTPS with --https).

    By default, existing repositories are refreshed (git pull) instead of skipped.
    Use --no-refresh to skip existing repositories without updating them.

    Examples:

        # Clone all active repositories from Gerrit server
        gerrit-clone clone --host gerrit.example.org

        # Clone all repositories from GitHub organization (auto-refresh existing)
        gerrit-clone clone --host github.com/lfreleng-actions

        # Clone without refreshing existing repos
        gerrit-clone clone --host github.com/myorg --no-refresh

        # Force refresh existing repos (stash local changes)
        gerrit-clone clone --host github.com/myorg --force

        # Clone GitHub org with gh CLI (preserves upstream/origin)
        gerrit-clone clone --host github.com/myorg --use-gh-cli

        # Clone to specific directory with custom threads
        gerrit-clone clone --host gerrit.example.org --output-path ./repos --threads 8

        # Clone with shallow depth and specific branch
        gerrit-clone clone --host gerrit.example.org --no-mirror --depth 10 --branch main

        # Clone with explicit HTTP credentials (highest priority)
        gerrit-clone clone --host gerrit.example.org --https --http-user myuser --http-password mypass

        # Clone with credentials from specific .netrc file
        gerrit-clone clone --host gerrit.example.org --netrc-file ~/.netrc.gerrit

        # Clone requiring .netrc credentials (fail if not found)
        gerrit-clone clone --host gerrit.example.org --netrc-required
    """
    # Configure graceful interrupt handling for multi-threaded operations
    handle_sigint_gracefully()

    # Set up console for error handling
    console = Console(stderr=True)

    # Initialize variables for exception handler scope
    file_logger = None
    error_collector = None
    log_file_path = None

    try:
        # Auto-detect source type if not specified
        from gerrit_clone.github_discovery import detect_github_source, parse_github_url

        detected_source_type = SourceType.GERRIT
        detected_github_org = github_org

        if source_type:
            # Use explicitly specified source type
            try:
                detected_source_type = SourceType(source_type.lower())
            except ValueError:
                console.print(
                    f"[red]Error:[/red] Invalid source type '{source_type}'. Must be 'gerrit' or 'github'"
                )
                raise typer.Exit(ExitCode.CONFIGURATION_ERROR)
        elif detect_github_source(host):
            # Auto-detect GitHub from host
            detected_source_type = SourceType.GITHUB
            # Extract org from URL if present
            _, org = parse_github_url(host)
            if org:
                detected_github_org = org
            console.print(
                f"[cyan]ℹ[/cyan] Auto-detected GitHub source from host: {host}"
            )

        # Validate GitHub-specific requirements
        if detected_source_type == SourceType.GITHUB:
            if not detected_github_org:
                console.print(
                    "[red]Error:[/red] GitHub organization/user not specified. "
                    "Use --github-org or include in --host (e.g., github.com/ORG)"
                )
                raise typer.Exit(ExitCode.CONFIGURATION_ERROR)

        # Validate mutually exclusive options
        if verbose and quiet:
            console.print(
                "[red]Error:[/red] --verbose and --quiet cannot be used together"
            )
            raise typer.Exit(ExitCode.CONFIGURATION_ERROR)

        # Prepare CLI arguments for logging
        cli_args = cli_args_to_dict(
        host=host,
        source_type=detected_source_type.value,
        github_token="<redacted>" if github_token else None,
        github_org=detected_github_org,
        use_gh_cli=use_gh_cli,
        no_refresh=no_refresh,
        force=force,
        fetch_only=fetch_only,
        skip_conflicts=skip_conflicts,
        port=port,
        base_url=base_url,
        ssh_user=ssh_user,
        ssh_identity_file=ssh_identity_file,
        path=output_path,
        skip_archived=skip_archived,
        include_project=include_project,
        ssh_debug=ssh_debug,
        allow_nested_git=allow_nested_git,
        nested_protection=nested_protection,
        move_conflicting=move_conflicting,
        threads=threads,
        depth=depth,
        branch=branch,
        mirror=mirror,
        use_https=use_https,
        keep_remote_protocol=keep_remote_protocol,
        strict_host_checking=strict_host_checking,
        clone_timeout=clone_timeout,
        retry_attempts=retry_attempts,
        retry_base_delay=retry_base_delay,
        retry_factor=retry_factor,
        retry_max_delay=retry_max_delay,
        manifest_filename=manifest_filename,
        config_file=config_file,
        verbose=verbose,
        quiet=quiet,
        cleanup=cleanup,
        exit_on_error=exit_on_error,
        log_file=log_file,
        disable_log_file=disable_log_file,
        log_level=log_level,
        no_netrc=no_netrc,
        netrc_file=str(netrc_file) if netrc_file else None,
        netrc_optional=netrc_optional,
    )

        # Set up unified logging system (file + console)
        file_logger, error_collector = init_logging(
            log_file=log_file,
            disable_file=disable_log_file,
            log_level=log_level,
            console_level="DEBUG" if verbose else "INFO",
            quiet=quiet,
            verbose=verbose,
            cli_args=cli_args,
            host=host,
            path=Path(output_path) if output_path else None,
        )

        # Set log_file_path for error handling compatibility
        from gerrit_clone.file_logging import get_default_log_path

        log_file_path = log_file if log_file else get_default_log_path(host, Path(output_path) if output_path else None)

        # Handle credential resolution for HTTP authentication
        # Priority: 1. CLI arguments (--http-user/--http-password)
        #           2. .netrc file
        #           3. Environment variables (GERRIT_HTTP_USER/GERRIT_HTTP_PASSWORD)
        #           4. Fallback environment variables (GERRIT_USERNAME/GERRIT_PASSWORD)
        if use_https:
            try:
                resolved_creds = resolve_gerrit_credentials(
                    host=host,
                    explicit_username=http_user,
                    explicit_password=http_password,
                    use_netrc=not no_netrc,
                    netrc_file=netrc_file,
                    env_username_var="GERRIT_HTTP_USER",
                    env_password_var="GERRIT_HTTP_PASSWORD",
                    fallback_env_username_var="GERRIT_USERNAME",
                    fallback_env_password_var="GERRIT_PASSWORD",
                )
                if resolved_creds:
                    # Set environment variables for downstream code to use
                    os.environ["GERRIT_HTTP_USER"] = resolved_creds.username
                    os.environ["GERRIT_HTTP_PASSWORD"] = resolved_creds.password
                    file_logger.debug(
                        "Loaded HTTP credentials for %s from %s",
                        host,
                        resolved_creds.source_detail,
                    )
                elif not no_netrc and not netrc_optional:
                    # No credentials found and netrc was required
                    console.print(
                        "[red]Error:[/red] No credentials found and --netrc-required set"
                    )
                    raise typer.Exit(ExitCode.CONFIGURATION_ERROR)
            except FileNotFoundError:
                if not netrc_optional:
                    console.print(
                        "[red]Error:[/red] No .netrc file found and --netrc-required set"
                    )
                    raise typer.Exit(ExitCode.CONFIGURATION_ERROR)
            except NetrcParseError as e:
                console.print(f"[red]Error:[/red] Failed to parse .netrc file: {e}")
                raise typer.Exit(ExitCode.CONFIGURATION_ERROR)

        # Log version to file in GitHub Actions environment (file only, no console)
        if _is_github_actions_context():
            try:
                file_logger.debug("gerrit-clone version %s", __version__)
            except Exception:
                file_logger.warning("Version information not available")

        # Parse discovery method and adjust for source type
        try:
            discovery_method_enum = DiscoveryMethod(discovery_method.lower())
        except ValueError:
            console = Console()
            console.print(
                Panel(
                    Text(
                        f"Invalid discovery method '{discovery_method}'\nMust be one of: ssh, http, both, github_api",
                        style="bold red",
                    ),
                    title="Configuration Error",
                    border_style="red",
                )
            )
            raise typer.Exit(ExitCode.CONFIGURATION_ERROR)

        # Auto-adjust discovery method for GitHub
        if detected_source_type == SourceType.GITHUB:
            if discovery_method_enum not in [DiscoveryMethod.GITHUB_API, DiscoveryMethod.HTTP]:
                discovery_method_enum = DiscoveryMethod.GITHUB_API
                if not quiet:
                    console.print(
                        "[cyan]ℹ[/cyan] Using GitHub API discovery for GitHub source"
                    )

        # Load and validate configuration
        try:
            config = load_config(
                host=host,
                port=port,  # Leave as None for GitHub, will default to 29418 for Gerrit
                base_url=base_url,
                ssh_user=ssh_user,
                ssh_identity_file=ssh_identity_file,
                path=output_path,
                skip_archived=skip_archived,
                allow_nested_git=allow_nested_git,
                nested_protection=nested_protection,
                move_conflicting=move_conflicting,
                threads=threads,
                depth=depth,
                branch=branch,
                mirror=mirror,
                use_https=use_https,
                keep_remote_protocol=keep_remote_protocol,
                strict_host_checking=strict_host_checking,
                clone_timeout=clone_timeout,
                retry_attempts=retry_attempts,
                retry_base_delay=retry_base_delay,
                retry_factor=retry_factor,
                retry_max_delay=retry_max_delay,
                manifest_filename=manifest_filename,
                config_file=config_file,
                verbose=verbose,
                quiet=quiet,
                include_projects=include_project,
                ssh_debug=ssh_debug,
                exit_on_error=exit_on_error,
                discovery_method=discovery_method_enum,
                source_type=detected_source_type,
                github_token=github_token,
                github_org=detected_github_org,
                use_gh_cli=use_gh_cli,
                auto_refresh=not no_refresh,
                force_refresh=force,
                fetch_only=fetch_only,
                skip_conflicts=skip_conflicts,
            )
        except ConfigurationError as e:
            if file_logger:
                file_logger.error("Configuration error: %s", str(e))
            if error_collector and log_file_path:
                error_collector.write_summary_to_file(log_file_path)
            console = Console()
            console.print(
                Panel(
                    Text(str(e), style="bold red"),
                    title="Configuration Error",
                    border_style="red",
                )
            )
            raise typer.Exit(ExitCode.CONFIGURATION_ERROR)

        # Show startup banner if not quiet
        if not quiet:
            _show_startup_banner(console, config)

        # Execute clone operation with Rich status integration
        try:
            batch_result = clone_repositories(config)
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
            raise typer.Exit(ExitCode.DISCOVERY_ERROR)

        # Show final results summary using Rich
        if not quiet:
            show_final_results(
                console, batch_result, str(log_file_path) if log_file_path else None
            )

        # Show error summary if there were issues
        if error_collector and not quiet:
            errors = [
                record.message
                for record in error_collector.errors + error_collector.critical_errors
            ]
            warnings = [record.message for record in error_collector.warnings]
            if errors or warnings:
                show_error_summary(console, errors, warnings)

        # Determine exit code based on results
        if batch_result.failed_count > 0:
            if file_logger:
                file_logger.debug(
                    "Clone completed with %d failures", batch_result.failed_count
                )
            exit_code = int(ExitCode.CLONE_ERROR)
        else:
            if file_logger:
                file_logger.debug("Clone completed successfully")
            exit_code = int(ExitCode.SUCCESS)

        # Optional cleanup
        if cleanup:
            from shutil import rmtree

            try:
                if file_logger:
                    file_logger.debug(
                        "Cleanup enabled - removing cloned directory: %s",
                        config.path,
                    )
                console.print(
                    f"[yellow]🧹 Cleanup enabled - removing cloned directory: {config.path}[/yellow]"
                )
                rmtree(config.path, ignore_errors=True)
                if file_logger:
                    file_logger.debug("Cleanup completed successfully")
                console.print("[green]Cleanup complete.[/green]")
            except Exception as e:
                if file_logger:
                    file_logger.debug("Cleanup failed: %s", str(e))
                console.print(f"[red]Cleanup failed:[/red] {e}")

        # Close file logging and write summary
        if error_collector and log_file_path:
            error_collector.write_summary_to_file(log_file_path)

        if exit_code != 0:
            raise typer.Exit(exit_code)
        return

    except KeyboardInterrupt:
        if file_logger:
            file_logger.warning("Operation cancelled by user (KeyboardInterrupt)")
        if error_collector and log_file_path:
            error_collector.write_summary_to_file(log_file_path)
        console.print("\n[yellow]Operation cancelled by user[/yellow]")
        # Flush console to ensure message is displayed before exit
        if hasattr(console.file, "flush"):
            console.file.flush()
        raise typer.Exit(int(ExitCode.INTERRUPT)) from None
    except typer.Exit:
        # Re-raise typer.Exit exceptions without catching them as generic exceptions
        if error_collector and log_file_path:
            error_collector.write_summary_to_file(log_file_path)
        raise
    except Exception as e:
        import traceback

        # Get the crash context from the traceback
        tb = traceback.extract_tb(e.__traceback__)
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

        if file_logger:
            file_logger.critical(
                "Tool crashed in %s: %s", crash_context, str(e), exc_info=True
            )
        if error_collector:
            error_collector.add_critical_error(
                f"Tool crashed: {type(e).__name__}: {e!s}",
                context=f"function: {crash_context}",
                exception=e,
            )
        if error_collector and log_file_path:
            error_collector.write_summary_to_file(log_file_path)

        # Use Rich status system for crash display
        handle_crash_display(console, e, str(log_file_path) if log_file_path else None)

        if verbose:
            console.print_exception()
        raise typer.Exit(ExitCode.GENERAL_ERROR) from None


def _show_startup_banner(console: Console, config: Any) -> None:
    """Show startup banner with configuration summary."""
    # Show version first
    console.print(_format_version_string())
    console.print()

    # Create summary text
    # Format host with port only if port is set (Gerrit) or omit for GitHub
    if config.effective_port is not None:
        host_display = f"{config.host}:{config.effective_port}"
    else:
        host_display = config.host

    lines = [
        f"Host: [cyan]{host_display} [{config.protocol}][/cyan]",
        f"Output: [cyan]{config.path}[/cyan]",
        f"Threads: [cyan]{config.effective_threads}[/cyan]",
    ]

    if config.ssh_user:
        lines.append(f"SSH User: [cyan]{config.ssh_user}[/cyan]")

    if config.ssh_identity_file:
        lines.append(f"SSH Identity: [cyan]{config.ssh_identity_file}[/cyan]")

    if config.depth:
        lines.append(f"Depth: [cyan]{config.depth}[/cyan]")

    if config.branch:
        lines.append(f"Branch: [cyan]{config.branch}[/cyan]")

    # Add mirror status
    lines.append(f"Git Mirror: [cyan]{config.mirror}[/cyan]")

    # Add common options
    lines.extend(
        [
            f"Discovery Method: [cyan]{str(getattr(config, 'discovery_method', DiscoveryMethod.SSH).value).upper()}[/cyan]",
            f"Skip Archived: [cyan]{config.skip_archived}[/cyan]",
        ]
    )

    # Add Gerrit-specific options only for Gerrit sources
    if config.source_type == SourceType.GERRIT:
        lines.extend(
            [
                f"Allow Nested Git: [cyan]{getattr(config, 'allow_nested_git', False)}[/cyan]",
                f"Nested Protection: [cyan]{getattr(config, 'nested_protection', False)}[/cyan]",
                f"Move Conflicting: [cyan]{getattr(config, 'move_conflicting', True)}[/cyan]",
            ]
        )

    # Add remaining common options
    lines.extend(
        [
            f"Strict Host Check: [cyan]{config.strict_host_checking}[/cyan]",
            f"Include Filter: [cyan]{', '.join(config.include_projects) if getattr(config, 'include_projects', []) else '—'}[/cyan]",
            f"SSH Debug: [cyan]{getattr(config, 'ssh_debug', False)}[/cyan]",
            f"Exit on Error: [cyan]{getattr(config, 'exit_on_error', False)}[/cyan]",
        ]
    )

    summary_text = Text.from_markup("\n".join(lines))

    # Set title based on source type
    if config.source_type == SourceType.GITHUB:
        title = "[bold]GitHub Clone Configuration[/bold]"
    else:
        title = "[bold]Gerrit Clone Configuration[/bold]"

    panel = Panel(
        summary_text,
        title=title,
        border_style="blue",
        padding=(1, 2),
    )

    console.print(panel)


@app.command()
def refresh(
    output_path: Path = typer.Option(
        Path(),
        "--output-path",
        help="Path to clone directory to refresh (default: current directory)",
        envvar="OUTPUT_PATH",
        exists=True,
        file_okay=False,
        dir_okay=True,
        resolve_path=False,
    ),
    threads: int | None = typer.Option(
        None,
        "--threads",
        help="Number of concurrent refresh operations (default: auto-detect based on CPU cores)",
        min=1,
    ),
    fetch_only: bool = typer.Option(
        False,
        "--fetch-only",
        help="Only fetch changes without merging (safer, allows inspection before merge)",
    ),
    prune: bool = typer.Option(
        True,
        "--prune / --no-prune",
        help="Prune deleted remote branches during fetch",
    ),
    timeout: int = typer.Option(
        300,
        "--timeout",
        help="Timeout for each git operation in seconds (min: 10, max: 1800)",
        min=10,
        max=1800,
    ),
    skip_conflicts: bool = typer.Option(
        True,
        "--skip-conflicts / --no-skip-conflicts",
        help="Skip repositories with uncommitted changes or conflicts",
    ),
    auto_stash: bool = typer.Option(
        False,
        "--auto-stash",
        help="Automatically stash uncommitted changes before refresh and restore after",
    ),
    strategy: str = typer.Option(
        "merge",
        "--strategy",
        help="Git pull strategy: 'merge' (fast-forward only) or 'rebase'",
    ),
    filter_gerrit_only: bool = typer.Option(
        True,
        "--gerrit-only / --all-repos",
        help="Only refresh repositories with Gerrit remotes",
    ),
    exit_on_error: bool = typer.Option(
        False,
        "--exit-on-error",
        help="Exit immediately when first error occurs (useful for debugging)",
    ),
    dry_run: bool = typer.Option(
        False,
        "--dry-run",
        help="Show what would be refreshed without making any changes",
    ),
    force: bool = typer.Option(
        False,
        "--force",
        "-f",
        help=(
            "Force refresh by automatically stashing uncommitted changes (without prompting), "
            "fixing detached HEAD states, and updating upstream tracking. This can be disruptive "
            "to your working copies; use with care and recover changes from `git stash` if needed."
        ),
    ),
    recursive: bool = typer.Option(
        True,
        "--recursive / --no-recursive",
        help="Recursively discover repositories in subdirectories (default: recursive)",
    ),
    verbose: bool = typer.Option(
        False,
        "--verbose",
        "-v",
        help="Enable verbose output with detailed logging",
    ),
    quiet: bool = typer.Option(
        False,
        "--quiet",
        "-q",
        help="Suppress non-essential output",
    ),
    manifest_filename: str | None = typer.Option(
        None,
        "--manifest-filename",
        help="Output manifest filename (default: refresh-manifest-TIMESTAMP.json)",
    ),
) -> None:
    """Refresh local content cloned from a Gerrit server.

    Scans the specified directory for Git repositories and updates them by pulling
    latest changes from their Gerrit remotes. Supports parallel updates, automatic
    stash handling, and various safety features.

    Examples:

        # Refresh all repos in current directory
        gerrit-clone refresh

        # Refresh ONAP repositories
        gerrit-clone refresh --output-path ~/repos/onap

        # Fetch only (don't merge)
        gerrit-clone refresh --output-path ~/repos --fetch-only

        # Use 16 threads for faster refresh
        gerrit-clone refresh --output-path ~/repos --threads 16

        # Auto-stash uncommitted changes
        gerrit-clone refresh --output-path ~/repos --auto-stash

        # Dry run (show what would be updated)
        gerrit-clone refresh --output-path ~/repos --dry-run
    """
    # Configure graceful interrupt handling for multi-threaded operations
    handle_sigint_gracefully()

    console = Console()

    # Validate strategy
    if strategy not in ("merge", "rebase"):
        console.print(f"[red]❌ Invalid pull strategy: {strategy}. Must be 'merge' or 'rebase'.[/red]")
        raise typer.Exit(ExitCode.VALIDATION_ERROR.value)

    # Display version
    console.print(_format_version_string("refresh"))
    console.print()

    # Initialize logging
    cli_args = cli_args_to_dict(**locals())

    from gerrit_clone.file_logging import get_default_log_path

    log_file_path = get_default_log_path("refresh", output_path)

    file_logger, error_collector = init_logging(
        log_file=log_file_path,
        disable_file=False,
        log_level="DEBUG",
        console_level="DEBUG" if verbose else "INFO",
        quiet=quiet,
        verbose=verbose,
        cli_args=cli_args,
        host=None,
    )

    if log_file_path and verbose:
        console.print(f"📝 Logging to: [cyan]{log_file_path}[/cyan]")
        console.print()

    # Display configuration summary
    console.print("[bold blue]Refresh Configuration[/bold blue]")
    console.print(f"Base Path: [cyan]{output_path}[/cyan]")
    console.print(f"Threads: [cyan]{threads or 'auto-detect'}[/cyan]")
    console.print(f"Mode: [cyan]{'Fetch Only' if fetch_only else f'Pull ({strategy})'}[/cyan]")
    console.print(f"Prune: [cyan]{prune}[/cyan]")
    console.print(f"Timeout: [cyan]{timeout}s[/cyan]")
    console.print(f"Skip Conflicts: [cyan]{skip_conflicts}[/cyan]")
    console.print(f"Auto Stash: [cyan]{auto_stash}[/cyan]")
    console.print(f"Filter: [cyan]{'Gerrit only' if filter_gerrit_only else 'All repos'}[/cyan]")
    console.print(f"Dry Run: [cyan]{dry_run}[/cyan]")
    console.print(f"Force: [cyan]{force}[/cyan]")
    console.print(f"Recursive: [cyan]{recursive}[/cyan]")
    console.print()

    try:
        # Execute refresh
        result = refresh_repositories(
            base_path=output_path,
            config=None,
            timeout=timeout,
            fetch_only=fetch_only,
            prune=prune,
            skip_conflicts=skip_conflicts,
            auto_stash=auto_stash,
            strategy=strategy,
            filter_gerrit_only=filter_gerrit_only,
            threads=threads,
            exit_on_error=exit_on_error,
            dry_run=dry_run,
            force=force,
            recursive=recursive,
        )

        # Display results
        _show_refresh_results(console, result, dry_run)

        # Write manifest with timestamp by default, or use specified filename
        if manifest_filename:
            manifest_file = output_path / manifest_filename
        else:
            timestamp = datetime.now(UTC).strftime("%Y-%m-%dT%H-%M-%SZ")
            manifest_file = output_path / f"refresh-manifest-{timestamp}.json"
        _write_refresh_manifest(manifest_file, result)
        console.print(f"📄 Manifest: [cyan]{manifest_file}[/cyan]")
        console.print()

        # Determine exit code
        if result.failed_count > 0:
            console.print(f"[yellow]⚠️  {result.failed_count} repositories failed to refresh[/yellow]")
            raise typer.Exit(ExitCode.GENERAL_ERROR.value)
        elif result.conflicts_count > 0:
            console.print(f"[yellow]⚠️  {result.conflicts_count} repositories have conflicts[/yellow]")
            raise typer.Exit(ExitCode.GENERAL_ERROR.value)
        else:
            console.print("[green]✅ All repositories refreshed successfully![/green]")
            raise typer.Exit(ExitCode.SUCCESS.value)

    except KeyboardInterrupt:
        console.print("\n[yellow]⚠️  Refresh cancelled by user[/yellow]")
        # Flush console to ensure message is displayed before exit
        if hasattr(console.file, "flush"):
            console.file.flush()
        raise typer.Exit(ExitCode.INTERRUPT.value)
    except typer.Exit:
        # Re-raise typer.Exit without catching it
        raise
    except Exception as e:
        console.print(f"[red]❌ Refresh failed: {e}[/red]")
        if verbose:
            import traceback
            console.print(traceback.format_exc())
        raise typer.Exit(ExitCode.GENERAL_ERROR.value)


def _show_refresh_results(console: Console, result: RefreshBatchResult, dry_run: bool) -> None:
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
        console.print(f"Repositories Updated: [cyan]{result.total_commits_pulled}[/cyan]")
        console.print(f"Total Files Changed: [cyan]{result.total_files_changed}[/cyan]")
        console.print()

    # Show failed/conflict details
    failed_results = [r for r in result.results if r.failed or r.has_conflicts]
    if failed_results:
        console.print("[bold yellow]Issues:[/bold yellow]")
        for r in failed_results[:10]:  # Show first 10
            status_emoji = "❌" if r.failed else "⚠️"
            console.print(f"  {status_emoji} {r.project_name}: {r.error_message or r.status.value}")

        if len(failed_results) > 10:
            console.print(f"  ... and {len(failed_results) - 10} more (see manifest for details)")
        console.print()


def _write_refresh_manifest(manifest_path: Path, result: RefreshBatchResult) -> None:
    """Write refresh manifest to JSON file.

    Args:
        manifest_path: Path to write manifest
        result: Refresh batch result
    """
    try:
        with manifest_path.open("w", encoding="utf-8") as f:
            json.dump(result.to_dict(), f, indent=2, ensure_ascii=False)
    except Exception as e:
        logger.warning(f"Failed to write refresh manifest: {e}")


@app.command(name="mirror")
def mirror(
    server: str = typer.Option(
        ...,
        "--server",
        help="Gerrit server hostname",
        envvar="GERRIT_HOST",
    ),
    org: str | None = typer.Option(
        None,
        "--org",
        help=(
            "Target GitHub organization for mirrored content "
            "(if not specified, user's primary org/account will be used)"
        ),
        envvar="GITHUB_ORG",
    ),
    projects: str | None = typer.Option(
        None,
        "--projects",
        help=(
            "Filter operations to a subset of the Gerrit project "
            "hierarchy (comma-separated, e.g., 'ccsdk, oom')"
        ),
        envvar="GERRIT_PROJECTS",
    ),
    output_path: Path = typer.Option(
        Path("/tmp/gerrit-mirror"),
        "--output-path",
        help="Local filesystem folder/path for cloned projects",
        envvar="MIRROR_OUTPUT_PATH",
        file_okay=False,
        resolve_path=False,
    ),
    recreate: bool = typer.Option(
        False,
        "--recreate",
        help="Delete and recreate any pre-existing remote GitHub repositories",
        envvar="GERRIT_MIRROR_RECREATE",
    ),
    overwrite: bool = typer.Option(
        False,
        "--overwrite",
        help="Overwrite local Git repositories at the target filesystem path",
        envvar="GERRIT_MIRROR_OVERWRITE",
    ),
    port: int | None = typer.Option(
        None,
        "--port",
        "-p",
        help="Gerrit port (default: 29418 for SSH)",
        envvar="GERRIT_PORT",
        min=1,
        max=65535,
    ),
    ssh_user: str | None = typer.Option(
        None,
        "--ssh-user",
        "-u",
        help="SSH username for Gerrit clone operations",
        envvar="GERRIT_SSH_USER",
    ),
    ssh_identity_file: Path | None = typer.Option(
        None,
        "--ssh-private-key",
        "-i",
        help="SSH private key file for authentication",
        envvar="GERRIT_SSH_PRIVATE_KEY",
        exists=True,
        file_okay=True,
        dir_okay=False,
        readable=True,
        resolve_path=True,
    ),
    threads: int | None = typer.Option(
        None,
        "--threads",
        "-t",
        help="Number of concurrent operations (default: auto)",
        envvar="GERRIT_THREADS",
        min=1,
    ),
    github_token: str | None = typer.Option(
        None,
        "--github-token",
        help=(
            "GitHub personal access token "
            "(default: GITHUB_TOKEN environment variable)"
        ),
        envvar="GITHUB_TOKEN",
    ),
    skip_archived: bool = typer.Option(
        True,
        "--skip-archived/--include-archived",
        help="Skip archived/read-only repositories",
        envvar="GERRIT_SKIP_ARCHIVED",
    ),
    discovery_method: str = typer.Option(
        "ssh",
        "--discovery-method",
        help="Method for discovering projects: ssh (default), http (REST API only), or both (union of both methods with SSH metadata preferred)",
        envvar="GERRIT_DISCOVERY_METHOD",
    ),
    use_https: bool = typer.Option(
        False,
        "--https/--ssh",
        help="Use HTTPS for cloning instead of SSH",
        envvar="GERRIT_USE_HTTPS",
    ),
    mirror: bool = typer.Option(
        True,
        "--mirror/--no-mirror",
        help="Use git clone --mirror for complete repository metadata (all refs, tags, branches). Creates bare repository.",
        envvar="GERRIT_MIRROR",
    ),
    strict_host_checking: bool = typer.Option(
        True,
        "--strict-host/--accept-unknown-host",
        help="SSH strict host key checking",
        envvar="GERRIT_STRICT_HOST",
    ),
    manifest_filename: str = typer.Option(
        "mirror-manifest.json",
        "--manifest-filename",
        help="Output manifest filename",
        envvar="GERRIT_MIRROR_MANIFEST",
    ),
    verbose: bool = typer.Option(
        False,
        "--verbose",
        "-v",
        help="Enable verbose/debug output",
        envvar="GERRIT_VERBOSE",
    ),
    quiet: bool = typer.Option(
        False,
        "--quiet",
        "-q",
        help="Suppress all output except errors",
        envvar="GERRIT_QUIET",
    ),
    http_user: str | None = typer.Option(
        None,
        "--http-user",
        help="HTTP username for Gerrit authentication (highest priority)",
        envvar="GERRIT_HTTP_USER",
    ),
    http_password: str | None = typer.Option(
        None,
        "--http-password",
        help="HTTP password for Gerrit authentication (highest priority)",
        envvar="GERRIT_HTTP_PASSWORD",
    ),
    no_netrc: bool = typer.Option(
        False,
        "--no-netrc",
        help="Disable .netrc credential lookup for HTTP authentication",
        envvar="GERRIT_NO_NETRC",
    ),
    netrc_file: Path | None = typer.Option(
        None,
        "--netrc-file",
        help="Explicit path to .netrc file for HTTP credentials",
        envvar="GERRIT_NETRC_FILE",
        exists=True,
        file_okay=True,
        dir_okay=False,
        readable=True,
        resolve_path=True,
    ),
    netrc_optional: bool = typer.Option(
        True,
        "--netrc-optional/--netrc-required",
        help="Whether to fail if .netrc file is not found (default: optional)",
        envvar="GERRIT_NETRC_OPTIONAL",
    ),
) -> None:
    """Mirror repositories from a Gerrit server to GitHub.

    This command discovers projects on a Gerrit server, clones them locally,
    and mirrors them to GitHub repositories. Gerrit project hierarchies
    (e.g., ccsdk/apps) are transformed to GitHub-compatible names
    (e.g., ccsdk-apps).

    Examples:

        # Mirror all projects to a GitHub org
        gerrit-clone mirror --server gerrit.onap.org --org myorg

        # Mirror specific projects
        gerrit-clone mirror --server gerrit.onap.org --org myorg \\
          --projects "ccsdk, oom, cps"

        # Recreate existing GitHub repos
        gerrit-clone mirror --server gerrit.onap.org --org myorg \\
          --recreate --overwrite

        # Use HTTPS for cloning and include archived projects
        gerrit-clone mirror --server gerrit.onap.org --org myorg \\
          --https --include-archived

        # Use HTTP API for discovery (no SSH required)
        gerrit-clone mirror --server gerrit.onap.org --org myorg \\
          --discovery-method http --https

        # Mirror with explicit HTTP credentials (highest priority)
        gerrit-clone mirror --server gerrit.onap.org --org myorg \\
          --https --http-user myuser --http-password mypass

        # Mirror with credentials from specific .netrc file
        gerrit-clone mirror --server gerrit.onap.org --org myorg \\
          --https --netrc-file ~/.netrc.gerrit
    """
    # Configure graceful interrupt handling for multi-threaded operations
    handle_sigint_gracefully()

    console = Console(stderr=True)

    try:
        # Validate mutually exclusive options
        if verbose and quiet:
            console.print(
                "[red]Error:[/red] --verbose and --quiet cannot "
                "be used together"
            )
            raise typer.Exit(ExitCode.CONFIGURATION_ERROR)

        # Handle credential resolution for HTTP authentication
        # Priority: 1. CLI arguments (--http-user/--http-password)
        #           2. .netrc file
        #           3. Environment variables (GERRIT_HTTP_USER/GERRIT_HTTP_PASSWORD)
        #           4. Fallback environment variables (GERRIT_USERNAME/GERRIT_PASSWORD)
        if use_https:
            try:
                resolved_creds = resolve_gerrit_credentials(
                    host=server,
                    explicit_username=http_user,
                    explicit_password=http_password,
                    use_netrc=not no_netrc,
                    netrc_file=netrc_file,
                    env_username_var="GERRIT_HTTP_USER",
                    env_password_var="GERRIT_HTTP_PASSWORD",
                    fallback_env_username_var="GERRIT_USERNAME",
                    fallback_env_password_var="GERRIT_PASSWORD",
                )
                if resolved_creds:
                    # Set environment variables for downstream code to use
                    os.environ["GERRIT_HTTP_USER"] = resolved_creds.username
                    os.environ["GERRIT_HTTP_PASSWORD"] = resolved_creds.password
                    if not quiet:
                        console.print(
                            f"🔐 Loaded HTTP credentials from {resolved_creds.source_detail}"
                        )
                elif not no_netrc and not netrc_optional:
                    # No credentials found and netrc was required
                    console.print(
                        "[red]Error:[/red] No credentials found and --netrc-required set"
                    )
                    raise typer.Exit(ExitCode.CONFIGURATION_ERROR)
            except FileNotFoundError:
                if not netrc_optional:
                    console.print(
                        "[red]Error:[/red] No .netrc file found and --netrc-required set"
                    )
                    raise typer.Exit(ExitCode.CONFIGURATION_ERROR)
            except NetrcParseError as e:
                console.print(f"[red]Error:[/red] Failed to parse .netrc file: {e}")
                raise typer.Exit(ExitCode.CONFIGURATION_ERROR)

        # Show startup banner
        console.print(_format_version_string(command="mirror"))
        console.print()

        # Initialize GitHub API
        if not quiet:
            console.print("🔑 Authenticating with GitHub...")

        try:
            github_api = GitHubAPI(token=github_token)
        except GitHubAuthError as e:
            console.print(f"[red]Error:[/red] {e}")
            raise typer.Exit(ExitCode.CONFIGURATION_ERROR)

        # Determine target org/user
        if org is None:
            if not quiet:
                console.print(
                    "ℹ️ No organization specified, "
                    "using default from GitHub token..."
                )
            org, is_org = get_default_org_or_user(github_api)
            if not quiet:
                org_type = "organization" if is_org else "user account"
                console.print(f"✓ Using {org_type}: [cyan]{org}[/cyan]")
        elif not quiet:
            console.print(
                f"✓ Using specified organization: [cyan]{org}[/cyan]"
            )

        # Parse project filters
        project_filters: list[str] = []
        if projects:
            project_filters = [
                p.strip() for p in projects.split(",") if p.strip()
            ]
            if not quiet:
                console.print(
                    f"📋 Project filters: "
                    f"[cyan]{', '.join(project_filters)}[/cyan]"
                )

        # Build Gerrit configuration
        from gerrit_clone.models import Config

        # Validate discovery method
        try:
            discovery_enum = DiscoveryMethod(discovery_method.lower())
        except ValueError:
            console.print(
                Panel(
                    Text(
                        f"Invalid discovery method '{discovery_method}'\nMust be one of: ssh, http, both",
                        style="bold red",
                    ),
                    title="Configuration Error",
                    border_style="red",
                )
            )
            raise typer.Exit(ExitCode.CONFIGURATION_ERROR)

        config = Config(
            host=server,
            port=port or 29418,
            ssh_user=ssh_user,
            ssh_identity_file=ssh_identity_file,
            path=output_path,
            threads=threads,
            skip_archived=skip_archived,
            discovery_method=discovery_enum,
            strict_host_checking=strict_host_checking,
            use_https=use_https,
            mirror=mirror,
            retry_policy=RetryPolicy(),
        )

        if not quiet:
            console.print(
                f"🌐 Connecting to Gerrit: [cyan]{server}[/cyan]"
            )

        # Discover projects
        all_projects, discovery_stats = discover_projects(config)

        if not all_projects:
            console.print("[yellow]No projects found on Gerrit server[/yellow]")
            raise typer.Exit(0)

        # Filter projects by hierarchy if specified
        if project_filters:
            projects_to_mirror = filter_projects_by_hierarchy(
                all_projects, project_filters
            )
        else:
            projects_to_mirror = all_projects

        if not projects_to_mirror:
            console.print(
                "[yellow]No projects matched the specified filters[/yellow]"
            )
            raise typer.Exit(0)

        if not quiet:
            console.print(
                f"📦 Found [cyan]{len(projects_to_mirror)}[/cyan] "
                f"projects to mirror"
            )
            console.print()

        # Create mirror manager
        mirror_manager = MirrorManager(
            config=config,
            github_api=github_api,
            github_org=org,
            recreate=recreate,
            overwrite=overwrite,
        )

        # Start mirroring
        started_at = datetime.now(UTC)
        if not quiet:
            console.print("🚀 Starting mirror operation...")

        results = mirror_manager.mirror_projects(projects_to_mirror)

        completed_at = datetime.now(UTC)

        # Create batch result
        batch_result = MirrorBatchResult(
            results=results,
            started_at=started_at,
            completed_at=completed_at,
            github_org=org,
            gerrit_host=server,
        )

        # Write manifest
        manifest_path = output_path / manifest_filename
        manifest_path.parent.mkdir(parents=True, exist_ok=True)
        with open(manifest_path, "w") as f:
            json.dump(batch_result.to_dict(), f, indent=2)

        if not quiet:
            console.print()
            console.print(
                f"✓ Manifest written to: [cyan]{manifest_path}[/cyan]"
            )

        # Show summary
        if not quiet:
            console.print()
            console.print("[bold]Mirror Summary[/bold]")
            console.print(f"  Discovery Method: [cyan]{discovery_enum.value.upper()}[/cyan]")
            console.print(f"  Clone Protocol: [cyan]{'HTTPS' if use_https else 'SSH'}[/cyan]")
            console.print(f"  Skip Archived: [cyan]{skip_archived}[/cyan]")
            console.print(f"  Total: {batch_result.total_count}")
            console.print(
                f"  [green]Succeeded: {batch_result.success_count}[/green]"
            )
            console.print(
                f"  [red]Failed: {batch_result.failed_count}[/red]"
            )
            console.print(
                f"  [yellow]Skipped: {batch_result.skipped_count}[/yellow]"
            )
            console.print(
                f"  Duration: {batch_result.duration_seconds:.1f}s"
            )

        # Close GitHub API client
        github_api.close()

        # Exit with appropriate code
        if batch_result.failed_count > 0:
            raise typer.Exit(ExitCode.CLONE_ERROR)
        else:
            raise typer.Exit(0)

    except GitHubAPIError as e:
        console.print(f"[red]GitHub API Error:[/red] {e}")
        if verbose:
            console.print_exception()
        raise typer.Exit(ExitCode.GENERAL_ERROR)
    except DiscoveryError as e:
        console.print(f"[red]Discovery Error:[/red] {e}")
        if verbose:
            console.print_exception()
        raise typer.Exit(ExitCode.DISCOVERY_ERROR)
    except ConfigurationError as e:
        console.print(f"[red]Configuration Error:[/red] {e}")
        if verbose:
            console.print_exception()
        raise typer.Exit(ExitCode.CONFIGURATION_ERROR)
    except KeyboardInterrupt:
        console.print("\n[yellow]Mirror operation cancelled by user[/yellow]")
        # Flush console to ensure message is displayed before exit
        if hasattr(console.file, "flush"):
            console.file.flush()
        raise typer.Exit(ExitCode.INTERRUPT)
    except typer.Exit:
        # Re-raise typer.Exit without catching it
        raise
    except Exception as e:
        console.print(f"[red]Unexpected Error:[/red] {e}")
        if verbose:
            console.print_exception()
        raise typer.Exit(ExitCode.GENERAL_ERROR)


@app.command()
def reset(
    org: str = typer.Option(
        ...,
        "--org",
        help="GitHub organization to reset (delete all repositories)",
        envvar="GITHUB_ORG",
    ),
    path: Path = typer.Option(
        Path("."),
        "--path",
        help="Local Gerrit clone folder hierarchy (default: current directory)",
        envvar="GERRIT_CLONE_PATH",
        file_okay=False,
        resolve_path=True,
    ),
    compare: bool = typer.Option(
        False,
        "--compare",
        help="Compare local Gerrit clone with remote GitHub repositories before deletion",
    ),
    github_token: str | None = typer.Option(
        None,
        "--github-token",
        help="GitHub personal access token (default: GITHUB_TOKEN environment variable)",
        envvar="GITHUB_TOKEN",
    ),
    no_confirm: bool = typer.Option(
        False,
        "--no-confirm",
        help="Skip confirmation prompt and delete immediately",
    ),
    include_automation_prs: bool = typer.Option(
        False,
        "--include-automation-prs",
        help="Include automation PRs (dependabot, pre-commit.ci, etc.) in PR counts (default: exclude)",
    ),
    verbose: bool = typer.Option(
        False,
        "--verbose",
        "-v",
        help="Enable verbose output",
        envvar="GERRIT_VERBOSE",
    ),
) -> None:
    """
    Remove all repositories from a GitHub organization.

    This command:

    1. Lists all repositories in the organization with PR/issue counts
       (by default, excludes automation PRs from dependabot, pre-commit.ci, etc.)

    2. Optionally compares with local Gerrit clone (--compare flag)

    3. Prompts for confirmation with unique hash (unless --no-confirm)

    4. Deletes all repositories permanently

    [red]WARNING: This operation is DESTRUCTIVE and IRREVERSIBLE![/red]

    Examples:

        # List repos and prompt for confirmation (excludes automation PRs)
        gerrit-clone reset --org my-test-org

        # Include automation PRs in counts
        gerrit-clone reset --org my-test-org --include-automation-prs

        # Compare with local clone before deletion
        gerrit-clone reset --org my-test-org --path /tmp/gerrit-mirror --compare

        # Delete immediately without prompt (DANGEROUS!)
        gerrit-clone reset --org my-test-org --no-confirm
    """
    console = Console(stderr=True)

    try:
        # Validate GitHub token
        if not github_token:
            console.print(
                "[red]❌ GitHub token required. "
                "Set GITHUB_TOKEN environment variable or use --github-token[/red]"
            )
            raise typer.Exit(ExitCode.CONFIGURATION_ERROR)

        # Show banner
        console.print(_format_version_string(command="reset"))
        console.print()

        # Initialize reset manager
        manager = ResetManager(
            org=org,
            github_token=github_token,
            local_path=path,
            console=console,
            include_automation_prs=include_automation_prs,
        )

        # Check token permissions using the GitHub API
        has_permissions = asyncio.run(manager.check_token_permissions())
        if not has_permissions:
            console.print(
                "[red]❌ Insufficient permissions. "
                "Ensure your GitHub token has 'delete_repo' scope.[/red]"
            )
            raise typer.Exit(ExitCode.CONFIGURATION_ERROR)

        # Execute reset operation
        result = asyncio.run(
            manager.execute_reset(
                compare=compare,
                no_confirm=no_confirm,
            )
        )

        # Display final summary
        if result.deleted_repos > 0:
            console.print(
                f"\n🎉 Reset complete: {result.deleted_repos}/{result.total_repos} "
                "repositories deleted"
            )

            if result.failed_deletions:
                console.print(
                    f"⚠️  {len(result.failed_deletions)} deletions failed"
                )

            if result.had_unsynchronized and compare:
                console.print(
                    f"⚠️  Note: {len(result.unsynchronized_repos)} repositories "
                    "had local/remote differences"
                )

            raise typer.Exit(0)
        else:
            console.print("\n❌ No repositories were deleted")
            raise typer.Exit(0)

    except GitHubAuthError as e:
        console.print(f"[red]❌ GitHub authentication error:[/red] {e}")
        raise typer.Exit(ExitCode.CONFIGURATION_ERROR)
    except GitHubAPIError as e:
        console.print(f"[red]❌ GitHub API error:[/red] {e}")
        raise typer.Exit(ExitCode.GENERAL_ERROR)
    except KeyboardInterrupt:
        console.print("\n❌ Reset cancelled by user")
        raise typer.Exit(1)
    except Exception as e:
        console.print(f"\n[red]Error:[/red] {e}")
        if verbose:
            import traceback
            console.print(traceback.format_exc())
        raise typer.Exit(ExitCode.GENERAL_ERROR)


@app.command(name="config")
def show_config(
    host: str | None = typer.Option(
        None,
        "--host",
        help="Gerrit server hostname",
        envvar="GERRIT_HOST",
    ),
    config_file: Path | None = typer.Option(
        None,
        "--config-file",
        "-c",
        help="Configuration file path",
        exists=True,
        file_okay=True,
        dir_okay=False,
        readable=True,
    ),
) -> None:
    """Show effective configuration from all sources.

    This command shows the configuration that would be used for clone operations,
    including values from environment variables, config files, and defaults.
    """
    console = Console()

    try:
        # Load configuration (allowing missing host for config display)
        if host is None:
            host = "example.gerrit.org"  # Placeholder for config display

        config = load_config(host=host, config_file=config_file)

        # Display configuration
        config_lines = [
            f"Host: [cyan]{config.host}:{config.effective_port} [{config.protocol}][/cyan]",
            f"Base URL: [cyan]{config.base_url}[/cyan]",
            f"SSH User: [cyan]{config.ssh_user or 'default'}[/cyan]",
            f"SSH Identity: [cyan]{config.ssh_identity_file or 'default'}[/cyan]",
            f"Path: [cyan]{config.path}[/cyan]",
            f"Protocol: [cyan]{config.protocol}[/cyan]",
            f"Git Mirror: [cyan]{config.mirror}[/cyan]",
            f"Skip Archived: [cyan]{config.skip_archived}[/cyan]",
            f"Allow Nested Git: [cyan]{getattr(config, 'allow_nested_git', True)}[/cyan]",
            f"Nested Protection: [cyan]{getattr(config, 'nested_protection', True)}[/cyan]",
            f"Move Conflicting: [cyan]{getattr(config, 'move_conflicting', True)}[/cyan]",
            f"Threads: [cyan]{config.effective_threads}[/cyan]",
            f"Clone Timeout: [cyan]{config.clone_timeout}s[/cyan]",
            f"Strict Host Check: [cyan]{config.strict_host_checking}[/cyan]",
            "",
            f"Retry Max Attempts: [cyan]{config.retry_policy.max_attempts}[/cyan]",
            f"Retry Base Delay: [cyan]{config.retry_policy.base_delay}s[/cyan]",
            f"Retry Factor: [cyan]{config.retry_policy.factor}[/cyan]",
            f"Retry Max Delay: [cyan]{config.retry_policy.max_delay}s[/cyan]",
            "",
            f"Manifest File: [cyan]{config.manifest_filename}[/cyan]",
        ]

        if config.depth:
            config_lines.insert(-3, f"Clone Depth: [cyan]{config.depth}[/cyan]")

        if config.branch:
            config_lines.insert(-3, f"Clone Branch: [cyan]{config.branch}[/cyan]")

        config_text = Text.from_markup("\n".join(config_lines))

        panel = Panel(
            config_text,
            title="[bold]Effective Configuration[/bold]",
            border_style="green",
            padding=(1, 2),
        )

        console.print(panel)

    except ConfigurationError as e:
        console = Console()
        console.print(
            Panel(
                Text(str(e), style="bold red"),
                title="Configuration Error",
                border_style="red",
            )
        )
        raise typer.Exit(ExitCode.CONFIGURATION_ERROR)
    except typer.Exit:
        # Re-raise typer.Exit without catching it
        raise
    except Exception as e:
        console.print(f"[red]Error:[/red] {e}")
        raise typer.Exit(ExitCode.GENERAL_ERROR) from None


if __name__ == "__main__":
    app()
