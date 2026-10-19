# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2025 Matthew Watkins <mwatkins@linuxfoundation.org>

"""Unit tests for configuration management."""

from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime
from itertools import pairwise
from pathlib import Path
from typing import TYPE_CHECKING
from unittest.mock import MagicMock, patch

import pytest
import yaml
from typer.testing import CliRunner

from source_clone.cli import app
from source_clone.config import ConfigManager, ConfigurationError, load_config
from source_clone.models import Config, DiscoveryMethod, SourceType

if TYPE_CHECKING:
    from collections.abc import Mapping


class TestConfigManager:
    """Test ConfigManager class."""

    def test_load_config_minimal(self):
        """Test loading minimal configuration."""
        manager = ConfigManager()
        config = manager.load_config(host="gerrit.example.org")

        assert config.host == "gerrit.example.org"
        assert config.port == 29418
        assert config.base_url == "https://gerrit.example.org"

    def test_load_config_all_cli_args(self):
        """Test loading configuration with all CLI arguments."""
        manager = ConfigManager()
        config = manager.load_config(
            host="gerrit.example.org",
            port=22,
            base_url="https://custom.example.org",
            ssh_user="testuser",
            path="/tmp/repos",
            skip_archived=False,
            threads=8,
            depth=10,
            branch="main",
            mirror=False,
            strict_host_checking=False,
            clone_timeout=300,
            retry_attempts=5,
            retry_base_delay=1.0,
            retry_factor=1.5,
            retry_max_delay=60.0,
            manifest_filename="manifest.json",
            verbose=True,
            quiet=False,
        )

        assert config.host == "gerrit.example.org"
        assert config.port == 22
        assert config.base_url == "https://custom.example.org"
        assert config.ssh_user == "testuser"
        assert config.path == Path("/tmp/repos").resolve()
        assert config.skip_archived is False
        assert config.threads == 8
        assert config.depth == 10
        assert config.branch == "main"
        assert config.mirror is False
        assert config.strict_host_checking is False
        assert config.clone_timeout == 300
        assert config.retry_policy.max_attempts == 5
        assert config.retry_policy.base_delay == 1.0
        assert config.retry_policy.factor == 1.5
        assert config.retry_policy.max_delay == 60.0
        assert config.manifest_filename == "manifest.json"
        assert config.verbose is True
        assert config.quiet is False

    def test_load_config_missing_host(self):
        """Test error when host is missing."""
        manager = ConfigManager()

        with pytest.raises(ConfigurationError, match="host is required"):
            manager.load_config()

    @patch.dict(
        os.environ,
        {
            "GERRIT_HOST": "env.gerrit.org",
            "GERRIT_PORT": "2222",
            "GERRIT_SSH_USER": "envuser",
            "GERRIT_SKIP_ARCHIVED": "0",
            "GERRIT_THREADS": "16",
            "GERRIT_CLONE_DEPTH": "5",
            "GERRIT_BRANCH": "develop",
            "GERRIT_MIRROR": "false",
            "GERRIT_STRICT_HOST": "false",
            "GERRIT_CLONE_TIMEOUT": "900",
            "GERRIT_RETRY_ATTEMPTS": "4",
            "GERRIT_RETRY_BASE_DELAY": "3.0",
        },
    )
    def test_load_config_from_env(self):
        """Test loading configuration from environment variables."""
        manager = ConfigManager()
        config = manager.load_config()

        assert config.host == "env.gerrit.org"
        assert config.port == 2222
        assert config.ssh_user == "envuser"
        assert config.skip_archived is False
        assert config.threads == 16
        assert config.depth == 5
        assert config.branch == "develop"
        assert config.mirror is False
        assert config.strict_host_checking is False
        assert config.clone_timeout == 900
        assert config.retry_policy.max_attempts == 4
        assert config.retry_policy.base_delay == 3.0

    def test_load_config_mirror_default_true(self):
        """Test that mirror defaults to True."""
        manager = ConfigManager()
        config = manager.load_config(host="gerrit.example.org")

        assert config.mirror is True

    def test_load_config_mirror_explicit_false(self):
        """Test setting mirror to False explicitly."""
        manager = ConfigManager()
        config = manager.load_config(host="gerrit.example.org", mirror=False)

        assert config.mirror is False

    def test_load_config_mirror_incompatible_with_depth(self):
        """Test that mirror mode ignores depth option with warning."""
        manager = ConfigManager()
        config = manager.load_config(host="gerrit.example.org", mirror=True, depth=10)

        assert config.mirror is True
        assert config.depth is None  # Depth is ignored in mirror mode

    def test_load_config_mirror_incompatible_with_branch(self):
        """Test that mirror mode ignores branch option with warning."""
        manager = ConfigManager()
        config = manager.load_config(
            host="gerrit.example.org", mirror=True, branch="main"
        )

        assert config.mirror is True
        assert config.branch is None  # Branch is ignored in mirror mode

    @patch.dict(
        os.environ, {"GERRIT_HOST": "env.example.org", "GERRIT_MIRROR": "false"}
    )
    def test_load_config_mirror_from_env(self):
        """Test loading mirror configuration from environment variable."""
        manager = ConfigManager()
        config = manager.load_config()

        assert config.host == "env.example.org"
        assert config.mirror is False

    @patch.dict(
        os.environ,
        {
            "GERRIT_HOST": "env.gerrit.org",
            "GERRIT_OUTPUT_DIR": "/legacy/path",  # Legacy env var
        },
    )
    def test_load_config_legacy_env_var(self):
        """Test loading configuration with legacy environment variable."""
        manager = ConfigManager()
        config = manager.load_config()

        assert config.path == Path("/legacy/path").resolve()

    @patch.dict(
        os.environ,
        {
            "GERRIT_HOST": "env.gerrit.org",
            "OUTPUT_PATH": "/new/path",
            "GERRIT_OUTPUT_DIR": "/legacy/path",  # Should be overridden
        },
    )
    def test_load_config_new_over_legacy_env_var(self):
        """Test new env var takes precedence over legacy."""
        manager = ConfigManager()
        config = manager.load_config()

        assert config.path == Path("/new/path").resolve()

    def test_load_config_cli_overrides_env(self):
        """Test CLI arguments override environment variables."""
        with patch.dict(
            os.environ,
            {
                "GERRIT_HOST": "env.gerrit.org",
                "GERRIT_PORT": "2222",
            },
        ):
            manager = ConfigManager()
            config = manager.load_config(
                host="cli.gerrit.org",
                port=3333,
            )

            assert config.host == "cli.gerrit.org"
            assert config.port == 3333

    def test_load_config_yaml_file(self):
        """Test loading configuration from YAML file."""
        yaml_config = {
            "host": "yaml.gerrit.org",
            "port": 2929,
            "ssh_user": "yamluser",
            "skip_archived": False,
            "threads": 12,
            "retry_attempts": 6,
        }

        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            yaml.dump(yaml_config, f)
            config_file = Path(f.name)

        try:
            manager = ConfigManager()
            config = manager.load_config(config_file=config_file)

            assert config.host == "yaml.gerrit.org"
            assert config.port == 2929
            assert config.ssh_user == "yamluser"
            assert config.skip_archived is False
            assert config.threads == 12
            assert config.retry_policy.max_attempts == 6
        finally:
            config_file.unlink()

    def test_load_config_json_file(self):
        """Test loading configuration from JSON file."""
        json_config = {
            "host": "json.gerrit.org",
            "port": 3939,
            "ssh_user": "jsonuser",
            "depth": 20,
            "branch": "release",
            "mirror": False,
        }

        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            json.dump(json_config, f)
            config_file = Path(f.name)

        try:
            manager = ConfigManager()
            config = manager.load_config(config_file=config_file)

            assert config.host == "json.gerrit.org"
            assert config.port == 3939
            assert config.ssh_user == "jsonuser"
            assert config.depth == 20
            assert config.branch == "release"
            assert config.mirror is False
        finally:
            config_file.unlink()

    def test_load_config_precedence(self):
        """Test configuration precedence: CLI > Env > File > Defaults."""
        # Create config file
        file_config = {
            "host": "file.gerrit.org",
            "port": 1111,
            "threads": 4,
        }

        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            yaml.dump(file_config, f)
            config_file = Path(f.name)

        try:
            with patch.dict(
                os.environ,
                {
                    "GERRIT_HOST": "env.gerrit.org",  # Should override file
                    "GERRIT_THREADS": "8",  # Should override file
                },
            ):
                manager = ConfigManager()
                config = manager.load_config(
                    host="cli.gerrit.org",  # Should override env and file
                    config_file=config_file,
                )

                # CLI wins
                assert config.host == "cli.gerrit.org"
                # Env wins over file
                assert config.threads == 8
                # File wins over defaults
                assert config.port == 1111
        finally:
            config_file.unlink()

    def test_load_config_invalid_file(self):
        """Test error with invalid config file."""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            f.write("invalid: yaml: content:")
            config_file = Path(f.name)

        try:
            manager = ConfigManager()
            with pytest.raises(ConfigurationError, match="Error parsing config file"):
                manager.load_config(config_file=config_file)
        finally:
            config_file.unlink()

    def test_load_config_missing_explicit_file(self):
        """Test error when explicit config file doesn't exist."""
        manager = ConfigManager()
        nonexistent_file = Path("/nonexistent/config.yaml")

        with pytest.raises(ConfigurationError, match="Config file not found"):
            manager.load_config(config_file=nonexistent_file)

    def test_load_config_unsupported_file_format(self):
        """Test error with unsupported config file format."""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False) as f:
            f.write("some content")
            config_file = Path(f.name)

        try:
            manager = ConfigManager()
            with pytest.raises(
                ConfigurationError, match="Unsupported config file format"
            ):
                manager.load_config(config_file=config_file)
        finally:
            config_file.unlink()

    @patch.dict(os.environ, {"GERRIT_SKIP_ARCHIVED": "invalid"})
    def test_parse_bool_invalid(self):
        """Test error parsing invalid boolean from environment."""
        manager = ConfigManager()

        with pytest.raises(ConfigurationError, match="Invalid boolean value"):
            manager.load_config(host="test")

    @patch.dict(os.environ, {"GERRIT_PORT": "not_a_number"})
    def test_parse_int_invalid(self):
        """Test error parsing invalid integer from environment."""
        manager = ConfigManager()

        with pytest.raises(ConfigurationError, match="Invalid integer value"):
            manager.load_config(host="test")

    @patch.dict(os.environ, {"GERRIT_RETRY_BASE_DELAY": "not_a_float"})
    def test_parse_float_invalid(self):
        """Test error parsing invalid float from environment."""
        manager = ConfigManager()

        with pytest.raises(ConfigurationError, match="Invalid float value"):
            manager.load_config(host="test")

    @patch.dict(
        os.environ,
        {
            "GERRIT_SKIP_ARCHIVED": "1",  # Should be True
            "GERRIT_STRICT_HOST": "false",  # Should be False
        },
    )
    def test_parse_bool_valid_values(self):
        """Test parsing valid boolean values from environment."""
        manager = ConfigManager()
        config = manager.load_config(host="test")

        assert config.skip_archived is True
        assert config.strict_host_checking is False


