# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""Definition of the ``mirror`` command.

Declares the command line surface and hands the parsed arguments to
:mod:`source_clone.cli_mirror_run`, which performs the work.

Unlike the rest of the package this module deliberately omits
``from __future__ import annotations``: Typer resolves the command
signature's annotations at runtime to derive each parameter type.
"""

from pathlib import Path

import typer
from rich.console import Console

from source_clone import cli_options_mirror as opts
from source_clone.cli_mirror_models import MirrorRequest
from source_clone.cli_mirror_run import (
    handle_command_error,
    handle_crash,
    handle_interrupt,
    run_mirror,
)
from source_clone.cli_session import CliSession
from source_clone.concurrent_utils import handle_sigint_gracefully
from source_clone.config import ConfigurationError
from source_clone.error_codes import DiscoveryError, ExitCode
from source_clone.github_api import GitHubAPIError


def mirror(
    server: str = opts.SERVER,
    org: str | None = opts.ORG,
    include_projects: str | None = opts.INCLUDE_PROJECTS,
    exclude_projects: str | None = opts.EXCLUDE_PROJECTS,
    output_path: Path = opts.OUTPUT_PATH,
    recreate: bool = opts.RECREATE,
    overwrite: bool = opts.OVERWRITE,
    port: int | None = opts.PORT,
    ssh_user: str | None = opts.SSH_USER,
    ssh_identity_file: Path | None = opts.SSH_IDENTITY_FILE,
    threads: int | None = opts.THREADS,
    github_token: str | None = opts.GITHUB_TOKEN,
    skip_archived: bool = opts.SKIP_ARCHIVED,
    discovery_method: str | None = opts.DISCOVERY_METHOD,
    use_https: bool = opts.USE_HTTPS,
    mirror: bool = opts.MIRROR,
    strict_host_checking: bool = opts.STRICT_HOST_CHECKING,
    manifest_filename: str = opts.MANIFEST_FILENAME,
    verbose: bool = opts.VERBOSE,
    quiet: bool = opts.QUIET,
    http_user: str | None = opts.HTTP_USER,
    http_password: str | None = opts.HTTP_PASSWORD,
    no_netrc: bool = opts.NO_NETRC,
    netrc_file: Path | None = opts.NETRC_FILE,
    netrc_optional: bool = opts.NETRC_OPTIONAL,
    set_default_branch: bool = opts.SET_DEFAULT_BRANCH,
    fix_default_branch: bool = opts.FIX_DEFAULT_BRANCH,
    remove_files: str | None = opts.REMOVE_FILES,
    git_filter: str | None = opts.GIT_FILTER,
    redact_secrets: bool = opts.REDACT_SECRETS,
) -> None:
    """Mirror repositories from a Gerrit server to GitHub.

    This command discovers projects on a Gerrit server, clones them locally,
    and mirrors them to GitHub repositories. Gerrit project hierarchies
    (e.g., ccsdk/apps) are transformed to GitHub-compatible names
    (e.g., ccsdk-apps).

    Examples:

        # Mirror all projects to a GitHub org
        source-clone mirror --host gerrit.onap.org --org myorg

        # Mirror specific projects (renamed from --projects)
        source-clone mirror --host gerrit.onap.org --org myorg \
          --include-projects "ccsdk, oom, cps"

        # Exclude a problematic repository
        source-clone mirror --host gerrit.onap.org --org myorg \
          --exclude-projects "testsuite/pythonsdk-tests"

        # Combine include and exclude with wildcards
        source-clone mirror --host gerrit.onap.org --org myorg \
          --include-projects "ccsdk, oom" --exclude-projects "*test*"

        # Recreate existing GitHub repos
        source-clone mirror --host gerrit.onap.org --org myorg \
          --recreate --overwrite

        # Use HTTPS for cloning and include archived projects
        source-clone mirror --host gerrit.onap.org --org myorg \
          --https --include-archived

        # Mirror without setting default branch on GitHub
        source-clone mirror --host gerrit.onap.org --org myorg \
          --no-set-default-branch

        # Disable the post-sync default branch repair pass
        source-clone mirror --host gerrit.onap.org --org myorg \
          --no-fix-default-branch

        # Use HTTP API for discovery (no SSH required)
        source-clone mirror --host gerrit.onap.org --org myorg \
          --discovery-method http --https

        # Mirror with explicit HTTP credentials (highest priority)
        source-clone mirror --host gerrit.onap.org --org myorg \
          --https --http-user myuser --http-password mypass

        # Mirror with credentials from specific .netrc file
        source-clone mirror --host gerrit.onap.org --org myorg \
          --https --netrc-file ~/.netrc.gerrit
    """
    # Configure graceful interrupt handling for multi-threaded operations
    handle_sigint_gracefully()

    console = Console(stderr=True)
    session = CliSession(console=console)
    request = MirrorRequest(
        server=server,
        org=org,
        include_projects=include_projects,
        exclude_projects=exclude_projects,
        output_path=output_path,
        recreate=recreate,
        overwrite=overwrite,
        port=port,
        ssh_user=ssh_user,
        ssh_identity_file=ssh_identity_file,
        threads=threads,
        github_token=github_token,
        skip_archived=skip_archived,
        discovery_method=discovery_method,
        use_https=use_https,
        mirror=mirror,
        strict_host_checking=strict_host_checking,
        manifest_filename=manifest_filename,
        verbose=verbose,
        quiet=quiet,
        http_user=http_user,
        http_password=http_password,
        no_netrc=no_netrc,
        netrc_file=netrc_file,
        netrc_optional=netrc_optional,
        set_default_branch=set_default_branch,
        fix_default_branch=fix_default_branch,
        remove_files=remove_files,
        git_filter=git_filter,
        redact_secrets=redact_secrets,
    )

    try:
        run_mirror(request, session)
    except GitHubAPIError as e:
        handle_command_error(
            session,
            e,
            label="GitHub API Error",
            log_format="GitHub API error: %s",
            exit_code=ExitCode.GENERAL_ERROR,
            verbose=verbose,
        )
    except DiscoveryError as e:
        handle_command_error(
            session,
            e,
            label="Discovery Error",
            log_format="Discovery error: %s",
            exit_code=ExitCode.DISCOVERY_ERROR,
            verbose=verbose,
        )
    except ConfigurationError as e:
        handle_command_error(
            session,
            e,
            label="Configuration Error",
            log_format="Configuration error: %s",
            exit_code=ExitCode.CONFIGURATION_ERROR,
            verbose=verbose,
        )
    except KeyboardInterrupt:
        handle_interrupt(session)
    except typer.Exit:
        # Re-raise typer.Exit without catching it
        session.write_summary()
        raise
    except Exception as e:
        handle_crash(session, e, verbose=verbose)
