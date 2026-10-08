# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""Preparation steps run before the ``clone`` command starts cloning.

Covers source detection, logging initialisation, HTTP credential resolution,
discovery-method parsing, configuration loading and the startup banner.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import TYPE_CHECKING, Any

import typer
from rich.console import Console
from rich.panel import Panel
from rich.text import Text

from gerrit_clone import cli_hooks
from gerrit_clone.cli_app import format_version_string
from gerrit_clone.config import ConfigurationError, load_config
from gerrit_clone.content_intent import IntentError
from gerrit_clone.content_intent_resolve import resolve_filters
from gerrit_clone.content_spec import ContentFilterSpec
from gerrit_clone.error_codes import ExitCode
from gerrit_clone.file_logging import (
    cli_args_to_dict,
    get_default_log_path,
    init_logging,
)
from gerrit_clone.github_discovery import detect_github_source, parse_github_url
from gerrit_clone.models import DiscoveryMethod, SourceType
from gerrit_clone.netrc import NetrcParseError

if TYPE_CHECKING:
    import logging

    from gerrit_clone.cli_clone_models import CloneRequest
    from gerrit_clone.cli_session import CliSession
    from gerrit_clone.file_logging import ErrorCollector
    from gerrit_clone.models import Config


def resolve_source(
    request: CloneRequest, console: Console
) -> tuple[SourceType, str | None]:
    """Determine the source type and GitHub organization for the request.

    Returns:
        Tuple of (source type, GitHub organization or None)
    """
    detected_source_type = SourceType.GERRIT
    detected_github_org = request.github_org

    if request.source_type:
        # Use explicitly specified source type
        try:
            detected_source_type = SourceType(request.source_type.lower())
        except ValueError:
            console.print(
                f"[red]Error:[/red] Invalid source type '{request.source_type}'. Must be 'gerrit' or 'github'"
            )
            raise typer.Exit(ExitCode.CONFIGURATION_ERROR) from None
    elif detect_github_source(request.host):
        # Auto-detect GitHub from host
        detected_source_type = SourceType.GITHUB
        # Extract org from URL if present
        _, org = parse_github_url(request.host)
        if org:
            detected_github_org = org
        console.print(
            f"[cyan]ℹ[/cyan] Auto-detected GitHub source from host: {request.host}"  # noqa: RUF001
        )

    if detected_source_type == SourceType.GITHUB and not detected_github_org:
        console.print(
            "[red]Error:[/red] GitHub organization/user not specified. "
            "Use --github-org or include in --host (e.g., github.com/ORG)"
        )
        raise typer.Exit(ExitCode.CONFIGURATION_ERROR)

    if request.verbose and request.quiet:
        console.print("[red]Error:[/red] --verbose and --quiet cannot be used together")
        raise typer.Exit(ExitCode.CONFIGURATION_ERROR)

    return detected_source_type, detected_github_org


def build_cli_args(
    request: CloneRequest,
    source_type: SourceType,
    github_org: str | None,
) -> dict[str, Any]:
    """Prepare CLI arguments for logging."""
    return cli_args_to_dict(
        host=request.host,
        source_type=source_type.value,
        github_token="<redacted>" if request.github_token else None,
        github_org=github_org,
        use_gh_cli=request.use_gh_cli,
        no_refresh=request.no_refresh,
        force=request.force,
        fetch_only=request.fetch_only,
        skip_conflicts=request.skip_conflicts,
        port=request.port,
        base_url=request.base_url,
        ssh_user=request.ssh_user,
        ssh_identity_file=request.ssh_identity_file,
        path=request.output_path,
        skip_archived=request.skip_archived,
        include_projects=request.include_projects,
        exclude_projects=request.exclude_projects,
        ssh_debug=request.ssh_debug,
        allow_nested_git=request.allow_nested_git,
        nested_protection=request.nested_protection,
        move_conflicting=request.move_conflicting,
        threads=request.threads,
        depth=request.depth,
        branch=request.branch,
        mirror=request.mirror,
        use_https=request.use_https,
        keep_remote_protocol=request.keep_remote_protocol,
        strict_host_checking=request.strict_host_checking,
        clone_timeout=request.clone_timeout,
        retry_attempts=request.retry_attempts,
        retry_base_delay=request.retry_base_delay,
        retry_factor=request.retry_factor,
        retry_max_delay=request.retry_max_delay,
        manifest_filename=request.manifest_filename,
        config_file=request.config_file,
        verbose=request.verbose,
        quiet=request.quiet,
        cleanup=request.cleanup,
        exit_on_error=request.exit_on_error,
        log_file=request.log_file,
        disable_log_file=request.disable_log_file,
        log_level=request.log_level,
        no_netrc=request.no_netrc,
        netrc_file=str(request.netrc_file) if request.netrc_file else None,
        netrc_optional=request.netrc_optional,
        remove_files=request.remove_files,
        git_filter=request.git_filter,
        redact_secrets=request.redact_secrets,
    )


def start_logging(
    request: CloneRequest, cli_args: dict[str, Any]
) -> tuple[logging.Logger, ErrorCollector, Path]:
    """Set up the unified logging system (file + console).

    Returns:
        Tuple of (file logger, error collector, log file path)
    """
    file_logger, error_collector = init_logging(
        log_file=request.log_file,
        disable_file=request.disable_log_file,
        log_level=request.log_level,
        console_level="DEBUG" if request.verbose else "WARNING",
        quiet=request.quiet,
        verbose=request.verbose,
        cli_args=cli_args,
        host=request.host,
        path=Path(request.output_path) if request.output_path else None,
        command="clone",
    )

    # Set log_file_path for error handling compatibility
    log_file_path = (
        request.log_file
        if request.log_file
        else get_default_log_path(
            request.host, Path(request.output_path) if request.output_path else None
        )
    )
    return file_logger, error_collector, log_file_path


