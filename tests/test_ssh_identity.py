# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""Tests that SSH key content supplied as an identity path is never echoed."""

from __future__ import annotations

import os
import re
from pathlib import Path

import pytest
import typer
from typer.testing import CliRunner

from source_clone.cli import app
from source_clone.config import ConfigurationError, load_config
from source_clone.ssh_identity import looks_like_key_content, parse_identity_file

# Assembled at runtime so secret scanners do not flag this fixture.
_MARKER = "OPENSSH " + "PRIVATE KEY"
SENTINEL = "SentinelKeyBody0123456789"
FAKE_KEY = f"-----BEGIN {_MARKER}-----\n{SENTINEL}\n-----END {_MARKER}-----\n"


def _squash(text: str) -> str:
    """Strip ANSI codes and every non-alphanumeric character.

    Rich wraps and boxes error output, which could split the sentinel
    across lines; squashing makes the absence check robust to that.
    """
    text = re.sub(r"\x1b\[[0-9;]*m", "", text)
    return re.sub(r"[^A-Za-z0-9]", "", text)


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (FAKE_KEY, True),
        (f"{_MARKER} without newlines", True),
        ("line one\nline two", True),
        ("line one\rline two", True),
        ("~/.ssh/id_ed25519", False),
        ("/tmp/gerrit key", False),
    ],
)
def test_looks_like_key_content(value: str, expected: bool) -> None:
    """Key material is recognised; ordinary paths are not."""
    assert looks_like_key_content(value) is expected


def test_parser_rejects_key_content_without_echoing_it() -> None:
    """The parser error must not contain any of the key material."""
    with pytest.raises(typer.BadParameter) as excinfo:
        parse_identity_file(FAKE_KEY)
    assert SENTINEL not in str(excinfo.value)
    assert "withheld" in str(excinfo.value)


def test_parser_accepts_readable_file(tmp_path: Path) -> None:
    """A readable file resolves to its absolute path."""
    key_file = tmp_path / "id_ed25519"
    key_file.write_text("placeholder")
    assert parse_identity_file(str(key_file)) == key_file.resolve()


@pytest.mark.parametrize(
    ("name", "make_dir", "message"),
    [("missing", False, "does not exist"), ("keys", True, "is a directory")],
)
def test_parser_rejects_unusable_paths(
    tmp_path: Path, name: str, make_dir: bool, message: str
) -> None:
    """Missing files and directories are rejected as before."""
    target = tmp_path / name
    if make_dir:
        target.mkdir()
    with pytest.raises(typer.BadParameter, match=message):
        parse_identity_file(str(target))


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="requires os.mkfifo")
def test_parser_rejects_fifo(tmp_path: Path) -> None:
    """A FIFO is refused: ``ssh -i`` could block reading it."""
    fifo = tmp_path / "id_fifo"
    os.mkfifo(fifo)
    with pytest.raises(typer.BadParameter, match="not a regular file"):
        parse_identity_file(str(fifo))


@pytest.mark.parametrize(
    "args",
    [
        ["clone", "--host", "gerrit.example.org"],
        ["mirror", "--server", "gerrit.example.org"],
    ],
)
def test_cli_rejects_key_content_in_env_without_echoing_it(args: list[str]) -> None:
    """Key content in GERRIT_SSH_PRIVATE_KEY fails cleanly and stays hidden."""
    result = CliRunner().invoke(app, args, env={"GERRIT_SSH_PRIVATE_KEY": FAKE_KEY})

    assert result.exit_code == 2
    squashed = _squash(result.output)
    assert SENTINEL not in squashed
    assert "withheld" in squashed


def test_load_config_rejects_key_content_without_echoing_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The config loader's environment path is guarded as well."""
    monkeypatch.setenv("GERRIT_SSH_PRIVATE_KEY", FAKE_KEY)
    with pytest.raises(ConfigurationError) as excinfo:
        load_config(host="gerrit.example.org")
    assert SENTINEL not in str(excinfo.value)
    assert "GERRIT_SSH_PRIVATE_KEY" in str(excinfo.value)


def test_load_config_rejects_key_content_passed_as_path() -> None:
    """A Path argument holding key content gets the same protection."""
    with pytest.raises(ConfigurationError) as excinfo:
        load_config(host="gerrit.example.org", ssh_identity_file=Path(FAKE_KEY))
    assert SENTINEL not in str(excinfo.value)
