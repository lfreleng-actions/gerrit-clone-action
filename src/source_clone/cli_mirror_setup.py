# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""Preparation steps run before the ``mirror`` command starts mirroring.

Covers option validation, HTTP credential resolution, logging setup, GitHub
authentication, target organization and filter resolution, and building the
Gerrit-side configuration.
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING

import typer
from rich.panel import Panel
from rich.text import Text

from source_clone import cli_hooks
from source_clone.error_codes import ExitCode
from source_clone.file_logging import (
    cli_args_to_dict,
    file_logging_active,
    get_default_log_path,
    init_logging,
)
from source_clone.github_api import GitHubAPI, GitHubAuthError, get_default_org_or_user
from source_clone.models import (
    Config,
    DiscoveryMethod,
    RetryPolicy,
    normalize_project_list,
)
from source_clone.netrc import NetrcParseError

if TYPE_CHECKING:
    from rich.console import Console

    from source_clone.cli_mirror_models import MirrorRequest
    from source_clone.cli_session import CliSession


def validate_request(request: MirrorRequest, console: Console) -> None:
    """Reject mutually exclusive option combinations."""
    if request.verbose and request.quiet:
        console.print("[red]Error:[/red] --verbose and --quiet cannot be used together")
        raise typer.Exit(ExitCode.CONFIGURATION_ERROR)


def apply_http_credentials(request: MirrorRequest, console: Console) -> None:
    """Resolve HTTP credentials and export them for downstream code.

    Priority: 1. CLI arguments (--http-user/--http-password)
              2. .netrc file
              3. Environment variables (GERRIT_HTTP_USER/GERRIT_HTTP_PASSWORD)
              4. Fallback environment variables (GERRIT_USERNAME/GERRIT_PASSWORD)
    """
    try:
        resolved_creds = cli_hooks.resolve_gerrit_credentials(
            host=request.server,
            explicit_username=request.http_user,
            explicit_password=request.http_password,
            use_netrc=not request.no_netrc,
            netrc_file=request.netrc_file,
            env_username_var="GERRIT_HTTP_USER",
            env_password_var="GERRIT_HTTP_PASSWORD",
            fallback_env_username_var="GERRIT_USERNAME",
            fallback_env_password_var="GERRIT_PASSWORD",
        )
        if resolved_creds:
            # Set environment variables for downstream code to use
            os.environ["GERRIT_HTTP_USER"] = resolved_creds.username
            os.environ["GERRIT_HTTP_PASSWORD"] = resolved_creds.password
            if not request.quiet:
                console.print(
                    f"🔐 Loaded HTTP credentials from {resolved_creds.source_detail}"
                )
        elif not request.no_netrc and not request.netrc_optional:
            # No credentials found and netrc was required
            console.print(
                "[red]Error:[/red] No credentials found and --netrc-required set"
            )
            raise typer.Exit(ExitCode.CONFIGURATION_ERROR)
    except FileNotFoundError:
        if not request.netrc_optional:
            console.print(
                "[red]Error:[/red] No .netrc file found and --netrc-required set"
            )
            raise typer.Exit(ExitCode.CONFIGURATION_ERROR) from None
    except NetrcParseError as e:
        console.print(f"[red]Error:[/red] Failed to parse .netrc file: {e}")
        raise typer.Exit(ExitCode.CONFIGURATION_ERROR) from e


def start_logging(request: MirrorRequest, session: CliSession) -> None:
    """Initialize logging so that logger.info / logger.warning
    messages from downstream modules (github_api, mirror_manager)
    reach the console.  Without this, only WARNING+ would be
    visible via Python's lastResort handler.
    """
    log_file_path = get_default_log_path(request.server, request.output_path)
    file_logger, error_collector = init_logging(
        log_file=log_file_path,
        disable_file=False,
        log_level="DEBUG",
        console_level="DEBUG" if request.verbose else "WARNING",
        quiet=request.quiet,
        verbose=request.verbose,
        cli_args=cli_args_to_dict(
            server=request.server,
            org=request.org,
            include_projects=request.include_projects,
            exclude_projects=request.exclude_projects,
            output_path=str(request.output_path),
            recreate=request.recreate,
            overwrite=request.overwrite,
            set_default_branch=request.set_default_branch,
            fix_default_branch=request.fix_default_branch,
            verbose=request.verbose,
            quiet=request.quiet,
        ),
        host=request.server,
        path=request.output_path,
        command="mirror",
    )
    session.log_file_path = log_file_path if file_logging_active() else None
    session.file_logger = file_logger
    session.error_collector = error_collector


