# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""The parsed ``mirror`` command line.

``MirrorRequest`` carries the command's arguments unchanged between the
pipeline steps in :mod:`source_clone.cli_mirror_setup` and
:mod:`source_clone.cli_mirror_run`.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path


@dataclass(frozen=True)
class MirrorRequest:
    """Command line arguments supplied to the ``mirror`` command."""

    server: str
    org: str | None
    include_projects: str | None
    exclude_projects: str | None
    output_path: Path
    recreate: bool
    overwrite: bool
    port: int | None
    ssh_user: str | None
    ssh_identity_file: Path | None
    threads: int | None
    github_token: str | None
    skip_archived: bool
    discovery_method: str | None
    use_https: bool
    mirror: bool
    strict_host_checking: bool
    manifest_filename: str
    verbose: bool
    quiet: bool
    http_user: str | None
    http_password: str | None
    no_netrc: bool
    netrc_file: Path | None
    netrc_optional: bool
    set_default_branch: bool
    fix_default_branch: bool
    remove_files: str | None
    git_filter: str | None
    redact_secrets: bool
