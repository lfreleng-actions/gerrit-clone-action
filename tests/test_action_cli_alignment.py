# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2025 The Linux Foundation

"""Tests to verify alignment between action.yaml inputs and CLI flags.

This test suite ensures that the GitHub Action interface (action.yaml) correctly
maps to the source-clone CLI flags. It catches mismatches like using `--path`
instead of `--output-path` in the action's shell script.
"""

from __future__ import annotations

import inspect
import re
import subprocess
from pathlib import Path
from typing import Any, ClassVar

import pytest
import yaml

# Import the CLI app to introspect its options
from gerrit_clone.cli import clone as clone_command

# Path to the action.yaml file (relative to project root)
ACTION_YAML_PATH = Path(__file__).parent.parent / "action.yaml"


def get_cli_option_names() -> set[str]:
    """Extract all CLI option names from the clone command using typer introspection."""
    option_names: set[str] = set()

    # Get the typer command's parameters
    # The clone function has typer.Option() defaults that contain the option info
    sig = inspect.signature(clone_command)
    for _param_name, param in sig.parameters.items():
        if param.default is not inspect.Parameter.empty:
            default = param.default
            # Check if it's a typer Option
            if hasattr(default, "param_decls"):
                # param_decls contains the option declarations like ('--host', '-h')
                # or combined forms like ('--skip-archived/--include-archived',)
                for decl in default.param_decls:
                    if decl.startswith("--"):
                        # Handle combined boolean flags like --skip-archived/--include-archived
                        if "/" in decl:
                            # Split on / and extract each flag
                            for flag_part in decl.split("/"):
                                stripped_flag = flag_part.strip()
                                if stripped_flag.startswith("--"):
                                    option_names.add(stripped_flag[2:])
                        else:
                            # Simple flag like --host
                            option_names.add(decl[2:])
    return option_names


@pytest.fixture
def action_yaml() -> dict[str, Any]:
    """Load and parse the action.yaml file."""
    with ACTION_YAML_PATH.open() as f:
        result: dict[str, Any] = yaml.safe_load(f)
        return result


@pytest.fixture
def cli_option_names() -> set[str]:
    """Get the set of valid CLI option names."""
    return get_cli_option_names()


@pytest.fixture
def action_shell_script(action_yaml: dict[str, Any]) -> str:
    """Extract the main shell script from action.yaml that builds the command."""
    # Find the step that builds and executes the source-clone command
    steps = action_yaml.get("runs", {}).get("steps", [])
    for step in steps:
        if step.get("id") == "clone" and "run" in step:
            script: str = step["run"]
            return script
    raise ValueError("Could not find the clone step with shell script in action.yaml")


class TestActionInputsExist:
    """Test that all action.yaml inputs are defined."""

    def test_action_yaml_exists(self) -> None:
        """Verify action.yaml file exists."""
        assert ACTION_YAML_PATH.exists(), f"action.yaml not found at {ACTION_YAML_PATH}"

    def test_action_yaml_is_valid(self, action_yaml: dict[str, Any]) -> None:
        """Verify action.yaml is valid YAML with expected structure."""
        assert "name" in action_yaml
        assert "inputs" in action_yaml
        assert "runs" in action_yaml
        assert action_yaml["runs"]["using"] == "composite"

    def test_required_inputs_defined(self, action_yaml: dict[str, Any]) -> None:
        """Verify required inputs are defined."""
        inputs = action_yaml["inputs"]
        assert "host" in inputs
        assert inputs["host"]["required"] is True

    def test_output_path_input_defined(self, action_yaml: dict[str, Any]) -> None:
        """Verify output-path input is defined correctly."""
        inputs = action_yaml["inputs"]
        assert "output-path" in inputs
        assert "description" in inputs["output-path"]


class TestCLIOptionsIntrospection:
    """Test CLI options using typer introspection."""

    def test_cli_has_expected_options(self, cli_option_names: set[str]) -> None:
        """Verify CLI has the expected core options."""
        expected_options = {
            "host",
            "output-path",
            "skip-archived",
            "threads",
            "clone-timeout",
            "retry-attempts",
            "verbose",
            "quiet",
        }
        for opt in expected_options:
            assert opt in cli_option_names, (
                f"Expected CLI option '--{opt}' not found. "
                f"Available: {sorted(cli_option_names)}"
            )

    def test_output_path_exists(self, cli_option_names: set[str]) -> None:
        """Verify --output-path option exists in CLI."""
        assert "output-path" in cli_option_names

    def test_path_does_not_exist(self, cli_option_names: set[str]) -> None:
        """Verify standalone --path option does NOT exist (it's --output-path)."""
        # 'path' alone should not be an option (output-path is correct)
        assert "path" not in cli_option_names, (
            "Found '--path' as a CLI option. This is likely incorrect - "
            "the option should be '--output-path'"
        )