class TestLoadConfigFunction:
    """Test load_config convenience function."""

    def test_load_config_function(self):
        """Test load_config convenience function."""
        config = load_config(host="test.gerrit.org")

        assert isinstance(config, Config)
        assert config.host == "test.gerrit.org"

    def test_load_config_function_with_args(self):
        """Test load_config function with arguments."""
        config = load_config(
            host="test.gerrit.org",
            port=2222,
            threads=4,
        )

        assert config.host == "test.gerrit.org"
        assert config.port == 2222
        assert config.threads == 4


class TestProtocolDiscoveryConsistency:
    """Test that the loader keeps clone protocol and discovery consistent.

    Regression coverage for the bug where ``use_https`` forced the port to 443
    while discovery defaulted to SSH, producing an SSH connection against the
    HTTPS port.
    """

    def test_https_keeps_ssh_port_and_derives_http_discovery(self):
        """use_https must not mutate the SSH port and derives HTTP discovery."""
        config = load_config(host="gerrit.example.org", use_https=True)
        assert config.port == 29418
        assert config.discovery_method == DiscoveryMethod.HTTP

    def test_https_with_explicit_ssh_discovery_fails_fast(self):
        """Explicit SSH discovery with HTTPS raises a configuration error."""
        with pytest.raises(ConfigurationError, match="use_https requires HTTP-based"):
            load_config(
                host="gerrit.example.org",
                use_https=True,
                discovery_method="ssh",
            )

    def test_https_with_explicit_both_discovery_fails_fast(self):
        """Explicit 'both' discovery with HTTPS raises a configuration error."""
        with pytest.raises(ConfigurationError, match="use_https requires HTTP-based"):
            load_config(
                host="gerrit.example.org",
                use_https=True,
                discovery_method="both",
            )

    def test_empty_discovery_method_is_derived(self):
        """An empty discovery_method string is treated as unset and derived."""
        config = load_config(
            host="gerrit.example.org",
            use_https=True,
            discovery_method="",
        )
        assert config.discovery_method == DiscoveryMethod.HTTP


