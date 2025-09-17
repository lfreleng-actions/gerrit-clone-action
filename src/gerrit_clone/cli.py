# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2025 Matthew Watkins <mwatkins@linuxfoundation.org>

"""Typer-based CLI for gerrit-clone tool."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import typer
from rich.console import Console
from rich.panel import Panel
from rich.text import Text

from gerrit_clone import __version__
from gerrit_clone.clone_manager import clone_repositories
from gerrit_clone.config import ConfigurationError, load_config
from gerrit_clone.logging import setup_logging


def version_callback(value: bool) -> None:
    """Show version information."""
    if value:
        console = Console()
        console.print(f"gerrit-clone version [cyan]{__version__}[/cyan]")
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
        help="Gerrit server hostname",
        envvar="GERRIT_HOST",
    ),
    port: int | None = typer.Option(
        None,
        "--port",
        "-p",
        help="Gerrit port (default: 29418 for SSH, 443 for HTTPS)",
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
    path_prefix: Path = typer.Option(
        Path(),
        "--path-prefix",
        help="Base directory for clone hierarchy",
        envvar="GERRIT_PATH_PREFIX",
        file_okay=False,
        resolve_path=True,
    ),
    skip_archived: bool = typer.Option(
        True,
        "--skip-archived/--include-archived",
        help="Skip archived/read-only repositories",
        envvar="GERRIT_SKIP_ARCHIVED",
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
        help="Timeout per clone operation in seconds",
        envvar="GERRIT_CLONE_TIMEOUT",
        min=30,
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
) -> None:
    """Clone all repositories from a Gerrit server.

    This command discovers all projects on the specified Gerrit server and clones
    them in parallel while preserving the project hierarchy. Repositories are
    cloned over SSH and must be accessible with your configured SSH keys.

    Examples:

        # Clone all active repositories from gerrit.example.org
        gerrit-clone --host gerrit.example.org

        # Clone to specific directory with custom threads
        gerrit-clone --host gerrit.example.org --path-prefix ./repos --threads 8

        # Clone with shallow depth and specific branch
        gerrit-clone --host gerrit.example.org --depth 10 --branch main

        # Include archived repositories
        gerrit-clone --host gerrit.example.org --include-archived
    """
    # Set up console for error handling
    console = Console(stderr=True)

    try:
        # Validate mutually exclusive options
        if verbose and quiet:
            console.print(
                "[red]Error:[/red] --verbose and --quiet cannot be used together"
            )
            raise typer.Exit(1)

        # Set up logging early with progress-aware integration
        logger = setup_logging(
            level="DEBUG" if verbose else "INFO",
            quiet=quiet,
            verbose=verbose,
            enable_progress_aware=True,
        )

        # Load and validate configuration
        try:
            config = load_config(
                host=host,
                port=port,
                base_url=base_url,
                ssh_user=ssh_user,
                ssh_identity_file=ssh_identity_file,
                path_prefix=path_prefix,
                skip_archived=skip_archived,
                threads=threads,
                depth=depth,
                branch=branch,
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
            )
        except ConfigurationError as e:
            console.print(f"[red]Configuration error:[/red] {e}")
            raise typer.Exit(1) from None

        # Show startup banner if not quiet
        if not quiet:
            _show_startup_banner(console, config)

        # Execute clone operation
        batch_result = clone_repositories(config)

        # Determine exit code based on results
        if batch_result.failed_count > 0:
            logger.error(f"Clone completed with {batch_result.failed_count} failures")
            raise typer.Exit(1)
        else:
            logger.info(f"✅ Clone completed successfully - {batch_result.success_count} repositories")
            # Exit successfully - no need to raise Exit(0)
            return

    except KeyboardInterrupt:
        console.print("\n[yellow]Operation cancelled by user[/yellow]")
        raise typer.Exit(130) from None  # Standard exit code for SIGINT
    except typer.Exit:
        # Re-raise typer.Exit exceptions without catching them as generic exceptions
        raise
    except Exception as e:
        console.print(f"[red]Unexpected error:[/red] {e}")
        if verbose:
            console.print_exception()
        raise typer.Exit(1) from None


def _show_startup_banner(console: Console, config: Any) -> None:
    """Show startup banner with configuration summary."""
    # Create summary text
    lines = [
        f"Host: [cyan]{config.host}:{config.effective_port} [{config.protocol}][/cyan]",
        f"Output: [cyan]{config.path_prefix}[/cyan]",
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

    lines.extend(
        [
            f"Skip Archived: [cyan]{config.skip_archived}[/cyan]",
            f"Strict Host Check: [cyan]{config.strict_host_checking}[/cyan]",
        ]
    )

    summary_text = Text.from_markup("\n".join(lines))

    panel = Panel(
        summary_text,
        title="[bold]Gerrit Clone Configuration[/bold]",
        border_style="blue",
        padding=(1, 2),
    )

    console.print(panel)
    console.print()


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
            f"Path Prefix: [cyan]{config.path_prefix}[/cyan]",
            f"Protocol: [cyan]{config.protocol}[/cyan]",
            f"Skip Archived: [cyan]{config.skip_archived}[/cyan]",
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
        console.print(f"[red]Configuration error:[/red] {e}")
        raise typer.Exit(1) from None
    except Exception as e:
        console.print(f"[red]Error:[/red] {e}")
        raise typer.Exit(1) from None


if __name__ == "__main__":
    app()
