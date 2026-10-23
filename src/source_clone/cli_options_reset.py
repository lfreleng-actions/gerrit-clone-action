# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""Typer option definitions for the ``reset`` command.

Declaring each option as a module-level constant keeps the command signature
in :mod:`source_clone.cli_reset` readable while leaving the option metadata
(flags, help text, environment variables and validation) unchanged.
"""

from __future__ import annotations

from pathlib import Path

import typer

ORG = typer.Option(
    ...,
    "--org",
    help="GitHub organization to reset (delete all repositories)",
    envvar="GITHUB_ORG",
)
PATH = typer.Option(
    Path(),
    "--output-path",
    "--path",  # Backward-compatible alias
    help="Local Gerrit clone folder hierarchy (default: current directory)",
    envvar="GERRIT_CLONE_PATH",
    file_okay=False,
    resolve_path=True,
)
COMPARE = typer.Option(
    False,
    "--compare",
    help="Compare local Gerrit clone with remote GitHub repositories before deletion",
)
GITHUB_TOKEN = typer.Option(
    None,
    "--github-token",
    help="GitHub personal access token (default: GITHUB_TOKEN environment variable)",
    envvar="GITHUB_TOKEN",
)
NO_CONFIRM = typer.Option(
    False,
    "--no-confirm",
    help="Skip confirmation prompt and delete immediately",
)
INCLUDE_AUTOMATION_PRS = typer.Option(
    False,
    "--include-automation-prs",
    help="Include automation PRs (dependabot, pre-commit.ci, etc.) in PR counts (default: exclude)",
)
VERBOSE = typer.Option(
    False,
    "--verbose",
    "-v",
    help="Enable verbose/debug output",
    envvar=["VERBOSE_DEBUG", "GERRIT_VERBOSE"],
)
QUIET = typer.Option(
    False,
    "--quiet",
    "-q",
    help="Suppress all output except errors",
    envvar="GERRIT_QUIET",
)
