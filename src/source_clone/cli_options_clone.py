# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""Typer option definitions for the ``clone`` command.

Declaring each option as a module-level constant keeps the command signature
in :mod:`source_clone.cli_clone` readable while leaving the option metadata
(flags, help text, environment variables and validation) unchanged.
"""

from __future__ import annotations

import typer

from source_clone.ssh_identity import parse_identity_file

HOST = typer.Option(
    ...,
    "--host",
    "-h",
    help="Source hostname (Gerrit server or GitHub URL like github.com/ORG)",
    envvar="GERRIT_HOST",
)
SOURCE_TYPE = typer.Option(
    None,
    "--source-type",
    help="Source type: gerrit or github (auto-detected from host if not specified)",
    envvar="SOURCE_TYPE",
)
GITHUB_TOKEN = typer.Option(
    None,
    "--github-token",
    help=(
        "GitHub personal access token; required for GitHub sources "
        "(or set GERRIT_CLONE_TOKEN/GITHUB_TOKEN env var)"
    ),
    envvar="GERRIT_CLONE_TOKEN",
)
GITHUB_ORG = typer.Option(
    None,
    "--org",
    "--github-org",  # Backward-compatible alias
    help="GitHub organization or user name (auto-detected from host if not specified)",
    envvar="GITHUB_ORG",
)
USE_GH_CLI = typer.Option(
    False,
    "--use-gh-cli",
    help="Use GitHub CLI (gh) for cloning instead of git (preserves upstream/origin)",
    envvar="USE_GH_CLI",
)
PORT = typer.Option(
    None,
    "--port",
    "-p",
    help=(
        "Gerrit SSH port (default: 29418). HTTPS discovery and cloning use "
        "the base URL, not this port. Only used for Gerrit sources; "
        "ignored for GitHub sources."
    ),
    envvar="GERRIT_PORT",
    min=1,
    max=65535,
)
BASE_URL = typer.Option(
    None,
    "--base-url",
    help="Base URL for Gerrit API (defaults to https://HOST)",
    envvar="GERRIT_BASE_URL",
)
SSH_USER = typer.Option(
    None,
    "--ssh-user",
    "-u",
    help="SSH username for clone operations",
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
# No default: an omitted option must not override GERRIT_PATH_PREFIX,
# GERRIT_OUTPUT_DIR or a config-file path.
OUTPUT_PATH = typer.Option(
    None,
    "--output-path",
    help=(
        "Clone destination; leave empty or pass a spelling of '.' "
        "(such as './') for ./{SERVER}/ or ./github.com/{ORG}/; any other "
        "value, even '..' or an absolute path, is used exactly as given. "
        "When omitted, the first of OUTPUT_PATH, GERRIT_PATH_PREFIX, "
        "GERRIT_OUTPUT_DIR and a config-file path that is set supplies the "
        "value under the same rule (an empty variable counts as unset); "
        "with none set, the structure above is used"
    ),
    envvar="OUTPUT_PATH",
    file_okay=False,
    resolve_path=False,
)
SKIP_ARCHIVED = typer.Option(
    True,
    "--skip-archived/--include-archived",
    help="Skip archived/read-only repositories",
    envvar="GERRIT_SKIP_ARCHIVED",
)
INCLUDE_PROJECTS = typer.Option(
    None,
    "--include-projects",
    "--include-project",  # Backward-compatible alias
    help=(
        "Restrict cloning to specific project(s). Supports shell-style wildcards "
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
        "Exclude specific project(s) from cloning. Applied after include filters. "
        "Supports shell-style wildcards (*, ?, [seq]), hierarchical matching, "
        "and comma or space-separated lists. Repeat for multiple patterns."
    ),
    envvar=None,
)
SSH_DEBUG = typer.Option(
    False,
    "--ssh-debug",
    help="Enable verbose SSH (-vvv) for troubleshooting authentication (single or few projects).",
    envvar="GERRIT_SSH_DEBUG",
)
DISCOVERY_METHOD = typer.Option(
    None,
    "--discovery-method",
    help=(
        "Method for discovering projects: ssh, http (REST API), both "
        "(union of both), or github_api. Default: derived from the clone "
        "protocol (http with --https, ssh otherwise; github_api for GitHub)"
    ),
    envvar="GERRIT_DISCOVERY_METHOD",
)
ALLOW_NESTED_GIT = typer.Option(
    True,
    "--allow-nested-git/--no-allow-nested-git",
    help="Allow nested git working trees when cloning both parent and child repositories",
    envvar="GERRIT_ALLOW_NESTED_GIT",
)
NESTED_PROTECTION = typer.Option(
    True,
    "--nested-protection/--no-nested-protection",
    help="Auto-add nested child repo paths to parent .git/info/exclude",
    envvar="GERRIT_NESTED_PROTECTION",
)
MOVE_CONFLICTING = typer.Option(
    True,
    "--move-conflicting/--no-move-conflicting",
    help="Move conflicting files/directories in parent repos to [NAME].parent to allow nested cloning",
    envvar="GERRIT_MOVE_CONFLICTING",
)
THREADS = typer.Option(
    None,
    "--threads",
    "-t",
    help="Number of concurrent clone threads (default: auto)",
    envvar="GERRIT_THREADS",
    min=1,
)
DEPTH = typer.Option(
    None,
    "--depth",
    "-d",
    help="Create shallow clone with given depth",
    envvar="GERRIT_CLONE_DEPTH",
    min=1,
)
BRANCH = typer.Option(
    None,
    "--branch",
    "-b",
    help="Clone specific branch instead of default",
    envvar="GERRIT_BRANCH",
)
MIRROR = typer.Option(
    None,
    "--mirror/--no-mirror",
    help="Use git clone --mirror for complete repository metadata (all refs, tags, branches). Creates bare repository. Incompatible with --depth and --branch.",
    envvar="GERRIT_MIRROR",
)
USE_HTTPS = typer.Option(
    False,
    "--https/--ssh",
    help="Use HTTPS for cloning instead of SSH",
    envvar="GERRIT_USE_HTTPS",
)
KEEP_REMOTE_PROTOCOL = typer.Option(
    False,
    "--keep-remote-protocol",
    help="Keep original clone protocol for remote (default: always set SSH)",
    envvar="GERRIT_KEEP_REMOTE_PROTOCOL",
)
STRICT_HOST_CHECKING = typer.Option(
    True,
    "--strict-host/--accept-unknown-host",
    help="SSH strict host key checking",
    envvar="GERRIT_STRICT_HOST",
)
CLONE_TIMEOUT = typer.Option(
    600,
    "--timeout",
    "--clone-timeout",  # Backward-compatible alias
    help="Timeout per clone operation in seconds (min: 30, max: 1800)",
    envvar="GERRIT_CLONE_TIMEOUT",
    min=30,
    max=1800,
)
RETRY_ATTEMPTS = typer.Option(
    3,
    "--retry-attempts",
    help="Maximum retry attempts per repository",
    envvar="GERRIT_RETRY_ATTEMPTS",
    min=1,
    max=10,
)
RETRY_BASE_DELAY = typer.Option(
    2.0,
    "--retry-base-delay",
    help="Base delay for retry backoff in seconds",
    envvar="GERRIT_RETRY_BASE_DELAY",
    min=0.1,
)
RETRY_FACTOR = typer.Option(
    2.0,
    "--retry-factor",
    help="Exponential backoff factor for retries",
    envvar="GERRIT_RETRY_FACTOR",
    min=1.0,
)
RETRY_MAX_DELAY = typer.Option(
    30.0,
    "--retry-max-delay",
    help="Maximum retry delay in seconds",
    envvar="GERRIT_RETRY_MAX_DELAY",
    min=1.0,
)
MANIFEST_FILENAME = typer.Option(
    "clone-manifest.json",
    "--manifest-filename",
    help="Output manifest filename",
    envvar="GERRIT_MANIFEST_FILENAME",
)
CONFIG_FILE = typer.Option(
    None,
    "--config-file",
    "-c",
    help="Configuration file path (YAML or JSON)",
    exists=True,
    file_okay=True,
    dir_okay=False,
    readable=True,
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
CLEANUP = typer.Option(
    False,
    "--cleanup/--no-cleanup",
    help=(
        "Remove cloned repositories (output-path) after run completes "
        "(success or failure), keeping the run's log file"
    ),
    envvar="GERRIT_CLEANUP",
)
NO_REFRESH = typer.Option(
    False,
    "--no-refresh",
    help="Skip refreshing existing repositories (default: auto-refresh existing repos)",
    envvar="NO_REFRESH",
)
FORCE = typer.Option(
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
)
FETCH_ONLY = typer.Option(
    False,
    "--fetch-only",
    help="Only fetch changes without merging (for existing repos)",
    envvar="FETCH_ONLY",
)
SKIP_CONFLICTS = typer.Option(
    True,
    "--skip-conflicts/--no-skip-conflicts",
    help="Skip repositories with uncommitted changes during refresh",
    envvar="SKIP_CONFLICTS",
)
EXIT_ON_ERROR = typer.Option(
    False,
    "--exit-on-error",
    "--stop-on-first-error",  # Backward compatibility
    help="Exit cloning immediately when the first error occurs (for debugging)",
    envvar="GERRIT_EXIT_ON_ERROR",
)
LOG_FILE = typer.Option(
    None,
    "--log-file",
    help="Custom log file path (default: source-clone.log in current directory)",
    envvar="GERRIT_LOG_FILE",
    file_okay=True,
    dir_okay=False,
    resolve_path=True,
)
DISABLE_LOG_FILE = typer.Option(
    False,
    "--disable-log-file",
    help="Disable creation of log file",
    envvar="GERRIT_DISABLE_LOG_FILE",
)
LOG_LEVEL = typer.Option(
    "DEBUG",
    "--log-level",
    help="File logging level (DEBUG, INFO, WARNING, ERROR, CRITICAL)",
    envvar="GERRIT_LOG_LEVEL",
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
REMOVE_FILES = typer.Option(
    None,
    "--remove-files",
    help=(
        "Remove files matching patterns from cloned repositories. "
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
