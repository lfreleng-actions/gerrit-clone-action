# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""Every subcommand accepts the same name for the same concept.

Each concept has one canonical flag name, listed first in ``--help``.
Older spellings stay accepted as aliases, because earlier ``action.yaml``
releases run an unpinned CLI and pass them.  Every spelling must reach
the command's run function with the same value.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import TYPE_CHECKING, Any
from unittest.mock import patch

import pytest
from typer.testing import CliRunner

from source_clone import cli_config, cli_options_clone
from source_clone.cli import app

if TYPE_CHECKING:
    from collections.abc import Callable

HOST = "gerrit.example.org"
ORG = "example-org"
PATH_TOKEN = "<path>"
ANSI = re.compile(r"\x1b\[[0-9;]*m")

# Patch target of each command's run function, and the base arguments
# that satisfy its other required options.
COMMANDS: dict[str, tuple[tuple[str, ...], list[str]]] = {
    "clone": (("source_clone.cli_clone.run_clone",), ["--host", HOST]),
    "mirror": (("source_clone.cli_mirror.run_mirror",), ["--server", HOST]),
    "refresh": (
        (
            "source_clone.cli_refresh.prepare_refresh",
            "source_clone.cli_refresh.run_refresh",
        ),
        [],
    ),
    "reset": (("source_clone.cli_reset.run_reset",), ["--org", ORG]),
}


def _run(command: str, args: list[str], *, base: bool = True) -> Any:
    """Invoke *command* with its work stubbed; return the request it got."""
    targets, base_args = COMMANDS[command]
    patches = [patch(target) for target in targets]
    mocks = [p.start() for p in patches]
    try:
        result = CliRunner().invoke(app, [command, *(base_args if base else []), *args])
    finally:
        for p in patches:
            p.stop()
    run = mocks[-1]
    assert result.exit_code == 0, result.output
    assert run.call_count == 1, result.output
    return run.call_args.args[0]


def _host(spelling: str) -> list[str]:
    return [spelling, HOST]


def _value(spelling: str, value: str) -> Callable[[Path], list[str]]:
    return lambda _tmp: [spelling, value]


def _path(spelling: str) -> Callable[[Path], list[str]]:
    return lambda tmp: [spelling, str(tmp)]


def _flag(spelling: str) -> Callable[[Path], list[str]]:
    return lambda _tmp: [spelling]


# (command, uses base args, argv builder, request attribute, expected)
SPELLINGS = [
    pytest.param(
        "clone", False, lambda _t: _host("--host"), "host", HOST, id="clone --host"
    ),
    pytest.param("clone", False, lambda _t: _host("-h"), "host", HOST, id="clone -h"),
    pytest.param(
        "mirror",
        False,
        lambda _t: _host("--server"),
        "server",
        HOST,
        id="mirror --server",
    ),
    pytest.param(
        "mirror",
        False,
        lambda _t: _host("--host"),
        "server",
        HOST,
        id="mirror --host",
    ),
    pytest.param(
        "clone",
        True,
        _value("--github-org", ORG),
        "github_org",
        ORG,
        id="clone --github-org",
    ),
    pytest.param(
        "clone",
        True,
        _value("--org", ORG),
        "github_org",
        ORG,
        id="clone --org",
    ),
    pytest.param("mirror", True, _value("--org", ORG), "org", ORG, id="mirror --org"),
    pytest.param("reset", False, _value("--org", ORG), "org", ORG, id="reset --org"),
    pytest.param(
        "clone",
        True,
        _path("--output-path"),
        "output_path",
        PATH_TOKEN,
        id="clone --output-path",
    ),
    pytest.param(
        "mirror",
        True,
        _path("--output-path"),
        "output_path",
        PATH_TOKEN,
        id="mirror --output-path",
    ),
    pytest.param(
        "refresh",
        True,
        _path("--output-path"),
        "output_path",
        PATH_TOKEN,
        id="refresh --output-path",
    ),
    pytest.param("reset", True, _path("--path"), "path", PATH_TOKEN, id="reset --path"),
    pytest.param(
        "reset",
        True,
        _path("--output-path"),
        "path",
        PATH_TOKEN,
        id="reset --output-path",
    ),
    pytest.param(
        "clone",
        True,
        _value("--clone-timeout", "120"),
        "clone_timeout",
        120,
        id="clone --clone-timeout",
    ),
    pytest.param(
        "clone",
        True,
        _value("--timeout", "120"),
        "clone_timeout",
        120,
        id="clone --timeout",
    ),
    pytest.param(
        "refresh",
        True,
        _value("--timeout", "120"),
        "timeout",
        120,
        id="refresh --timeout",
    ),
    pytest.param(
        "clone",
        True,
        _value("--include-projects", "a"),
        "include_projects",
        ["a"],
        id="clone --include-projects",
    ),
    pytest.param(
        "clone",
        True,
        _value("--include-project", "a"),
        "include_projects",
        ["a"],
        id="clone --include-project",
    ),
    pytest.param(
        "refresh",
        True,
        _value("--include-projects", "a"),
        "include_projects",
        ["a"],
        id="refresh --include-projects",
    ),
    pytest.param(
        "refresh",
        True,
        _value("--include-project", "a"),
        "include_projects",
        ["a"],
        id="refresh --include-project",
    ),
    pytest.param(
        "mirror",
        True,
        _value("--include-projects", "a"),
        "include_projects",
        "a",
        id="mirror --include-projects",
    ),
    pytest.param(
        "mirror",
        True,
        _value("--projects", "a"),
        "include_projects",
        "a",
        id="mirror --projects",
    ),
    pytest.param(
        "mirror",
        True,
        _value("--include-project", "a"),
        "include_projects",
        "a",
        id="mirror --include-project",
    ),
    pytest.param(
        "clone",
        True,
        _value("--exclude-projects", "b"),
        "exclude_projects",
        ["b"],
        id="clone --exclude-projects",
    ),
    pytest.param(
        "clone",
        True,
        _value("--exclude-project", "b"),
        "exclude_projects",
        ["b"],
        id="clone --exclude-project",
    ),
    pytest.param(
        "refresh",
        True,
        _value("--exclude-projects", "b"),
        "exclude_projects",
        ["b"],
        id="refresh --exclude-projects",
    ),
    pytest.param(
        "refresh",
        True,
        _value("--exclude-project", "b"),
        "exclude_projects",
        ["b"],
        id="refresh --exclude-project",
    ),
    pytest.param(
        "mirror",
        True,
        _value("--exclude-projects", "b"),
        "exclude_projects",
        "b",
        id="mirror --exclude-projects",
    ),
    pytest.param(
        "mirror",
        True,
        _value("--exclude-project", "b"),
        "exclude_projects",
        "b",
        id="mirror --exclude-project",
    ),
    pytest.param(
        "clone",
        True,
        _flag("--exit-on-error"),
        "exit_on_error",
        True,
        id="clone --exit-on-error",
    ),
    pytest.param(
        "clone",
        True,
        _flag("--stop-on-first-error"),
        "exit_on_error",
        True,
        id="clone --stop-on-first-error",
    ),
    pytest.param("clone", True, _flag("--force"), "force", True, id="clone --force"),
    pytest.param("clone", True, _flag("-f"), "force", True, id="clone -f"),
    pytest.param(
        "refresh", True, _flag("--force"), "force", True, id="refresh --force"
    ),
    pytest.param("refresh", True, _flag("-f"), "force", True, id="refresh -f"),
]