def authenticate(request: MirrorRequest, console: Console) -> GitHubAPI:
    """Create an authenticated GitHub API client."""
    try:
        return GitHubAPI(token=request.github_token)
    except GitHubAuthError as e:
        console.print(f"[red]Error:[/red] {e}")
        raise typer.Exit(ExitCode.CONFIGURATION_ERROR) from e


def resolve_org(request: MirrorRequest, console: Console, github_api: GitHubAPI) -> str:
    """Determine target org/user."""
    org = request.org
    if org is None:
        if not request.quiet:
            console.print(
                "ℹ\ufe0f No organization specified, "  # noqa: RUF001
                "using default from GitHub token..."
            )
        org, is_org = get_default_org_or_user(github_api)
        if not request.quiet:
            org_type = "organization" if is_org else "user account"
            console.print(f"✓ Using {org_type}: [cyan]{org}[/cyan]")
    elif not request.quiet:
        console.print(f"✓ Using specified organization: [cyan]{org}[/cyan]")
    return org


def resolve_project_filters(
    request: MirrorRequest, console: Console
) -> tuple[list[str], list[str]]:
    """Parse the include and exclude project filters."""
    # Parse project filters (include)
    project_filters = normalize_project_list(
        [request.include_projects] if request.include_projects else []
    )
    if project_filters and not request.quiet:
        console.print(f"📋 Include filters: [cyan]{', '.join(project_filters)}[/cyan]")

    # Parse project filters (exclude)
    exclude_filters = normalize_project_list(
        [request.exclude_projects] if request.exclude_projects else []
    )
    if exclude_filters and not request.quiet:
        console.print(f"🚫 Exclude filters: [cyan]{', '.join(exclude_filters)}[/cyan]")

    return project_filters, exclude_filters


def resolve_discovery_method(
    request: MirrorRequest, console: Console
) -> DiscoveryMethod | None:
    """Validate discovery method (None means "derive in Config from the
    clone protocol"). Only validate/convert when explicitly set.
    """
    discovery_method = request.discovery_method
    if not (discovery_method and discovery_method.strip()):
        return None

    try:
        discovery_enum = DiscoveryMethod(discovery_method.strip().lower())
    except ValueError:
        _print_invalid_discovery_method(console, discovery_method)
        raise typer.Exit(ExitCode.CONFIGURATION_ERROR) from None

    # Mirror targets Gerrit only; github_api is a valid enum value but
    # not a valid Gerrit discovery method. Reject it here so the user
    # gets a clear configuration error rather than an unexpected error
    # raised later by Config.
    if discovery_enum == DiscoveryMethod.GITHUB_API:
        _print_invalid_discovery_method(console, discovery_method)
        raise typer.Exit(ExitCode.CONFIGURATION_ERROR) from None

    return discovery_enum


def _print_invalid_discovery_method(console: Console, discovery_method: str) -> None:
    """Report an unusable ``--discovery-method`` value."""
    console.print(
        Panel(
            Text(
                f"Invalid discovery method '{discovery_method}'\nMust be one of: ssh, http, both",
                style="bold red",
            ),
            title="Configuration Error",
            border_style="red",
        )
    )


def build_config(
    request: MirrorRequest, discovery_enum: DiscoveryMethod | None
) -> Config:
    """Build the Gerrit-side configuration used for discovery and cloning."""
    return Config(
        host=request.server,
        port=request.port or 29418,
        ssh_user=request.ssh_user,
        ssh_identity_file=request.ssh_identity_file,
        path=request.output_path,
        threads=request.threads,
        skip_archived=request.skip_archived,
        discovery_method=discovery_enum,
        strict_host_checking=request.strict_host_checking,
        use_https=request.use_https,
        mirror=request.mirror,
        retry_policy=RetryPolicy(),
    )