class TestPathAutoAdjustment:
    """Test path auto-adjustment for both Gerrit and GitHub sources."""

    def test_github_path_auto_adjusted_from_current_dir(self, tmp_path: Path):
        """Test that path is auto-adjusted to host when using current directory."""
        manager = ConfigManager()

        # Simulate being in tmp_path
        original_cwd = Path.cwd()
        try:
            os.chdir(tmp_path)

            # When path is the literal default ".", it should be adjusted to host
            config = manager.load_config(
                host="github.com/myorg",
                source_type=SourceType.GITHUB,
                path=Path(),  # The CLI default, equal to Path(".")
            )

            # path should be adjusted to the host value (but resolved to absolute)
            # The cwd when the config was created was tmp_path, so it becomes tmp_path / "github.com/myorg"
            assert config.path == (tmp_path / "github.com/myorg").resolve()
        finally:
            os.chdir(original_cwd)

    def test_github_enterprise_path_auto_adjusted(self, tmp_path: Path):
        """Test that path is auto-adjusted for GitHub Enterprise servers."""
        manager = ConfigManager()

        # Simulate being in tmp_path
        original_cwd = Path.cwd()
        try:
            os.chdir(tmp_path)

            # GitHub Enterprise server with org
            config = manager.load_config(
                host="github.enterprise.com/engineering",
                source_type=SourceType.GITHUB,
                path=Path(),
            )

            # Should create structure: github.enterprise.com/engineering/{PROJECT}
            # path is resolved to absolute, so it becomes tmp_path / "github.enterprise.com/engineering"
            assert (
                config.path
                == (tmp_path / "github.enterprise.com/engineering").resolve()
            )
        finally:
            os.chdir(original_cwd)

    def test_github_explicit_path_not_adjusted(self, tmp_path: Path):
        """Test that explicitly set path is not auto-adjusted."""
        manager = ConfigManager()

        # Explicitly set to a different directory
        custom_path = tmp_path / "custom" / "repos"
        config = manager.load_config(
            host="github.com/myorg",
            source_type=SourceType.GITHUB,
            path=custom_path,
        )

        # path should remain as explicitly set
        assert config.path == custom_path

    def test_github_path_creates_expected_structure(self, tmp_path: Path):
        """Test that the path structure matches commit message specification."""
        manager = ConfigManager()

        original_cwd = Path.cwd()
        try:
            os.chdir(tmp_path)

            config = manager.load_config(
                host="github.com/opennetworkinglab",
                source_type=SourceType.GITHUB,
                path=Path(),
            )

            # Verify the structure matches: {PATH}/github.com/{ORG}
            # path is resolved to absolute, so relative to where config was created (tmp_path)
            assert config.path == (tmp_path / "github.com/opennetworkinglab").resolve()

            # Simulate project clone path construction
            project_name = "my-repo"
            expected_clone_path = config.path / project_name
            assert (
                expected_clone_path
                == (tmp_path / "github.com/opennetworkinglab/my-repo").resolve()
            )
        finally:
            os.chdir(original_cwd)

    def test_gerrit_path_auto_adjusted_from_current_dir(self, tmp_path: Path):
        """Test that Gerrit path is auto-adjusted to server name when using current directory."""
        manager = ConfigManager()

        original_cwd = Path.cwd()
        try:
            os.chdir(tmp_path)

            # When path is the literal default ".", it should be adjusted to server name
            config = manager.load_config(
                host="gerrit.example.org",
                source_type=SourceType.GERRIT,
                path=Path(),
            )

            # path should be adjusted to the server name
            # Structure: ./{GERRIT_SERVER_NAME}
            assert config.path == (tmp_path / "gerrit.example.org").resolve()
        finally:
            os.chdir(original_cwd)

    def test_gerrit_path_auto_adjusted_with_port_in_host(self, tmp_path: Path):
        """Test that Gerrit path uses only hostname when host includes port."""
        manager = ConfigManager()

        original_cwd = Path.cwd()
        try:
            os.chdir(tmp_path)

            # Host with port should extract just the hostname
            config = manager.load_config(
                host="gerrit.example.org:29418",
                source_type=SourceType.GERRIT,
                path=Path(),
            )

            # path should use only hostname, not the port
            assert config.path == (tmp_path / "gerrit.example.org").resolve()
        finally:
            os.chdir(original_cwd)

    def test_gerrit_explicit_path_not_adjusted(self, tmp_path: Path):
        """Test that explicitly set Gerrit path is not auto-adjusted."""
        manager = ConfigManager()

        # Explicitly set to a different directory
        custom_path = tmp_path / "custom" / "gerrit-repos"
        config = manager.load_config(
            host="gerrit.example.org",
            source_type=SourceType.GERRIT,
            path=custom_path,
        )

        # path should remain as explicitly set
        assert config.path == custom_path

    def test_gerrit_path_creates_expected_structure(self, tmp_path: Path):
        """Test that Gerrit path structure matches specification."""
        manager = ConfigManager()

        original_cwd = Path.cwd()
        try:
            os.chdir(tmp_path)

            config = manager.load_config(
                host="gerrit.o-ran-sc.org",
                source_type=SourceType.GERRIT,
                path=Path(),
            )

            # Verify the structure matches: ./{GERRIT_SERVER_NAME}
            assert config.path == (tmp_path / "gerrit.o-ran-sc.org").resolve()

            # Simulate project clone path construction
            project_name = "test-project"
            expected_clone_path = config.path / project_name
            assert (
                expected_clone_path
                == (tmp_path / "gerrit.o-ran-sc.org/test-project").resolve()
            )
        finally:
            os.chdir(original_cwd)

    @pytest.mark.parametrize(
        ("host", "source_type"),
        [
            ("gerrit.example.org", SourceType.GERRIT),
            ("github.com/myorg", SourceType.GITHUB),
        ],
    )
    def test_absolute_cwd_path_not_adjusted(
        self, tmp_path: Path, host: str, source_type: SourceType
    ):
        """An absolute path equal to the CWD is used exactly as given."""
        manager = ConfigManager()

        original_cwd = Path.cwd()
        try:
            os.chdir(tmp_path)

            config = manager.load_config(
                host=host,
                source_type=source_type,
                path=Path.cwd(),
            )

            assert config.path == tmp_path.resolve()
        finally:
            os.chdir(original_cwd)

    @pytest.mark.parametrize(
        ("host", "source_type", "expected"),
        [
            ("gerrit.example.org", SourceType.GERRIT, "gerrit.example.org"),
            ("github.com/myorg", SourceType.GITHUB, "github.com/myorg"),
        ],
    )
    def test_no_path_yields_auto_structure(
        self, tmp_path: Path, host: str, source_type: SourceType, expected: str
    ):
        """No path from any source still yields the auto-structure."""
        manager = ConfigManager()
        manager._config_paths = []

        original_cwd = Path.cwd()
        try:
            os.chdir(tmp_path)
            with patch.dict(os.environ, {}, clear=True):
                config = manager.load_config(host=host, source_type=source_type)

            assert config.path == (tmp_path / expected).resolve()
        finally:
            os.chdir(original_cwd)

    def test_output_path_env_absolute_cwd_not_adjusted(self, tmp_path: Path):
        """OUTPUT_PATH set to an absolute path equal to the CWD is kept."""
        manager = ConfigManager()

        original_cwd = Path.cwd()
        try:
            os.chdir(tmp_path)
            with patch.dict(os.environ, {"OUTPUT_PATH": str(Path.cwd())}):
                config = manager.load_config(host="gerrit.example.org")

            assert config.path == tmp_path.resolve()
        finally:
            os.chdir(original_cwd)