class TestActionCLIFlagAlignment:
    """Test alignment between action.yaml inputs and CLI flags used in the script."""

    def test_output_path_uses_correct_cli_flag(
        self, action_shell_script: str, cli_option_names: set[str]
    ) -> None:
        """Verify action uses --output-path, not --path."""
        # This is the critical test that would have caught the bug
        assert "--output-path" in action_shell_script, (
            "Action script should use '--output-path' flag"
        )

        # Check that --path is NOT used (except as part of --output-path)
        # Find all occurrences of cmd="$cmd --something"
        flag_pattern = r'cmd="\$cmd\s+--(\S+)'
        matches = re.findall(flag_pattern, action_shell_script)

        for match in matches:
            # Extract just the flag name (before any quotes or spaces)
            flag_name = match.split("'")[0].split('"')[0].strip()
            if flag_name == "path":
                pytest.fail(
                    "Action uses '--path' but CLI expects '--output-path'. "
                    "This mismatch will cause the action to fail."
                )

    def test_all_cli_flags_in_script_are_valid(
        self, action_shell_script: str, cli_option_names: set[str]
    ) -> None:
        """Verify all CLI flags used in action script are valid CLI options."""
        # Match standalone flag additions like: cmd="$cmd --flag"
        flag_pattern = r"--([a-z][a-z0-9-]+)"
        all_flags_in_script = set(re.findall(flag_pattern, action_shell_script))

        # Filter to only flags that appear in cmd building context
        # by looking for flags followed by common patterns
        cmd_building_flags = set()
        for flag in all_flags_in_script:
            # Check if this flag appears in a cmd="$cmd --flag pattern
            if re.search(rf'cmd="\$cmd\s+--{re.escape(flag)}', action_shell_script):
                cmd_building_flags.add(flag)

        # Known valid aliases that are part of boolean flag pairs
        # These should now be properly extracted by get_cli_option_names()
        # but we keep a small set for any edge cases
        known_valid_aliases: set[str] = set()

        invalid_flags = []
        for flag in cmd_building_flags:
            if flag in known_valid_aliases:
                continue
            if flag not in cli_option_names:
                invalid_flags.append(flag)

        if invalid_flags:
            pytest.fail(
                f"Action script uses invalid CLI flags: {sorted(invalid_flags)}\n"
                f"Valid CLI options: {sorted(cli_option_names)}"
            )

    def test_critical_flags_are_correctly_mapped(
        self, action_shell_script: str
    ) -> None:
        """Test that critical flags are correctly mapped in the action script."""
        critical_mappings = {
            "output-path": "--output-path",
            "clone-timeout": "--clone-timeout",
            "retry-attempts": "--retry-attempts",
            "discovery-method": "--discovery-method",
        }

        for input_name, expected_flag in critical_mappings.items():
            if (
                f"inputs.{input_name}" in action_shell_script
                or f"inputs['{input_name}']" in action_shell_script
            ):
                assert expected_flag in action_shell_script, (
                    f"Input '{input_name}' is referenced but CLI flag "
                    f"'{expected_flag}' not found in script"
                )


class TestInputDescriptionsMatchCLI:
    """Test that action input descriptions align with CLI help."""

    def test_output_path_description_mentions_default(
        self, action_yaml: dict[str, Any]
    ) -> None:
        """Verify output-path description mentions the default behavior."""
        assert "." in action_yaml["inputs"]["output-path"].get("default", ""), (
            "output-path should have a sensible default"
        )


class TestActionOutputsAlignment:
    """Test that action outputs are properly configured."""

    def test_manifest_path_output_exists(self, action_yaml: dict[str, Any]) -> None:
        """Verify manifest-path output is defined."""
        outputs = action_yaml.get("outputs", {})
        # Check for manifest-related output
        manifest_outputs = [k for k in outputs if "manifest" in k.lower()]
        assert len(manifest_outputs) > 0, "Action should have a manifest path output"

    def test_count_outputs_exist(self, action_yaml: dict[str, Any]) -> None:
        """Verify count outputs are defined."""
        outputs = action_yaml.get("outputs", {})
        expected_outputs = ["success-count", "failure-count", "total-count"]
        for expected in expected_outputs:
            # Check with both hyphen and underscore variants
            found = (
                expected in outputs
                or expected.replace("-", "_") in outputs
                or expected.replace("-", "") in outputs
            )
            assert found, f"Expected output '{expected}' not found in action outputs"


