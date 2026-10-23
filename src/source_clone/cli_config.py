# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""Definition of the ``config`` command.

Shows the effective configuration assembled from environment variables,
configuration files and defaults.

Unlike the rest of the package this module deliberately omits
``from __future__ import annotations``: Typer resolves the command
signature's annotations at runtime to derive each parameter type.
"""

from pathlib import Path

import typer
from rich.console import Console
from rich.panel import Panel
from rich.text import Text

from source_clone.config import ConfigurationError, load_config
from source_clone.error_codes import ExitCode
from source_clone.models import Config

HOST = typer.Option(
    None,
    "--host",
    help="Source hostname (Gerrit server or GitHub URL like github.com/ORG)",
    envvar="GERRIT_HOST",
)
CONFIG_FILE = typer.Option(
    None,
    "--config-file",
    "-c",
    help="Configuration file path",
    exists=True,
    file_okay=True,
    dir_okay=False,
    readable=True,
)


def show_config(
    host: str | None = HOST,
    config_file: Path | None = CONFIG_FILE,
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

        console.print(_build_config_panel(config))

    except ConfigurationError as e:
        console = Console()
        console.print(
            Panel(
                Text(str(e), style="bold red"),
                title="Configuration Error",
                border_style="red",
            )
        )
        raise typer.Exit(ExitCode.CONFIGURATION_ERROR) from e
    except typer.Exit:
        # Re-raise typer.Exit without catching it
        raise
    except Exception as e:
        console.print(f"[red]Error:[/red] {e}")
        raise typer.Exit(ExitCode.GENERAL_ERROR) from e


def _build_config_panel(config: Config) -> Panel:
    """Render the effective configuration for display."""
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

    return Panel(
        config_text,
        title="[bold]Effective Configuration[/bold]",
        border_style="green",
        padding=(1, 2),
    )