# Values that are empty or a spelling of "." select the auto-structure.
DOT_SPELLINGS = ["", ".", "./", ".//", "./.", "././"]

SOURCES = [
    pytest.param("gerrit.example.org", "gerrit", "gerrit.example.org", id="gerrit"),
    pytest.param("github.com/myorg", "github", "github.com/myorg", id="github"),
]

# The CLI detects the source type from the host.
CLI_SOURCES = [
    pytest.param("gerrit.example.org", "gerrit.example.org", id="gerrit"),
    pytest.param("github.com/myorg", "github.com/myorg", id="github"),
]


def _given_path(work: Path, value: str) -> tuple[str, Path]:
    """Return the CLI/config value for *value* and the path it must yield.

    ``"<cwd>"`` stands for the absolute path of *work*.
    """
    if value == "<cwd>":
        return str(work), work.resolve()
    return value, (work / value).resolve()


# Values that are not a spelling of "." are used exactly as given, even
# when they resolve to the working directory.
GIVEN_PATHS = ["child/..", "..", ".hidden", "./my-repos", "<cwd>"]


@pytest.fixture
def work_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Run from ``tmp_path/work``, with a ``child`` directory inside it."""
    work = tmp_path / "work"
    (work / "child").mkdir(parents=True)
    monkeypatch.chdir(work)
    for name in ("OUTPUT_PATH", "GERRIT_PATH_PREFIX", "GERRIT_OUTPUT_DIR"):
        monkeypatch.delenv(name, raising=False)
    return work


def _manager() -> ConfigManager:
    """A ConfigManager that reads no config file."""
    manager = ConfigManager()
    manager._config_paths = []
    return manager


class TestOutputPathSpellings:
    """Pin which output-path values select the auto-structure."""

    @pytest.mark.parametrize("value", DOT_SPELLINGS)
    @pytest.mark.parametrize(("host", "source_type", "expected"), SOURCES)
    def test_load_config_dot_spelling_auto_structure(
        self, work_dir: Path, host: str, source_type: str, expected: str, value: str
    ):
        """An empty value or a spelling of '.' selects the auto-structure."""
        config = _manager().load_config(host=host, source_type=source_type, path=value)

        assert config.path == (work_dir / expected).resolve()

    @pytest.mark.parametrize("value", DOT_SPELLINGS)
    @pytest.mark.parametrize(("host", "source_type", "expected"), SOURCES)
    def test_env_dot_spelling_auto_structure(
        self,
        work_dir: Path,
        monkeypatch: pytest.MonkeyPatch,
        host: str,
        source_type: str,
        expected: str,
        value: str,
    ):
        """OUTPUT_PATH empty or a spelling of '.' selects the auto-structure."""
        monkeypatch.setenv("OUTPUT_PATH", value)

        config = _manager().load_config(host=host, source_type=source_type)

        assert config.path == (work_dir / expected).resolve()

    @pytest.mark.parametrize("value", GIVEN_PATHS)
    @pytest.mark.parametrize(("host", "source_type", "expected"), SOURCES)
    def test_load_config_other_value_used_as_given(
        self, work_dir: Path, host: str, source_type: str, expected: str, value: str
    ):
        """A value that is not a spelling of '.' is used as given."""
        given, resolved = _given_path(work_dir, value)

        config = _manager().load_config(host=host, source_type=source_type, path=given)

        assert config.path == resolved
        assert config.path != (work_dir / expected).resolve()


class TestCliOutputPathSpellings:
    """Pin the clone command's --output-path boundary end to end."""

    @staticmethod
    def _run_clone(host: str, output_path: str) -> Config:
        """Invoke ``clone`` with the clone itself stubbed; return its Config.

        The source type is left to auto-detection from *host*.
        """
        captured: list[Config] = []
        now = datetime.now()

        def fake_clone(config: Config) -> MagicMock:
            captured.append(config)
            return MagicMock(
                started_at=now,
                completed_at=now,
                total_count=0,
                success_count=0,
                failed_count=0,
                skipped_count=0,
                refreshed_count=0,
                results=[],
                success_rate=100.0,
            )

        with patch("source_clone.cli.clone_repositories", side_effect=fake_clone):
            result = CliRunner().invoke(
                app,
                [
                    "clone",
                    "--host",
                    host,
                    "--output-path",
                    output_path,
                    "--disable-log-file",
                    "--quiet",
                    # GitHub sources need a token to start; Gerrit ignores it.
                    "--github-token",
                    "test-token",
                ],
            )

        assert len(captured) == 1, result.output
        return captured[0]

    @pytest.mark.parametrize("value", DOT_SPELLINGS)
    @pytest.mark.parametrize(("host", "expected"), CLI_SOURCES)
    def test_cli_dot_spelling_auto_structure(
        self, work_dir: Path, host: str, expected: str, value: str
    ):
        """--output-path empty or a spelling of '.' selects the auto-structure."""
        config = self._run_clone(host, value)

        assert config.path == (work_dir / expected).resolve()

    @pytest.mark.parametrize("value", GIVEN_PATHS)
    @pytest.mark.parametrize(("host", "expected"), CLI_SOURCES)
    def test_cli_other_value_used_as_given(
        self, work_dir: Path, host: str, expected: str, value: str
    ):
        """--output-path that is not a spelling of '.' is used as given."""
        given, resolved = _given_path(work_dir, value)

        config = self._run_clone(host, given)

        assert config.path == resolved
        assert config.path != (work_dir / expected).resolve()