def apply_http_credentials(
    request: CloneRequest, console: Console, file_logger: logging.Logger
) -> None:
    """Resolve HTTP credentials and export them for downstream code.

    Priority: 1. CLI arguments (--http-user/--http-password)
              2. .netrc file
              3. Environment variables (GERRIT_HTTP_USER/GERRIT_HTTP_PASSWORD)
              4. Fallback environment variables (GERRIT_USERNAME/GERRIT_PASSWORD)
    """
    try:
        resolved_creds = cli_hooks.resolve_gerrit_credentials(
            host=request.host,
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
            file_logger.debug(
                "Loaded HTTP credentials for %s from %s",
                request.host,
                resolved_creds.source_detail,
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


def resolve_discovery_method(
    request: CloneRequest, console: Console, source_type: SourceType
) -> DiscoveryMethod | None:
    """Parse the requested discovery method.

    ``None`` means "derive in Config from source type and clone protocol", so
    only validate/convert when explicitly set.
    """
    discovery_method = request.discovery_method
    if not (discovery_method and discovery_method.strip()):
        return None

    try:
        discovery_method_enum = DiscoveryMethod(discovery_method.strip().lower())
    except ValueError:
        console.print(
            Panel(
                Text(
                    f"Invalid discovery method '{discovery_method}'\nMust be one of: ssh, http, both, github_api",
                    style="bold red",
                ),
                title="Configuration Error",
                border_style="red",
            )
        )
        raise typer.Exit(ExitCode.CONFIGURATION_ERROR) from None

    # Auto-adjust explicit discovery method for GitHub
    if source_type == SourceType.GITHUB and discovery_method_enum not in [
        DiscoveryMethod.GITHUB_API,
        DiscoveryMethod.HTTP,
    ]:
        discovery_method_enum = DiscoveryMethod.GITHUB_API
        if not request.quiet:
            console.print(
                "[cyan]ℹ[/cyan] Using GitHub API discovery for GitHub source"  # noqa: RUF001
            )

    return discovery_method_enum


def build_config(
    request: CloneRequest,
    session: CliSession,
    source_type: SourceType,
    github_org: str | None,
    discovery_method: DiscoveryMethod | None,
) -> Config:
    """Load the effective configuration, reporting configuration errors."""
    try:
        config = load_config(
            host=request.host,
            port=request.port,  # Leave as None for GitHub, will default to 29418 for Gerrit
            base_url=request.base_url,
            ssh_user=request.ssh_user,
            ssh_identity_file=request.ssh_identity_file,
            path=request.output_path,
            skip_archived=request.skip_archived,
            allow_nested_git=request.allow_nested_git,
            nested_protection=request.nested_protection,
            move_conflicting=request.move_conflicting,
            threads=request.threads,
            depth=request.depth,
            branch=request.branch,
            mirror=request.mirror,
            use_https=request.use_https,
            keep_remote_protocol=request.keep_remote_protocol,
            strict_host_checking=request.strict_host_checking,
            clone_timeout=request.clone_timeout,
            retry_attempts=request.retry_attempts,
            retry_base_delay=request.retry_base_delay,
            retry_factor=request.retry_factor,
            retry_max_delay=request.retry_max_delay,
            manifest_filename=request.manifest_filename,
            config_file=request.config_file,
            verbose=request.verbose,
            quiet=request.quiet,
            include_projects=request.include_projects,
            exclude_projects=request.exclude_projects,
            ssh_debug=request.ssh_debug,
            exit_on_error=request.exit_on_error,
            discovery_method=discovery_method,
            source_type=source_type,
            github_token=request.github_token,
            github_org=github_org,
            use_gh_cli=request.use_gh_cli,
            auto_refresh=not request.no_refresh,
            force_refresh=request.force,
            fetch_only=request.fetch_only,
            skip_conflicts=request.skip_conflicts,
        )
    except ConfigurationError as e:
        _report_configuration_error(session, e)
        raise typer.Exit(ExitCode.CONFIGURATION_ERROR) from e
    options = ContentFilterSpec.from_options(
        request.remove_files, request.git_filter, request.redact_secrets, config.path
    )
    try:
        # Written down before anything is cloned or refreshed.
        config.content_filters = resolve_filters(
            config.path, options, persist=True, command="clone"
        )
    except IntentError as e:
        _report_configuration_error(session, e)
        raise typer.Exit(ExitCode.CONFIGURATION_ERROR) from e
    return config


def _report_configuration_error(session: CliSession, error: Exception) -> None:
    """Log and display a configuration error before aborting."""
    if session.file_logger:
        session.file_logger.error("Configuration error: %s", str(error))
    session.write_summary()
    console = Console()
    console.print(
        Panel(
            Text(str(error), style="bold red"),
            title="Configuration Error",
            border_style="red",
        )
    )


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
        title = "[bold]Gerrit Clone Configuration[/bold]"

    panel = Panel(
        summary_text,
        title=title,
        border_style="blue",
        padding=(1, 2),
    )

    console.print(panel)