@pytest.mark.parametrize(("command", "base", "argv", "attr", "expected"), SPELLINGS)
def test_spelling_reaches_run_function(
    command: str,
    base: bool,
    argv: Callable[[Path], list[str]],
    attr: str,
    expected: object,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Each spelling hands the command's run function the same value."""
    monkeypatch.chdir(tmp_path)
    request = _run(command, argv(tmp_path), base=base)

    value = getattr(request, attr)
    if expected == PATH_TOKEN:
        assert Path(value).resolve() == tmp_path.resolve()
    else:
        assert value == expected


# (command, canonical name, alias listed after it)
HELP_ORDER = [
    pytest.param("mirror", "--host", "--server", id="mirror --host"),
    pytest.param("clone", "--org", "--github-org", id="clone --org"),
    pytest.param("reset", "--output-path", "--path", id="reset --output-path"),
    pytest.param(
        "clone",
        "--timeout",
        "--clone-timeout",
        id="clone --timeout",
    ),
    pytest.param(
        "mirror",
        "--include-projects",
        "--include-project",
        id="mirror --include-project",
    ),
    pytest.param("mirror", "--include-projects", "--projects", id="mirror --projects"),
    pytest.param(
        "mirror",
        "--exclude-projects",
        "--exclude-project",
        id="mirror --exclude-project",
    ),
    pytest.param(
        "clone", "--include-projects", "--include-project", id="clone --include-project"
    ),
    pytest.param(
        "clone", "--exclude-projects", "--exclude-project", id="clone --exclude-project"
    ),
    pytest.param(
        "refresh",
        "--include-projects",
        "--include-project",
        id="refresh --include-project",
    ),
    pytest.param(
        "refresh",
        "--exclude-projects",
        "--exclude-project",
        id="refresh --exclude-project",
    ),
]


def _token(name: str) -> re.Pattern[str]:
    return re.compile(rf"(?<![\w-]){re.escape(name)}(?![\w-])")


@pytest.mark.parametrize(("command", "canonical", "alias"), HELP_ORDER)
def test_help_lists_canonical_name_first(
    command: str, canonical: str, alias: str
) -> None:
    """The options table names the canonical flag before its alias."""
    result = CliRunner().invoke(app, [command, "--help"], env={"COLUMNS": "250"})
    assert result.exit_code == 0, result.output
    options = ANSI.sub("", result.output).split("Options", 1)[1]

    rows = [line for line in options.splitlines() if _token(alias).search(line)]
    assert rows, f"{alias} missing from {command} --help"
    row = rows[0]
    first = _token(canonical).search(row)
    assert first is not None, f"{canonical} not beside {alias}: {row}"
    alias_match = _token(alias).search(row)
    assert alias_match is not None
    assert first.start() < alias_match.start()


def test_config_host_help_matches_clone() -> None:
    """``config --host`` describes the option as ``clone --host`` does."""
    assert cli_config.HOST.help == cli_options_clone.HOST.help