# Every source of the clone destination, highest precedence first. "cli"
# is --output-path and "file" is the ``path`` key of a config file.
PATH_SOURCES = ["cli", "OUTPUT_PATH", "GERRIT_PATH_PREFIX", "GERRIT_OUTPUT_DIR", "file"]

GERRIT_HOST = "gerrit.example.org"


def _apply_sources(
    sources: Mapping[str, str | None], monkeypatch: pytest.MonkeyPatch, config_dir: Path
) -> tuple[str | None, Path | None]:
    """Set each source in *sources*; return the CLI path and config file.

    No config file is read unless *sources* names ``file``, whose value
    may be ``None`` to write ``"path": null``.
    """
    monkeypatch.setenv("HOME", str(config_dir / "home"))
    for name in ("OUTPUT_PATH", "GERRIT_PATH_PREFIX", "GERRIT_OUTPUT_DIR"):
        if (value := sources.get(name)) is not None:
            monkeypatch.setenv(name, value)
    config_file = None
    if "file" in sources:
        config_file = config_dir / "config.json"
        config_file.write_text(json.dumps({"path": sources["file"]}))
    return sources.get("cli"), config_file


def _load(
    sources: Mapping[str, str | None], monkeypatch: pytest.MonkeyPatch, config_dir: Path
) -> Config:
    """Resolve *sources* through ``load_config``."""
    cli_path, config_file = _apply_sources(sources, monkeypatch, config_dir)
    return _manager().load_config(
        host=GERRIT_HOST, path=cli_path, config_file=config_file
    )