class TestNoDeprecatedFlags:
    """Test that deprecated or renamed flags are not used."""

    def test_no_path_prefix_flag(self, action_shell_script: str) -> None:
        """Verify --path-prefix is not used (was renamed to --output-path)."""
        assert "--path-prefix" not in action_shell_script, (
            "Action uses deprecated '--path-prefix' flag. Use '--output-path' instead."
        )

    def test_no_dest_path_flag(self, action_shell_script: str) -> None:
        """Verify --dest-path is not used (was renamed to --output-path)."""
        assert "--dest-path" not in action_shell_script, (
            "Action uses deprecated '--dest-path' flag. Use '--output-path' instead."
        )

    def test_no_output_dir_flag(self, action_shell_script: str) -> None:
        """Verify --output-dir is not used (correct flag is --output-path)."""
        assert "--output-dir" not in action_shell_script, (
            "Action uses '--output-dir' flag. Use '--output-path' instead."
        )

    def test_no_standalone_path_flag(self, action_shell_script: str) -> None:
        """Verify --path is not used alone (should be --output-path)."""
        # Pattern to find --path that is NOT part of --output-path or --ssh-identity-file
        # Look for: --path followed by space, quote, or end of line
        pattern = r"--path(?![a-z-])"
        matches = re.findall(pattern, action_shell_script)
        # Filter out matches that are part of --output-path
        if matches:
            # Double-check by looking at context
            for match in re.finditer(r"--path\b", action_shell_script):
                start = max(0, match.start() - 20)
                context = action_shell_script[start : match.end() + 10]
                if "output-path" not in context and "private-key" not in context:
                    pytest.fail(
                        f"Action uses standalone '--path' flag. "
                        f"Use '--output-path' instead. Context: {context!r}"
                    )


class TestBooleanFlagConsistency:
    """Test that boolean flags are handled consistently."""

    BOOLEAN_FLAG_PAIRS: ClassVar[list[tuple[str, str]]] = [
        ("skip-archived", "include-archived"),
        ("allow-nested-git", "no-allow-nested-git"),
        ("nested-protection", "no-nested-protection"),
        ("move-conflicting", "no-move-conflicting"),
        ("mirror", "no-mirror"),
        ("https", "ssh"),
        ("strict-host", "accept-unknown-host"),
    ]

    def test_boolean_flags_use_correct_form(self, cli_option_names: set[str]) -> None:
        """Verify both positive and negative forms of boolean flags exist in CLI."""
        for positive, negative in self.BOOLEAN_FLAG_PAIRS:
            # Typer generates both forms for boolean flags (e.g., --mirror/--no-mirror)
            # Both forms should exist in the CLI
            assert positive in cli_option_names, (
                f"Positive flag '--{positive}' not found in CLI options. "
                f"Available: {sorted(cli_option_names)}"
            )
            assert negative in cli_option_names, (
                f"Negative flag '--{negative}' not found in CLI options. "
                f"Available: {sorted(cli_option_names)}"
            )


