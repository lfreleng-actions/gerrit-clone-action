# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""Startup banner for the clone command.

Prints the version and a panel summarising the effective configuration
before cloning starts.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from rich.panel import Panel
from rich.text import Text

from source_clone.cli_app import format_version_string
from source_clone.models import DiscoveryMethod, SourceType

if TYPE_CHECKING:
    from rich.console import Console


def show_startup_banner(console: Console, config: Any) -> None:
    """Show startup banner with configuration summary."""
    # Show version first
    console.print(format_version_string())
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
    discovery = getattr(config, "discovery_method", None) or DiscoveryMethod.SSH
    lines.extend(
        [
            f"Discovery Method: [cyan]{discovery.value.upper()}[/cyan]",
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
            f"Exclude Filter: [cyan]{', '.join(config.exclude_projects) if getattr(config, 'exclude_projects', []) else '—'}[/cyan]",
            f"SSH Debug: [cyan]{getattr(config, 'ssh_debug', False)}[/cyan]",
            f"Exit on Error: [cyan]{getattr(config, 'exit_on_error', False)}[/cyan]",
        ]
    )

    summary_text = Text.from_markup("\n".join(lines))

    # Set title based on source type
    if config.source_type == SourceType.GITHUB:
        title = "[bold]GitHub Clone Configuration[/bold]"
    else:
        title = "[bold]Source Clone Configuration[/bold]"

    panel = Panel(
        summary_text,
        title=title,
        border_style="blue",
        padding=(1, 2),
    )

    console.print(panel)
