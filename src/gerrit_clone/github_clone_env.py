# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""Git process environment for non-interactive GitHub clones."""

from __future__ import annotations

import os
from typing import TYPE_CHECKING

from gerrit_clone.git_credential_env import build_token_auth_env
from gerrit_clone.github_clone_url import assert_trusted_origin

if TYPE_CHECKING:
    from gerrit_clone.models import Config

#: Disables every credential helper for the clone, so git cannot prompt,
#: write the token into the system keychain, or fall back to a cached
#: credential.  Only applied when a token is supplied: authentication
#: then comes exclusively from it, and fails predictably when it is
#: invalid.  Without a token the helper *is* the authentication.
_NO_CREDENTIAL_HELPER = ("credential.helper", "")


def build_git_env(config: Config, clone_url: str) -> dict[str, str]:
    """Build environment variables for git commands.

    Sets up the environment for secure, non-interactive git operations:
    - SSH configuration if ssh_identity_file is provided
    - GIT_TERMINAL_PROMPT=0 for HTTP(S) to prevent interactive prompts
    - Credential helpers disabled when a token supersedes them
    - Token authentication supplied through ``GIT_CONFIG_*`` variables,
      scoped to the clone URL's origin

    The token is deliberately kept out of the clone URL: a URL is passed
    to ``git clone`` as an argument and so appears in the host's process
    listing for the lifetime of the clone, which nothing can redact
    after the fact.  See :mod:`gerrit_clone.git_credential_env`.

    Args:
        config: Configuration with optional SSH and HTTPS settings
        clone_url: URL about to be cloned.  Required, because the
            credential is scoped to its origin and checked against the
            configured host; ``config.use_https`` alone cannot answer
            either question.  Whether the clone speaks HTTP(S) is taken
            from here too: the SSH branch falls back to an HTTPS URL
            when a project has no SSH URL, and such a clone needs the
            credential just as much.  Token authentication is only added
            for an ``https://`` URL, so a plaintext or otherwise
            unexpected scheme never receives it.

    Raises:
        UnsafeCloneUrlError: If a token is configured and *clone_url* is
            not on the configured source host.  Scoping the credential
            to the URL's origin is no help if that origin is the
            attacker's.
    """
    env = os.environ.copy()

    # Add SSH key if provided
    if config.ssh_identity_file:
        ssh_cmd = f"ssh -i {config.ssh_identity_file}"
        if not config.strict_host_checking:
            ssh_cmd += " -o StrictHostKeyChecking=no"
        env["GIT_SSH_COMMAND"] = ssh_cmd
    elif not config.strict_host_checking:
        env["GIT_SSH_COMMAND"] = "ssh -o StrictHostKeyChecking=no"

    over_http = clone_url.startswith(("http://", "https://"))
    over_https = clone_url.startswith("https://")

    if over_http:
        # Disable interactive prompts (fail fast if auth fails)
        env["GIT_TERMINAL_PROMPT"] = "0"

        token = config.github_token if over_https else None
        if token:
            assert_trusted_origin(clone_url, config)
        # Credential helpers are only suppressed when the token replaces
        # them.  Without one, the documented "HTTPS with credential
        # helper" mode is the whole point, so disabling them would leave
        # no way to authenticate at all.
        extra = [_NO_CREDENTIAL_HELPER] if token else []
        env.update(build_token_auth_env(token, clone_url, extra))

    return env