class TestCLIFlagExecution:
    """Test that CLI flags can actually be parsed (without executing clone)."""

    @pytest.mark.parametrize("command", ["source-clone", "gerrit-clone"])
    def test_output_path_flag_is_accepted(self, tmp_path: Path, command: str) -> None:
        """Verify --output-path flag is accepted by CLI under either name."""
        result = subprocess.run(
            [
                command,
                "clone",
                "--host",
                "test.example.org",
                "--output-path",
                str(tmp_path),
                "--help",  # Use help to avoid actual execution
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        # Verify the command succeeded (help should return 0)
        assert result.returncode == 0, (
            f"{command} failed with code {result.returncode}.\n"
            f"stdout: {result.stdout}\n"
            f"stderr: {result.stderr}"
        )
        # Verify we got help output (proves CLI parsed the flags correctly)
        assert "Usage:" in result.stdout or "clone" in result.stdout.lower(), (
            f"Expected help output but got: {result.stdout}"
        )
        # Should not fail with "No such option" error
        assert "No such option" not in result.stderr, (
            f"CLI rejected a flag: {result.stderr}"
        )

    def test_path_flag_is_rejected(self, tmp_path: Path) -> None:
        """Verify --path flag is NOT accepted by CLI (should suggest --output-path)."""
        result = subprocess.run(
            [
                "source-clone",
                "clone",
                "--host",
                "test.example.org",
                "--path",
                str(tmp_path),
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        # Should fail with "No such option" error
        assert result.returncode != 0
        assert (
            "No such option: --path" in result.stderr
            or "No such option" in result.stderr
        )


class TestActionInputToCLIMapping:
    """Test that action inputs map correctly to CLI options."""

    INPUT_TO_CLI_MAP: ClassVar[list[tuple[str, str]]] = [
        ("host", "host"),
        ("port", "port"),
        ("ssh-user", "ssh-user"),
        ("base-url", "base-url"),
        ("output-path", "output-path"),
        ("skip-archived", "skip-archived"),
        ("include-projects", "include-projects"),
        ("exclude-projects", "exclude-projects"),
        ("threads", "threads"),
        ("depth", "depth"),
        ("branch", "branch"),
        ("clone-timeout", "clone-timeout"),
        ("retry-attempts", "retry-attempts"),
        ("retry-base-delay", "retry-base-delay"),
        ("retry-factor", "retry-factor"),
        ("retry-max-delay", "retry-max-delay"),
        ("manifest-filename", "manifest-filename"),
        ("config-file", "config-file"),
        ("verbose", "verbose"),
        ("quiet", "quiet"),
        ("log-file", "log-file"),
        ("disable-log-file", "disable-log-file"),
        ("log-level", "log-level"),
        ("force", "force"),
        ("https", "https"),
    ]

    def test_action_inputs_have_matching_cli_options(
        self, action_yaml: dict[str, Any], cli_option_names: set[str]
    ) -> None:
        """Verify each action input has a corresponding CLI option."""
        action_inputs = action_yaml.get("inputs", {})

        for action_input, cli_flag in self.INPUT_TO_CLI_MAP:
            if action_input in action_inputs:
                assert cli_flag in cli_option_names, (
                    f"Action input '{action_input}' should map to CLI flag "
                    f"'--{cli_flag}', but '--{cli_flag}' not found in CLI options. "
                    f"Available options: {sorted(cli_option_names)}"
                )

    def test_output_path_not_mapped_to_path(
        self, action_yaml: dict[str, Any], cli_option_names: set[str]
    ) -> None:
        """Explicitly verify output-path maps to --output-path, not --path."""
        assert "output-path" in action_yaml.get("inputs", {}), (
            "Action should have 'output-path' input"
        )
        assert "output-path" in cli_option_names, (
            "CLI should have '--output-path' option"
        )
        assert "path" not in cli_option_names, (
            "CLI should NOT have standalone '--path' option"
        )


ALIAS_INPUT_PAIRS: list[tuple[str, str]] = [
    ("use-https", "https"),
    ("force-refresh", "force"),
]

EXPRESSION_PATTERN = re.compile(r"\$\{\{(.*?)\}\}", re.DOTALL)


def _reads_input(expression: str, name: str) -> bool:
    """Return whether *expression* reads ``inputs.<name>``."""
    return re.search(rf"\binputs\.{re.escape(name)}(?![\w-])", expression) is not None


class TestAliasInputs:
    """``force`` and ``https`` behave exactly like their older spellings."""

    @pytest.mark.parametrize(("old", "new"), ALIAS_INPUT_PAIRS)
    def test_alias_input_defined(
        self, action_yaml: dict[str, Any], old: str, new: str
    ) -> None:
        """The new input exists, is optional and defaults like the old one."""
        inputs = action_yaml["inputs"]
        assert new in inputs
        assert inputs[new].get("required") is False
        assert inputs[new].get("default") == "false"
        assert inputs[old].get("default") == "false"

    @pytest.mark.parametrize(("old", "new"), ALIAS_INPUT_PAIRS)
    def test_every_read_honours_both_spellings(self, old: str, new: str) -> None:
        """Each expression that reads the old input reads the new one too."""
        text = ACTION_YAML_PATH.read_text(encoding="utf-8")
        reads = [
            expr
            for expr in EXPRESSION_PATTERN.findall(text)
            if _reads_input(expr, old) or _reads_input(expr, new)
        ]
        assert reads, f"No expression reads inputs.{old}"
        for expr in reads:
            assert _reads_input(expr, old), f"{expr!r} ignores inputs.{old}"
            assert _reads_input(expr, new), f"{expr!r} ignores inputs.{new}"

    def test_env_mappings_read_both_spellings(
        self, action_yaml: dict[str, Any]
    ) -> None:
        """INPUT_USE_HTTPS and INPUT_FORCE_REFRESH are true for either input."""
        env_names = {
            "INPUT_USE_HTTPS": "use-https",
            "INPUT_FORCE_REFRESH": "force-refresh",
        }
        partners = dict(ALIAS_INPUT_PAIRS)
        seen: set[str] = set()
        for step in action_yaml["runs"]["steps"]:
            for name, value in (step.get("env") or {}).items():
                if name not in env_names:
                    continue
                seen.add(name)
                old = env_names[name]
                new = partners[old]
                assert f"inputs.{old} == 'true'" in value, (name, value)
                assert f"inputs.{new} == 'true'" in value, (name, value)
                assert "||" in value, (name, value)
        assert seen == set(env_names)

    def test_ssh_setup_skipped_for_either_https_input(
        self, action_yaml: dict[str, Any]
    ) -> None:
        """The SSH setup step runs only when neither HTTPS input is true."""
        steps = action_yaml["runs"]["steps"]
        ssh = next(s for s in steps if s.get("name") == "Setup SSH authentication")
        condition = ssh["if"]
        assert "inputs.use-https != 'true'" in condition
        assert "inputs.https != 'true'" in condition
        assert "&&" in condition


class TestNoEmptyExpressions:
    """Detect empty or malformed GitHub Actions expression patterns.

    The Actions template engine parses ALL ${{ }} patterns in run
    blocks — including inside shell comments.  An empty expression
    like ``${{ }}`` causes the runner to reject the manifest with
    "An expression was expected".

    See: https://github.com/lfreleng-actions/project-reporting-tool/
         actions/runs/24069756050
    """

    EXPRESSION_RE: ClassVar[re.Pattern[str]] = re.compile(r"\$\{\{(.*?)\}\}", re.DOTALL)

    def _collect_run_blocks(self, action_yaml: dict[str, Any]) -> list[tuple[str, str]]:
        """Extract all run blocks from the action manifest.

        Returns:
            List of (step_name, run_content) tuples.
        """
        blocks: list[tuple[str, str]] = []
        steps = action_yaml.get("runs", {}).get("steps", [])
        for step in steps:
            if "run" in step:
                name = step.get("name", step.get("id", "unnamed"))
                blocks.append((name, step["run"]))
        return blocks

    def test_no_empty_expressions_in_run_blocks(
        self, action_yaml: dict[str, Any]
    ) -> None:
        """Every ${{ }} expression must have non-empty content."""
        violations: list[str] = []
        for step_name, script in self._collect_run_blocks(action_yaml):
            for match in self.EXPRESSION_RE.finditer(script):
                body = match.group(1).strip()
                if not body:
                    # Show surrounding context for easier debugging
                    start = max(0, match.start() - 30)
                    end = min(len(script), match.end() + 30)
                    context = script[start:end].replace("\n", "\\n")
                    violations.append(
                        f"Step '{step_name}': empty expression near: ...{context}..."
                    )
        assert not violations, (
            "Found empty ${{{{ }}}} expression(s) in action.yaml run "
            "blocks. The GitHub Actions template engine rejects these "
            "with 'An expression was expected':\n"
            + "\n".join(f"  - {v}" for v in violations)
        )

    def test_no_unclosed_expressions_in_run_blocks(
        self, action_yaml: dict[str, Any]
    ) -> None:
        """Every ${{ must have a matching }}."""
        unclosed_re = re.compile(r"\$\{\{(?:(?!\}\}).)*$", re.DOTALL)
        violations: list[str] = []
        for step_name, script in self._collect_run_blocks(action_yaml):
            for match in unclosed_re.finditer(script):
                start = max(0, match.start() - 20)
                end = min(len(script), match.end() + 20)
                context = script[start:end].replace("\n", "\\n")
                violations.append(
                    f"Step '{step_name}': unclosed expression near: ...{context}..."
                )
        assert not violations, (
            "Found unclosed ${{{{ expression(s) in action.yaml run "
            "blocks:\n" + "\n".join(f"  - {v}" for v in violations)
        )


@pytest.fixture
def install_script(action_yaml: dict[str, Any]) -> str:
    """The run block of the step that installs the CLI."""
    for step in action_yaml["runs"]["steps"]:
        script = step.get("run", "")
        if "uvx " in script:
            return str(script)
    raise ValueError("Could not find the install step in action.yaml")


class TestActionRunsSourceClone:
    """The Action installs and runs the CLI as ``source-clone``."""

    def test_install_step_uses_source_clone(self, install_script: str) -> None:
        assert "command -v source-clone " in install_script
        assert "uvx source-clone --help" in install_script
        assert "exec uvx source-clone " in install_script
        assert '"$HOME/.local/bin/source-clone"' in install_script
        assert "gerrit-clone" not in install_script

    def test_command_array_starts_source_clone(self, action_shell_script: str) -> None:
        assert "cmd=(source-clone clone)" in action_shell_script
        assert "gerrit-clone" not in action_shell_script
