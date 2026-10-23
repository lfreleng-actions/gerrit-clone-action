# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""The parsed ``clone`` command line.

``CloneRequest`` carries the command's arguments unchanged between the
pipeline steps in :mod:`source_clone.cli_clone_setup` and
:mod:`source_clone.cli_clone_run`.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path


@dataclass(frozen=True)
class CloneRequest:
    """Command line arguments supplied to the ``clone`` command."""

    host: str
    source_type: str | None
    github_token: str | None
    github_org: str | None
    use_gh_cli: bool
    port: int | None
    base_url: str | None
    ssh_user: str | None
    ssh_identity_file: Path | None
    output_path: Path | None
    skip_archived: bool
    include_projects: list[str] | None
    exclude_projects: list[str] | None
    ssh_debug: bool
    discovery_method: str | None
    allow_nested_git: bool
    nested_protection: bool
    move_conflicting: bool
    threads: int | None
    depth: int | None
    branch: str | None
    mirror: bool | None
    use_https: bool
    keep_remote_protocol: bool
    strict_host_checking: bool
    clone_timeout: int
    retry_attempts: int
    retry_base_delay: float
    retry_factor: float
    retry_max_delay: float
    manifest_filename: str
    config_file: Path | None
    verbose: bool
    quiet: bool
    cleanup: bool
    no_refresh: bool
    force: bool
    fetch_only: bool
    skip_conflicts: bool
    exit_on_error: bool
    log_file: Path | None
    disable_log_file: bool
    log_level: str
    http_user: str | None
    http_password: str | None
    no_netrc: bool
    netrc_file: Path | None
    netrc_optional: bool
    remove_files: str | None
    git_filter: str | None
    redact_secrets: bool
    # Where github_token came from, for logs; never the token itself
    github_token_source: str = "--github-token"
