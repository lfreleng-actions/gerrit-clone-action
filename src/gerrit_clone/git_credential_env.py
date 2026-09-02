# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""Git credentials supplied through the process environment.

A token embedded in a URL that is then passed to ``git`` becomes part of
that process's arguments, readable through ``ps`` or
``/proc/<pid>/cmdline`` by anything else on the host for as long as the
command runs.  Nothing can redact ``argv`` after the fact, and a bulk
clone creates that exposure once per repository.

``GIT_CONFIG_COUNT`` / ``GIT_CONFIG_KEY_<n>`` / ``GIT_CONFIG_VALUE_<n>``
inject config into git through the environment instead, which is not
exposed in the process listing.  Both the clone path
(:mod:`gerrit_clone.github_clone_env`) and the mirror push path
(:mod:`gerrit_clone.mirror_push`) authenticate this way, and share the
implementation here.
"""

from __future__ import annotations

import base64
from typing import TYPE_CHECKING
from urllib.parse import urlparse

if TYPE_CHECKING:
    from collections.abc import Iterable

#: Git release that introduced the ``GIT_CONFIG_*`` environment
#: variables used here (March 2021).  Recorded in the README under
#: requirements.
MINIMUM_GIT_VERSION = "2.31"

#: libcurl release that stopped forwarding a custom ``Authorization``
#: header to a different host across a redirect (CVE-2018-1000007,
#: January 2018).  Git hands ``http.extraheader`` to libcurl as exactly
#: such a header, so the origin scoping below chooses where the header
#: is *sent first* and libcurl decides where it may follow.  Recorded in
#: the README alongside the Git floor.
MINIMUM_LIBCURL_VERSION = "7.58"


def token_auth_config(github_token: str, url: str) -> tuple[str, str]:
    """Return the git config entry that authenticates *url* as *github_token*.

    Uses an ``Authorization`` header rather than a credential helper so
    the value stays inside the environment.

    The key is **scoped to the URL's origin** --
    ``http.<scheme>://<host>.extraheader`` -- rather than the global
    ``http.extraheader``.  A global header is attached to whatever URL
    git is asked for, so an externally supplied clone URL pointing
    elsewhere would hand the token to that host.

    That scoping decides which URL the header is attached to, and no
    more.  Once the request is in flight the header is an ordinary
    custom libcurl header, and a libcurl older than
    :data:`MINIMUM_LIBCURL_VERSION` forwards a custom ``Authorization``
    across a redirect to a different host.  Redirects are not disabled
    to close that: git follows the initial one by default, and GitHub
    relies on it for renamed and transferred repositories, so refusing
    would break cloning them for a gap that a supported libcurl does
    not have.

    Args:
        github_token: GitHub token to authenticate with.
        url: URL the credential is for.

    Returns:
        A ``(key, value)`` git config pair.
    """
    credentials = base64.b64encode(f"x-access-token:{github_token}".encode()).decode()
    parsed = urlparse(url)
    origin = f"{parsed.scheme}://{parsed.netloc}"
    return (f"http.{origin}.extraheader", f"AUTHORIZATION: basic {credentials}")


def git_config_env(entries: Iterable[tuple[str, str]]) -> dict[str, str]:
    """Render git config *entries* as ``GIT_CONFIG_*`` variables.

    Args:
        entries: ``(key, value)`` git config pairs, in order.

    Returns:
        Environment overrides, or an empty dict when there is nothing to
        set.  ``GIT_CONFIG_COUNT`` is only emitted alongside at least one
        entry, so merging the result into an existing environment cannot
        leave git looking for variables that are not there.
    """
    pairs = list(entries)
    if not pairs:
        return {}

    env = {"GIT_CONFIG_COUNT": str(len(pairs))}
    for index, (key, value) in enumerate(pairs):
        env[f"GIT_CONFIG_KEY_{index}"] = key
        env[f"GIT_CONFIG_VALUE_{index}"] = value
    return env


def build_token_auth_env(
    github_token: str | None,
    url: str | None = None,
    extra_config: Iterable[tuple[str, str]] = (),
) -> dict[str, str]:
    """Build the git environment that authenticates with *github_token*.

    Args:
        github_token: Token to authenticate with, if one is configured.
        url: URL the credential is for.  The credential is scoped to its
            origin, so this is required whenever a token is supplied.
        extra_config: Further git config entries to set in the same
            ``GIT_CONFIG_*`` block.  They must be passed here rather
            than written separately, because a second block would
            overwrite ``GIT_CONFIG_COUNT``.

    Returns:
        Environment overrides to merge into the git subprocess
        environment.

    Raises:
        ValueError: If a token is supplied without the URL it is for.
    """
    entries = list(extra_config)
    if github_token:
        if url is None:
            raise ValueError("A token must be scoped to the URL it authenticates")
        entries.append(token_auth_config(github_token, url))
    return git_config_env(entries)
