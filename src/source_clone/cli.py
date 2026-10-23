# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2025 Matthew Watkins <mwatkins@linuxfoundation.org>

"""Typer-based CLI for source-clone tool.

This module is the public entry point for the command line interface.  It
assembles the Typer application by importing every command module and
re-exports the collaborators that the command bodies resolve at call time, so
that ``source_clone.cli.<name>`` remains the single place to substitute them.
"""

from __future__ import annotations

from source_clone.cli_app import app, main, version_callback
from source_clone.cli_clone import clone
from source_clone.cli_config import show_config
from source_clone.cli_mirror import mirror
from source_clone.cli_refresh import refresh
from source_clone.cli_reset import reset
from source_clone.clone_manager import clone_repositories
from source_clone.netrc import resolve_gerrit_credentials
from source_clone.unified_discovery import discover_projects

# Registered explicitly rather than by decorator so that the order the
# commands appear in ``--help`` does not depend on import order.
app.command()(clone)
app.command()(refresh)
app.command(name="mirror")(mirror)
app.command()(reset)
app.command(name="config")(show_config)

__all__ = [
    "app",
    "clone",
    "clone_repositories",
    "discover_projects",
    "main",
    "mirror",
    "refresh",
    "reset",
    "resolve_gerrit_credentials",
    "show_config",
    "version_callback",
]


if __name__ == "__main__":
    app()
