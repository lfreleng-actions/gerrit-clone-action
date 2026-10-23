# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""Late-bound access to collaborators re-exported by :mod:`source_clone.cli`.

The command bodies live in dedicated modules, but ``source_clone.cli`` stays
the public facade that callers patch.  Resolving these collaborators through
the facade at call time keeps ``source_clone.cli.<name>`` the single point of
substitution, and importing the facade lazily avoids an import cycle with the
command modules it aggregates.
"""

from __future__ import annotations

from importlib import import_module
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from pathlib import Path
    from types import ModuleType

    from source_clone.models import BatchResult, Config, Project
    from source_clone.netrc import GerritCredentials


def _facade() -> ModuleType:
    """Return the :mod:`source_clone.cli` facade module."""
    return import_module("source_clone.cli")


def clone_repositories(config: Config) -> BatchResult:
    """Clone all repositories via the facade's ``clone_repositories``."""
    result: BatchResult = _facade().clone_repositories(config)
    return result


def discover_projects(config: Config) -> tuple[list[Project], dict[str, Any]]:
    """Discover projects via the facade's ``discover_projects``."""
    result: tuple[list[Project], dict[str, Any]] = _facade().discover_projects(config)
    return result


def resolve_gerrit_credentials(
    host: str,
    *,
    explicit_username: str | None = None,
    explicit_password: str | None = None,
    use_netrc: bool = True,
    netrc_file: Path | None = None,
    env_username_var: str = "GERRIT_HTTP_USER",
    env_password_var: str = "GERRIT_HTTP_PASSWORD",
    fallback_env_username_var: str | None = None,
    fallback_env_password_var: str | None = None,
) -> GerritCredentials | None:
    """Resolve credentials via the facade's ``resolve_gerrit_credentials``."""
    credentials: GerritCredentials | None = _facade().resolve_gerrit_credentials(
        host=host,
        explicit_username=explicit_username,
        explicit_password=explicit_password,
        use_netrc=use_netrc,
        netrc_file=netrc_file,
        env_username_var=env_username_var,
        env_password_var=env_password_var,
        fallback_env_username_var=fallback_env_username_var,
        fallback_env_password_var=fallback_env_password_var,
    )
    return credentials