def _clone(
    sources: Mapping[str, str | None],
    monkeypatch: pytest.MonkeyPatch,
    config_dir: Path,
    *,
    log_file: bool = False,
) -> Config:
    """Resolve *sources* through ``clone`` with cloning stubbed."""
    cli_path, config_file = _apply_sources(sources, monkeypatch, config_dir)
    args = ["clone", "--host", GERRIT_HOST, "--quiet"]
    if cli_path is not None:
        args += ["--output-path", cli_path]
    if config_file is not None:
        args += ["--config-file", str(config_file)]
    if not log_file:
        args.append("--disable-log-file")
    captured: list[Config] = []
    now = datetime.now()

    def fake_clone(config: Config) -> MagicMock:
        captured.append(config)
        return MagicMock(
            started_at=now,
            completed_at=now,
            total_count=0,
            success_count=0,
            failed_count=0,
            skipped_count=0,
            refreshed_count=0,
            results=[],
            success_rate=100.0,
        )

    with patch("source_clone.cli.clone_repositories", side_effect=fake_clone):
        result = CliRunner().invoke(app, args)

    assert len(captured) == 1, result.output
    return captured[0]


ADJACENT_PAIRS = [
    pytest.param(higher, lower, value, id=f"{higher}={value}-over-{lower}")
    for higher, lower in pairwise(PATH_SOURCES)
    for value in ("high", ".")
]


