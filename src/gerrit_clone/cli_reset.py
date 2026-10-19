# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""Definition of the ``reset`` command.

Declares the command line surface and hands the parsed arguments to
:mod:`gerrit_clone.cli_reset_run`, which performs the work.

Unlike the rest of the package this module deliberately omits
``from __future__ import annotations``: Typer resolves the command
signature's annotations at runtime to derive each parameter type.
"""

from pathlib import Path

import typer
from rich.console import Console

from gerrit_clone import cli_options_reset as opts
from gerrit_clone.cli_reset_run import (
    ResetRequest,
    handle_command_error,
    handle_crash,
    handle_interrupt,
    run_reset,
)
from gerrit_clone.cli_session import CliSession
from gerrit_clone.error_codes import ExitCode
from gerrit_clone.github_api import GitHubAPIError, GitHubAuthError


def reset(
    org: str = opts.ORG,
    path: Path = opts.PATH,
    compare: bool = opts.COMPARE,
    github_token: str | None = opts.GITHUB_TOKEN,
    no_confirm: bool = opts.NO_CONFIRM,
    include_automation_prs: bool = opts.INCLUDE_AUTOMATION_PRS,
    verbose: bool = opts.VERBOSE,
    quiet: bool = opts.QUIET,
) -> None:
    """
    Remove all repositories from a GitHub organization.

    This command:

    1. Lists all repositories in the organization with PR/issue counts
       (by default, excludes automation PRs from dependabot, pre-commit.ci, etc.)

    2. Optionally compares with local Gerrit clone (--compare flag)

    3. Prompts for confirmation with unique hash (unless --no-confirm)

    4. Deletes all repositories permanently

    [red]WARNING: This operation is DESTRUCTIVE and IRREVERSIBLE![/red]

    Examples:

        # List repos and prompt for confirmation (excludes automation PRs)
        source-clone reset --org my-test-org

        # Include automation PRs in counts
        source-clone reset --org my-test-org --include-automation-prs

        # Compare with local clone before deletion
        source-clone reset --org my-test-org --output-path /tmp/gerrit-mirror --compare

        # Delete immediately without prompt (DANGEROUS!)
        source-clone reset --org my-test-org --no-confirm
    """
    console = Console(stderr=True)
    session = CliSession(console=console)
    request = ResetRequest(
        org=org,
        path=path,
        compare=compare,
        github_token=github_token,
        no_confirm=no_confirm,
        include_automation_prs=include_automation_prs,
        verbose=verbose,
        quiet=quiet,
    )

    try:
        run_reset(request, session)
    except GitHubAuthError as e:
        handle_command_error(
            session,
            e,
            label="GitHub authentication error",
            log_format="GitHub authentication error: %s",
            exit_code=ExitCode.CONFIGURATION_ERROR,
        )
    except GitHubAPIError as e:
        handle_command_error(
            session,
            e,
            label="GitHub API error",
            log_format="GitHub API error: %s",
            exit_code=ExitCode.GENERAL_ERROR,
        )
    except KeyboardInterrupt:
        handle_interrupt(session)
    except typer.Exit:
        # Re-raise typer.Exit exceptions without catching them as generic exceptions
        session.write_summary()
        raise
    except Exception as e:
        handle_crash(session, e, verbose=verbose)
