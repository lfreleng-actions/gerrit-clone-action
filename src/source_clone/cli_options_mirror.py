# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""Typer option definitions for the ``mirror`` command.

Declaring each option as a module-level constant keeps the command signature
in :mod:`source_clone.cli_mirror` readable while leaving the option metadata
(flags, help text, environment variables and validation) unchanged.
"""

from __future__ import annotations

from pathlib import Path

import typer

from source_clone.ssh_identity import parse_identity_file

SERVER = typer.Option(
    ...,
    "--host",
    "--server",  # Backward-compatible alias
    help="Gerrit server hostname",
    envvar="GERRIT_HOST",
)
ORG = typer.Option(
    None,
    "--org",
    help=(
        "Target GitHub organization for mirrored content "
        "(if not specified, user's primary org/account will be used)"
    ),
    envvar="GITHUB_ORG",
)
INCLUDE_PROJECTS = typer.Option(
    None,
    "--include-projects",
    "--include-project",  # Alias matching clone and refresh
    "--projects",  # Backward-compatible alias
    help=(
        "Include only matching projects. Supports shell-style wildcards "
        "(*, ?, [seq]), hierarchical matching (e.g. 'ccsdk' includes ccsdk/apps), "
        "and comma or space-separated lists."
    ),
    envvar="GERRIT_PROJECTS",
)
EXCLUDE_PROJECTS = typer.Option(
    None,
    "--exclude-projects",
    "--exclude-project",  # Alias matching clone and refresh
    help=(
        "Exclude matching projects. Applied after include filters. "
        "Supports shell-style wildcards (*, ?, [seq]), hierarchical matching, "
        "and comma or space-separated lists."
    ),
    envvar="GERRIT_EXCLUDE_PROJECTS",
)
OUTPUT_PATH = typer.Option(
    Path("/tmp/gerrit-mirror"),
    "--output-path",
    help="Local filesystem folder/path for cloned projects",
    envvar="MIRROR_OUTPUT_PATH",
    file_okay=False,
    resolve_path=False,
)
RECREATE = typer.Option(
    False,
    "--recreate",
    help="Delete and recreate any pre-existing remote GitHub repositories",
    envvar="GERRIT_MIRROR_RECREATE",
)
OVERWRITE = typer.Option(
    False,
    "--overwrite",
    help="Overwrite local Git repositories at the target filesystem path",
    envvar="GERRIT_MIRROR_OVERWRITE",
)
PORT = typer.Option(
    None,
    "--port",
    "-p",
    help="Gerrit port (default: 29418 for SSH)",
    envvar="GERRIT_PORT",
    min=1,
    max=65535,
)
SSH_USER = typer.Option(
    None,
    "--ssh-user",
    "-u",
    help="SSH username for Gerrit clone operations",
    envvar="GERRIT_SSH_USER",
)
SSH_IDENTITY_FILE = typer.Option(
    None,
    "--ssh-identity-file",
    "--ssh-private-key",  # Backward-compatible alias (prefer --ssh-identity-file)
    "-i",
    help="SSH identity (private key) file path for authentication",
    envvar="GERRIT_SSH_PRIVATE_KEY",
    parser=parse_identity_file,
)
THREADS = typer.Option(
    None,
    "--threads",
    "-t",
    help="Number of concurrent operations (default: auto)",
    envvar="GERRIT_THREADS",
    min=1,
)
GITHUB_TOKEN = typer.Option(
    None,
    "--github-token",
    help=("GitHub personal access token (default: GITHUB_TOKEN environment variable)"),
    envvar="GITHUB_TOKEN",
)
SKIP_ARCHIVED = typer.Option(
    True,
    "--skip-archived/--include-archived",
    help="Skip archived/read-only repositories",
    envvar="GERRIT_SKIP_ARCHIVED",
)
DISCOVERY_METHOD = typer.Option(
    None,
    "--discovery-method",
    help=(
        "Method for discovering projects: ssh, http (REST API only), or "
        "both (union, SSH metadata preferred). Default: derived from the "
        "clone protocol (http with --https, ssh otherwise)"
    ),
    envvar="GERRIT_DISCOVERY_METHOD",
)
USE_HTTPS = typer.Option(
    False,
    "--https/--ssh",
    help="Use HTTPS for cloning instead of SSH",
    envvar="GERRIT_USE_HTTPS",
)
MIRROR = typer.Option(
    True,
    "--mirror/--no-mirror",
    help="Use git clone --mirror for complete repository metadata (all refs, tags, branches). Creates bare repository.",
    envvar="GERRIT_MIRROR",
)
STRICT_HOST_CHECKING = typer.Option(
    True,
    "--strict-host/--accept-unknown-host",
    help="SSH strict host key checking",
    envvar="GERRIT_STRICT_HOST",
)
MANIFEST_FILENAME = typer.Option(
    "mirror-manifest.json",
    "--manifest-filename",
    help="Output manifest filename",
    envvar="GERRIT_MIRROR_MANIFEST",
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
HTTP_USER = typer.Option(
    None,
    "--http-user",
    help="HTTP username for Gerrit authentication (highest priority)",
    envvar="GERRIT_HTTP_USER",
)
HTTP_PASSWORD = typer.Option(
    None,
    "--http-password",
    help="HTTP password for Gerrit authentication (highest priority)",
    envvar="GERRIT_HTTP_PASSWORD",
)
NO_NETRC = typer.Option(
    False,
    "--no-netrc",
    help="Disable .netrc credential lookup for HTTP authentication",
    envvar="GERRIT_NO_NETRC",
)
NETRC_FILE = typer.Option(
    None,
    "--netrc-file",
    help="Explicit path to .netrc file for HTTP credentials",
    envvar="GERRIT_NETRC_FILE",
    exists=True,
    file_okay=True,
    dir_okay=False,
    readable=True,
    resolve_path=True,
)
NETRC_OPTIONAL = typer.Option(
    True,
    "--netrc-optional/--netrc-required",
    help="Whether to fail if .netrc file is not found (default: optional)",
    envvar="GERRIT_NETRC_OPTIONAL",
)
SET_DEFAULT_BRANCH = typer.Option(
    True,
    "--set-default-branch/--no-set-default-branch",
    help=(
        "After pushing to GitHub, set the default branch to match the "
        "HEAD symbolic ref from the Gerrit clone (default: enabled)"
    ),
    envvar="GERRIT_SET_DEFAULT_BRANCH",
)
FIX_DEFAULT_BRANCH = typer.Option(
    True,
    "--fix-default-branch/--no-fix-default-branch",
    help=(
        "After syncing, repair any existing GitHub repositories that "
        "have no default branch configured (default: enabled). "
        "Gerrit parent projects (HEAD → refs/meta/config, no code "
        "branches) are identified and logged at INFO level rather "
        "than flagged as errors. Real repositories whose previous "
        "push failed are fixed by selecting the best candidate branch."
    ),
    envvar="GERRIT_FIX_DEFAULT_BRANCH",
)
REMOVE_FILES = typer.Option(
    None,
    "--remove-files",
    help=(
        "Remove files matching patterns from cloned repositories "
        "before pushing to GitHub. Supports shell-style globs "
        "(e.g. '.github/dependabot.yml', '.github/**'), "
        "regex (prefix with 'regex:'), and comma-separated lists."
    ),
    envvar="GERRIT_REMOVE_FILES",
)
GIT_FILTER = typer.Option(
    None,
    "--git-filter",
    help=(
        "Replace tokens in git history for matching projects. "
        "Format: 'project_pattern:token1,token2;project2:token3'. "
        "Semicolons separate project entries. Colons separate "
        "project patterns from comma-separated tokens. "
        "Supports wildcards in project patterns."
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
