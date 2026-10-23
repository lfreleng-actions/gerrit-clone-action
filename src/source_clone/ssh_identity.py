# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""Validation for the SSH identity file setting.

``GERRIT_SSH_PRIVATE_KEY`` (and ``--ssh-identity-file``) take a *path*,
but the name invites passing the key material itself, as CI secrets
usually hold it.  Default path validation then echoes the value in its
error, and that output is re-wrapped, so a CI runner's secret masking
can miss parts of the key.  These helpers recognise key content and
reject it with a message that never includes the value.
"""

from __future__ import annotations

import os
from pathlib import Path

import typer

KEY_CONTENT_HINT = (
    "looks like private key content, not a file path. Supply the path "
    "to a key file, or load the key into ssh-agent and omit it. The "
    "value is withheld to avoid disclosing it."
)


def looks_like_key_content(value: str) -> bool:
    """Return True when *value* is key material rather than a file path."""
    return "\n" in value or "\r" in value or "PRIVATE KEY" in value


def parse_identity_file(value: str | Path) -> Path:
    """Convert a ``--ssh-identity-file`` value to a readable file path.

    Used as the Typer ``parser`` in place of Typer's path type, whose
    errors quote the offending value.  Real paths must exist, be a
    readable regular file, and are resolved; FIFOs and devices are
    refused because ``ssh -i`` could block reading them.

    Raises:
        typer.BadParameter: If *value* is key content or not a readable
            regular file.  Key content is never included in the message.
    """
    text = str(value)
    if looks_like_key_content(text):
        raise typer.BadParameter(f"Value {KEY_CONTENT_HINT}")

    path = Path(text).resolve()
    if not path.exists():
        raise typer.BadParameter(f"File '{text}' does not exist.")
    if path.is_dir():
        raise typer.BadParameter(f"File '{text}' is a directory.")
    if not path.is_file():
        raise typer.BadParameter(f"File '{text}' is not a regular file.")
    if not os.access(path, os.R_OK):
        raise typer.BadParameter(f"File '{text}' is not readable.")
    return path
