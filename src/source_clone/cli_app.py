# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""Typer application object shared by every source-clone command module.

The application lives here rather than in :mod:`source_clone.cli` so that the
per-command modules can register themselves against it without importing the
public facade that aggregates them.
"""

from __future__ import annotations

import os
import sys

import typer
from rich.console import Console

from source_clone import __version__
from source_clone.logging import get_logger

logger = get_logger(__name__)


def is_github_actions_context() -> bool:
    """Detect if running in GitHub Actions environment."""
    return (
        os.getenv("GITHUB_ACTIONS") == "true"
        or os.getenv("GITHUB_EVENT_NAME", "").strip() != ""
    )


def format_version_string(command: str = "", styled: bool = True) -> str:
    """Format version string with consistent styling.

    Args:
        command: Optional command name to include (e.g., "mirror")
        styled: Whether to include Rich markup styling

    Returns:
        Formatted version string
    """
    if styled:
        if command:
            return f"🏷️  [bold]source-clone {command}[/bold] version [cyan]{__version__}[/cyan]"
        return f"🏷️  source-clone version [cyan]{__version__}[/cyan]"
    else:
        if command:
            return f"🏷️  source-clone {command} version {__version__}"
        return f"🏷️  source-clone version {__version__}"


# Show version information when --help is used.
#
# This runs at import time, before Typer takes over, so it writes to
# stdout directly: the Rich console the commands share is not built yet,
# and this is user-facing CLI output rather than diagnostics, so routing
# it through the logger (stderr, silenceable by level) would be wrong.
if "--help" in sys.argv:
    try:
        sys.stdout.write(f"{format_version_string(styled=False)}\n")
    except Exception as exc:
        logger.debug("Failed to format version string: %s", exc, exc_info=True)
        sys.stdout.write(
            "\u26a0\ufe0f source-clone version information not available\n"
        )


def version_callback(value: bool) -> None:
    """Show version information."""
    if value:
        console = Console()
        console.print(format_version_string())
        raise typer.Exit()


app = typer.Typer(
    name="source-clone",
    help=(
        "A multi-threaded CLI tool for bulk cloning, mirroring and refreshing "
        "repositories from Gerrit servers and GitHub organisations."
    ),
    no_args_is_help=True,
    rich_markup_mode="rich",
    add_completion=True,
)

VERSION = typer.Option(
    None,
    "--version",
    callback=version_callback,
    is_eager=True,
    help="Show version information",
)


@app.callback()
def main(
    version: bool | None = VERSION,
) -> None:
    """Main CLI entry point with top-level options."""
    pass
