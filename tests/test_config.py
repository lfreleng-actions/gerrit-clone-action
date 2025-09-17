# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2025 Matthew Watkins <mwatkins@linuxfoundation.org>

"""Unit tests for configuration management."""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest
import yaml

from gerrit_clone.config import ConfigManager, ConfigurationError, load_config
from gerrit_clone.models import Config


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
            path_prefix="/tmp/repos",
            skip_archived=False,
            threads=8,
            depth=10,
            branch="main",
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
        assert config.path_prefix == Path("/tmp/repos").resolve()
        assert config.skip_archived is False
        assert config.threads == 8
        assert config.depth == 10
        assert config.branch == "main"
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
        assert config.strict_host_checking is False
        assert config.clone_timeout == 900
        assert config.retry_policy.max_attempts == 4
        assert config.retry_policy.base_delay == 3.0

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

        assert config.path_prefix == Path("/legacy/path").resolve()

    @patch.dict(
        os.environ,
        {
            "GERRIT_HOST": "env.gerrit.org",
            "GERRIT_PATH_PREFIX": "/new/path",
            "GERRIT_OUTPUT_DIR": "/legacy/path",  # Should be overridden
        },
    )
    def test_load_config_new_over_legacy_env_var(self):
        """Test new env var takes precedence over legacy."""
        manager = ConfigManager()
        config = manager.load_config()

        assert config.path_prefix == Path("/new/path").resolve()

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