def _pair_case(
    work_dir: Path, higher: str, lower: str, higher_value: str
) -> tuple[dict[str, str], Path]:
    """Return the sources for a pair and the path the higher one gives."""
    low = str(work_dir.parent / "low")
    if higher_value == ".":
        return {higher: ".", lower: low}, (work_dir / GERRIT_HOST).resolve()
    high = work_dir.parent / "high"
    return {higher: str(high), lower: low}, high.resolve()


class TestOutputPathSources:
    """Every documented output-path source works, in precedence order."""

    @pytest.fixture
    def config_dir(self, tmp_path: Path) -> Path:
        """A directory outside the working directory for config files."""
        path = tmp_path / "config"
        path.mkdir()
        return path

    @pytest.mark.parametrize("source", PATH_SOURCES)
    def test_load_config_single_source_used(
        self,
        work_dir: Path,
        monkeypatch: pytest.MonkeyPatch,
        config_dir: Path,
        source: str,
    ):
        """Each source alone sets the path through load_config."""
        target = work_dir.parent / "target"

        config = _load({source: str(target)}, monkeypatch, config_dir)

        assert config.path == target.resolve()

    @pytest.mark.parametrize("source", PATH_SOURCES)
    def test_clone_single_source_used(
        self,
        work_dir: Path,
        monkeypatch: pytest.MonkeyPatch,
        config_dir: Path,
        source: str,
    ):
        """Each source alone sets the path through clone."""
        target = work_dir.parent / "target"

        config = _clone({source: str(target)}, monkeypatch, config_dir)

        assert config.path == target.resolve()

    @pytest.mark.parametrize("value", DOT_SPELLINGS)
    @pytest.mark.parametrize("source", PATH_SOURCES)
    def test_load_config_dot_spelling_auto_structure(
        self,
        work_dir: Path,
        monkeypatch: pytest.MonkeyPatch,
        config_dir: Path,
        source: str,
        value: str,
    ):
        """Each source given an empty value or '.' yields the auto-structure."""
        config = _load({source: value}, monkeypatch, config_dir)

        assert config.path == (work_dir / GERRIT_HOST).resolve()

    @pytest.mark.parametrize("value", DOT_SPELLINGS)
    @pytest.mark.parametrize("source", PATH_SOURCES)
    def test_clone_dot_spelling_auto_structure(
        self,
        work_dir: Path,
        monkeypatch: pytest.MonkeyPatch,
        config_dir: Path,
        source: str,
        value: str,
    ):
        """Each source given an empty value or '.' yields the auto-structure."""
        config = _clone({source: value}, monkeypatch, config_dir)

        assert config.path == (work_dir / GERRIT_HOST).resolve()

    def test_load_config_nothing_set_auto_structure(
        self, work_dir: Path, monkeypatch: pytest.MonkeyPatch, config_dir: Path
    ):
        """With no source set, load_config yields the auto-structure."""
        config = _load({}, monkeypatch, config_dir)

        assert config.path == (work_dir / GERRIT_HOST).resolve()

    def test_clone_nothing_set_auto_structure(
        self, work_dir: Path, monkeypatch: pytest.MonkeyPatch, config_dir: Path
    ):
        """With no source set, clone yields the auto-structure."""
        config = _clone({}, monkeypatch, config_dir)

        assert config.path == (work_dir / GERRIT_HOST).resolve()

    def test_load_config_null_config_file_path_counts_as_unset(
        self, work_dir: Path, monkeypatch: pytest.MonkeyPatch, config_dir: Path
    ):
        """A config file holding 'path: null' yields the auto-structure."""
        config = _load({"file": None}, monkeypatch, config_dir)

        assert config.path == (work_dir / GERRIT_HOST).resolve()

    def test_clone_null_config_file_path_counts_as_unset(
        self, work_dir: Path, monkeypatch: pytest.MonkeyPatch, config_dir: Path
    ):
        """A config file holding 'path: null' yields the auto-structure."""
        config = _clone({"file": None}, monkeypatch, config_dir)

        assert config.path == (work_dir / GERRIT_HOST).resolve()

    @pytest.mark.parametrize(("higher", "lower", "higher_value"), ADJACENT_PAIRS)
    def test_load_config_higher_source_wins(
        self,
        work_dir: Path,
        monkeypatch: pytest.MonkeyPatch,
        config_dir: Path,
        higher: str,
        lower: str,
        higher_value: str,
    ):
        """The higher of two adjacent sources decides the path."""
        sources, expected = _pair_case(work_dir, higher, lower, higher_value)

        config = _load(sources, monkeypatch, config_dir)

        assert config.path == expected

    @pytest.mark.parametrize(("higher", "lower", "higher_value"), ADJACENT_PAIRS)
    def test_clone_higher_source_wins(
        self,
        work_dir: Path,
        monkeypatch: pytest.MonkeyPatch,
        config_dir: Path,
        higher: str,
        lower: str,
        higher_value: str,
    ):
        """The higher of two adjacent sources decides the path."""
        sources, expected = _pair_case(work_dir, higher, lower, higher_value)

        config = _clone(sources, monkeypatch, config_dir)

        assert config.path == expected

    @pytest.mark.parametrize("lower", PATH_SOURCES[2:])
    def test_clone_empty_output_path_env_counts_as_unset(
        self,
        work_dir: Path,
        monkeypatch: pytest.MonkeyPatch,
        config_dir: Path,
        lower: str,
    ):
        """An empty OUTPUT_PATH defers to the next source that is set."""
        target = work_dir.parent / "target"

        config = _clone(
            {"OUTPUT_PATH": "", lower: str(target)}, monkeypatch, config_dir
        )

        assert config.path == target.resolve()

    @pytest.mark.parametrize("value", [".", ""])
    def test_clone_dot_output_path_as_action_passes_it(
        self,
        work_dir: Path,
        monkeypatch: pytest.MonkeyPatch,
        config_dir: Path,
        value: str,
    ):
        """--output-path '.' or '' overrides every lower source, as before."""
        low = str(work_dir.parent / "low")
        sources = {
            "cli": value,
            "GERRIT_PATH_PREFIX": low,
            "GERRIT_OUTPUT_DIR": low,
            "file": low,
        }

        config = _clone(sources, monkeypatch, config_dir)

        assert config.path == (work_dir / GERRIT_HOST).resolve()

    @pytest.mark.parametrize(
        "sources", [(), ("GERRIT_PATH_PREFIX",), ("GERRIT_OUTPUT_DIR",), ("file",)]
    )
    def test_clone_default_log_file_stays_in_working_directory(
        self,
        work_dir: Path,
        monkeypatch: pytest.MonkeyPatch,
        config_dir: Path,
        sources: tuple[str, ...],
    ):
        """Omitting --output-path keeps the log in the working directory."""
        target = work_dir.parent / "target"

        _clone(
            {s: str(target) for s in sources}, monkeypatch, config_dir, log_file=True
        )

        assert (work_dir / f"{GERRIT_HOST}.log").is_file()
        assert not (target / f"{GERRIT_HOST}.log").exists()


