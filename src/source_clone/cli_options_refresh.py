# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""Typer option definitions for the ``refresh`` command.

Declaring each option as a module-level constant keeps the command signature
in :mod:`source_clone.cli_refresh` readable while leaving the option metadata
(flags, help text, environment variables and validation) unchanged.
"""

from __future__ import annotations

from pathlib import Path

import typer

OUTPUT_PATH = typer.Option(
    Path(),
    "--output-path",
    help="Path to clone directory to refresh (default: current directory)",
    envvar="OUTPUT_PATH",
    exists=True,
    file_okay=False,
    dir_okay=True,
    resolve_path=False,
)
INCLUDE_PROJECTS = typer.Option(
    None,
    "--include-projects",
    "--include-project",  # Backward-compatible alias
    help=(
        "Restrict refresh to specific project(s). Supports shell-style wildcards "
        "(*, ?, [seq]), hierarchical matching (e.g. 'ccsdk' includes ccsdk/apps), "
        "and comma or space-separated lists. Repeat for multiple patterns."
    ),
    envvar=None,
)
EXCLUDE_PROJECTS = typer.Option(
    None,
    "--exclude-projects",
    "--exclude-project",  # Backward-compatible alias
    help=(
        "Exclude specific project(s) from refresh. Applied after include filters. "
        "Supports shell-style wildcards (*, ?, [seq]), hierarchical matching, "
        "and comma or space-separated lists. Repeat for multiple patterns."
    ),
    envvar=None,
)
THREADS = typer.Option(
    None,
    "--threads",
    help="Number of concurrent refresh operations (default: auto-detect based on CPU cores)",
    min=1,
)
FETCH_ONLY = typer.Option(
    False,
    "--fetch-only",
    help="Only fetch changes without merging (safer, allows inspection before merge)",
)
PRUNE = typer.Option(
    True,
    "--prune / --no-prune",
    help="Prune deleted remote branches during fetch",
)
TIMEOUT = typer.Option(
    300,
    "--timeout",
    help="Timeout for each git operation in seconds (min: 10, max: 1800)",
    min=10,
    max=1800,
)
SKIP_CONFLICTS = typer.Option(
    True,
    "--skip-conflicts / --no-skip-conflicts",
    help="Skip repositories with uncommitted changes or conflicts",
)
AUTO_STASH = typer.Option(
    False,
    "--auto-stash",
    help="Automatically stash uncommitted changes before refresh and restore after",
)
STRATEGY = typer.Option(
    "merge",
    "--strategy",
    help="Git pull strategy: 'merge' (fast-forward only) or 'rebase'",
)
FILTER_GERRIT_ONLY = typer.Option(
    False,
    "--gerrit-only / --all-repos",
    help=(
        "Only refresh repositories cloned from Gerrit. The nearest "
        "clone-manifest.json above a repository decides when it records "
        "a source type; otherwise a remote pointing at a Gerrit server "
        "does. Default: --all-repos, every repository under the path"
    ),
)
EXIT_ON_ERROR = typer.Option(
    False,
    "--exit-on-error",
    help="Exit immediately when first error occurs (useful for debugging)",
)
DRY_RUN = typer.Option(
    False,
    "--dry-run",
    help="Show what would be refreshed without making any changes",
)
FORCE = typer.Option(
    False,
    "--force",
    "-f",
    help=(
        "Force refresh by automatically stashing uncommitted changes (without prompting), "
        "fixing detached HEAD states, and updating upstream tracking. This can be disruptive "
        "to your working copies; use with care and recover changes from `git stash` if needed."
    ),
)
FORCE_HARD = typer.Option(
    False,
    "--force-hard",
    help=(
        "Everything --force does, plus hard-reset each repository's default "
        "branch to its upstream ref, discarding local commits and "
        "divergence so local content exactly matches the remote. "
        "DESTRUCTIVE: local-only commits are permanently lost."
    ),
)
RECURSIVE = typer.Option(
    True,
    "--recursive / --no-recursive",
    help="Recursively discover repositories in subdirectories (default: recursive)",
)
VERBOSE = typer.Option(
    False,
    "--verbose",
    "-v",
    help="Enable verbose output with detailed logging",
    envvar=["VERBOSE_DEBUG", "GERRIT_VERBOSE"],
)
QUIET = typer.Option(
    False,
    "--quiet",
    "-q",
    help="Suppress non-essential output",
)
MANIFEST_FILENAME = typer.Option(
    None,
    "--manifest-filename",
    help="Output manifest filename (default: refresh-manifest-TIMESTAMP.json)",
)
REMOVE_FILES = typer.Option(
    None,
    "--remove-files",
    help=(
        "Remove files matching patterns from refreshed repositories. "
        "Supports shell-style globs (e.g. '.github/**', '*.jar'), "
        "regex (prefix with 'regex:'), and comma-separated lists."
    ),
    envvar="GERRIT_REMOVE_FILES",
)
GIT_FILTER = typer.Option(
    None,
    "--git-filter",
    help=(
        "Replace tokens in git history for matching projects. "
        "Format: 'project:token1,token2;project2:token3'. "
        "Semicolons separate project entries, colons separate "
        "project patterns from comma-separated tokens."
    ),
    envvar="GERRIT_GIT_FILTER",
)
REDACT_SECRETS = typer.Option(
    False,
    "--redact-secrets/--no-redact-secrets",
    help=(
        "Scan repository content for well-known credential "
        "patterns (e.g. GitLab PATs, GitHub PATs, AWS keys) "
        "and replace them with safe placeholder values."
    ),
    envvar="GERRIT_REDACT_SECRETS",
)
