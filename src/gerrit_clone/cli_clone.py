# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""Definition of the ``clone`` command.

Declares the command line surface and hands the parsed arguments to
:mod:`gerrit_clone.cli_clone_run`, which performs the work.

Unlike the rest of the package this module deliberately omits
``from __future__ import annotations``: Typer resolves the command
signature's annotations at runtime to derive each parameter type.
"""

from pathlib import Path

import typer
from rich.console import Console

from gerrit_clone import cli_options_clone as opts
from gerrit_clone.cli_clone_models import CloneRequest
from gerrit_clone.cli_clone_run import handle_crash, handle_interrupt, run_clone
from gerrit_clone.cli_session import CliSession
from gerrit_clone.concurrent_utils import handle_sigint_gracefully


def clone(
    ctx: typer.Context,
    host: str = opts.HOST,
    source_type: str | None = opts.SOURCE_TYPE,
    github_token: str | None = opts.GITHUB_TOKEN,
    github_org: str | None = opts.GITHUB_ORG,
    use_gh_cli: bool = opts.USE_GH_CLI,
    port: int | None = opts.PORT,
    base_url: str | None = opts.BASE_URL,
    ssh_user: str | None = opts.SSH_USER,
    ssh_identity_file: Path | None = opts.SSH_IDENTITY_FILE,
    output_path: Path | None = opts.OUTPUT_PATH,
    skip_archived: bool = opts.SKIP_ARCHIVED,
    include_projects: list[str] | None = opts.INCLUDE_PROJECTS,
    exclude_projects: list[str] | None = opts.EXCLUDE_PROJECTS,
    ssh_debug: bool = opts.SSH_DEBUG,
    discovery_method: str | None = opts.DISCOVERY_METHOD,
    allow_nested_git: bool = opts.ALLOW_NESTED_GIT,
    nested_protection: bool = opts.NESTED_PROTECTION,
    move_conflicting: bool = opts.MOVE_CONFLICTING,
    threads: int | None = opts.THREADS,
    depth: int | None = opts.DEPTH,
    branch: str | None = opts.BRANCH,
    mirror: bool | None = opts.MIRROR,
    use_https: bool = opts.USE_HTTPS,
    keep_remote_protocol: bool = opts.KEEP_REMOTE_PROTOCOL,
    strict_host_checking: bool = opts.STRICT_HOST_CHECKING,
    clone_timeout: int = opts.CLONE_TIMEOUT,
    retry_attempts: int = opts.RETRY_ATTEMPTS,
    retry_base_delay: float = opts.RETRY_BASE_DELAY,
    retry_factor: float = opts.RETRY_FACTOR,
    retry_max_delay: float = opts.RETRY_MAX_DELAY,
    manifest_filename: str = opts.MANIFEST_FILENAME,
    config_file: Path | None = opts.CONFIG_FILE,
    verbose: bool = opts.VERBOSE,
    quiet: bool = opts.QUIET,
    cleanup: bool = opts.CLEANUP,
    no_refresh: bool = opts.NO_REFRESH,
    force: bool = opts.FORCE,
    fetch_only: bool = opts.FETCH_ONLY,
    skip_conflicts: bool = opts.SKIP_CONFLICTS,
    exit_on_error: bool = opts.EXIT_ON_ERROR,
    log_file: Path | None = opts.LOG_FILE,
    disable_log_file: bool = opts.DISABLE_LOG_FILE,
    log_level: str = opts.LOG_LEVEL,
    http_user: str | None = opts.HTTP_USER,
    http_password: str | None = opts.HTTP_PASSWORD,
    no_netrc: bool = opts.NO_NETRC,
    netrc_file: Path | None = opts.NETRC_FILE,
    netrc_optional: bool = opts.NETRC_OPTIONAL,
    remove_files: str | None = opts.REMOVE_FILES,
    git_filter: str | None = opts.GIT_FILTER,
    redact_secrets: bool = opts.REDACT_SECRETS,
) -> None:
    """Clone all repositories from a Gerrit server or GitHub organization.

    This command discovers all projects/repositories from the specified source and clones
    them in parallel while preserving the project hierarchy. For Gerrit, repositories are
    cloned over SSH by default. For GitHub, SSH is also default (HTTPS with --https).

    By default, existing repositories are refreshed (git pull) instead of skipped.
    Use --no-refresh to skip existing repositories without updating them.

    Examples:

        # Clone all active repositories from Gerrit server
        source-clone clone --host gerrit.example.org

        # Clone all repositories from GitHub organization (auto-refresh existing)
        source-clone clone --host github.com/lfreleng-actions

        # Clone without refreshing existing repos
        source-clone clone --host github.com/myorg --no-refresh

        # Force refresh existing repos (stash local changes)
        source-clone clone --host github.com/myorg --force

        # Clone GitHub org with gh CLI (preserves upstream/origin)
        source-clone clone --host github.com/myorg --use-gh-cli

        # Clone to specific directory with custom threads
        source-clone clone --host gerrit.example.org --output-path ./repos --threads 8

        # Clone with shallow depth and specific branch
        source-clone clone --host gerrit.example.org --no-mirror --depth 10 --branch main

        # Clone with explicit HTTP credentials (highest priority)
        source-clone clone --host gerrit.example.org --https --http-user myuser --http-password mypass

        # Clone with credentials from specific .netrc file
        source-clone clone --host gerrit.example.org --netrc-file ~/.netrc.gerrit

        # Clone requiring .netrc credentials (fail if not found)
        source-clone clone --host gerrit.example.org --netrc-required
    """
    # Configure graceful interrupt handling for multi-threaded operations
    handle_sigint_gracefully()

    console = Console(stderr=True)
    session = CliSession(console=console)
    request = CloneRequest(
        host=host,
        source_type=source_type,
        github_token=github_token,
        github_token_source=_github_token_source(ctx),
        github_org=github_org,
        use_gh_cli=use_gh_cli,
        port=port,
        base_url=base_url,
        ssh_user=ssh_user,
        ssh_identity_file=ssh_identity_file,
        output_path=output_path,
        skip_archived=skip_archived,
        include_projects=include_projects,
        exclude_projects=exclude_projects,
        ssh_debug=ssh_debug,
        discovery_method=discovery_method,
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
        no_refresh=no_refresh,
        force=force,
        fetch_only=fetch_only,
        skip_conflicts=skip_conflicts,
        exit_on_error=exit_on_error,
        log_file=log_file,
        disable_log_file=disable_log_file,
        log_level=log_level,
        http_user=http_user,
        http_password=http_password,
        no_netrc=no_netrc,
        netrc_file=netrc_file,
        netrc_optional=netrc_optional,
        remove_files=remove_files,
        git_filter=git_filter,
        redact_secrets=redact_secrets,
    )

    try:
        run_clone(request, session)
    except KeyboardInterrupt:
        handle_interrupt(session)
    except typer.Exit:
        # Re-raise typer.Exit exceptions without catching them as generic exceptions
        session.write_summary()
        raise
    except Exception as e:
        handle_crash(session, e, verbose=verbose)


def _github_token_source(ctx: typer.Context) -> str:
    """Name where the --github-token value came from, for logging.

    The option also reads GERRIT_CLONE_TOKEN, so its value alone cannot
    tell a typed token from one left in the environment; the parameter
    source can.
    """
    source = ctx.get_parameter_source("github_token")
    if source is not None and source.name == "ENVIRONMENT":
        return "GERRIT_CLONE_TOKEN"
    return "--github-token"