class TestGitHubBaseUrl:
    """GitHub API base-URL derivation in Config.__post_init__."""

    def test_plain_github_com(self) -> None:
        """A bare github.com host resolves to api.github.com."""
        config = Config(host="github.com", source_type=SourceType.GITHUB)
        assert config.base_url == "https://api.github.com"

    def test_github_com_with_org_suffix(self) -> None:
        """github.com/ORG still resolves to api.github.com, not /api/v3."""
        config = Config(host="github.com/myorg", source_type=SourceType.GITHUB)
        assert config.base_url == "https://api.github.com"

    def test_github_com_with_scheme_and_org(self) -> None:
        """A scheme prefix and org suffix are both stripped before match."""
        config = Config(host="https://github.com/myorg", source_type=SourceType.GITHUB)
        assert config.base_url == "https://api.github.com"

    def test_enterprise_host_with_org(self) -> None:
        """A GitHub Enterprise host yields a bare-host /api/v3 URL."""
        config = Config(
            host="github.enterprise.com/engineering",
            source_type=SourceType.GITHUB,
        )
        assert config.base_url == "https://github.enterprise.com/api/v3"

    def test_lookalike_host_not_treated_as_github_com(self) -> None:
        """A lookalike host is treated as enterprise, not api.github.com."""
        config = Config(host="github.com.evil.example", source_type=SourceType.GITHUB)
        assert config.base_url == "https://github.com.evil.example/api/v3"

    def test_enterprise_host_preserves_http_scheme(self) -> None:
        """An http:// scheme on an enterprise host is preserved."""
        config = Config(
            host="http://ghe.internal",
            source_type=SourceType.GITHUB,
        )
        assert config.base_url == "http://ghe.internal/api/v3"

    def test_enterprise_host_preserves_https_scheme_with_org(self) -> None:
        """An explicit https:// scheme with an org suffix is preserved."""
        config = Config(
            host="https://ghe.internal/engineering",
            source_type=SourceType.GITHUB,
        )
        assert config.base_url == "https://ghe.internal/api/v3"
