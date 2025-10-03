# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2025 Matthew Watkins <mwatkins@linuxfoundation.org>

"""Configuration management for Gerrit clone operations."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import yaml

from gerrit_clone.models import Config, RetryPolicy


class ConfigurationError(Exception):
    """Raised when configuration is invalid or cannot be loaded."""


class ConfigManager:
    """Manages configuration from multiple sources with precedence."""

    def __init__(self) -> None:
        """Initialize configuration manager."""
        self._config_paths = [
            Path.home() / ".config" / "gerrit-clone" / "config.yaml",
            Path.home() / ".config" / "gerrit-clone" / "config.json",
        ]

    def load_config(
        self,
        host: str | None = None,
        port: int | None = None,
        base_url: str | None = None,
        ssh_user: str | None = None,
        ssh_identity_file: str | Path | None = None,
        path_prefix: str | Path | None = None,
        skip_archived: bool | None = None,
        allow_nested_git: bool | None = None,
        nested_protection: bool | None = None,
        move_conflicting: bool | None = None,
        threads: int | None = None,
        depth: int | None = None,
        branch: str | None = None,
        use_https: bool | None = None,
        keep_remote_protocol: bool | None = None,
        strict_host_checking: bool | None = None,
        clone_timeout: int | None = None,
        retry_attempts: int | None = None,
        retry_base_delay: float | None = None,
        retry_factor: float | None = None,
        retry_max_delay: float | None = None,
        manifest_filename: str | None = None,
        verbose: bool | None = None,
        quiet: bool | None = None,
        config_file: str | Path | None = None,
        include_projects: str | list[str] | None = None,
        exclude_projects: str | list[str] | None = None,
        ssh_debug: bool | None = None,
        exit_on_error: bool | None = None,
    ) -> Config:
        """Load configuration from all sources with precedence.

        Precedence order: CLI args > Environment variables > Config file > Defaults

        Args:
            host: Gerrit server hostname
            port: Gerrit SSH port
            base_url: Base URL for Gerrit API (overrides host-based default)
            ssh_user: SSH username
            ssh_identity_file: SSH private key file for authentication
            path_prefix: Base directory for clones
            skip_archived: Skip non-active repositories
            allow_nested_git: Permit nested git working trees
            nested_protection: Auto-add nested child paths to parent .git/info/exclude
            move_conflicting: Move conflicting files/directories in parent repos to [NAME].parent
            threads: Number of concurrent clone threads
            depth: Git clone depth (shallow clone)
            branch: Specific branch to clone
            use_https: Use HTTPS for cloning instead of SSH
            keep_remote_protocol: Keep original clone protocol for remote
            strict_host_checking: Enforce strict SSH host key checking
            clone_timeout: Timeout per clone operation in seconds
            retry_attempts: Maximum retry attempts per repository
            retry_base_delay: Base delay for retry backoff
            retry_factor: Exponential backoff factor
            retry_max_delay: Maximum retry delay
            manifest_filename: Output manifest filename
            verbose: Enable verbose logging
            quiet: Suppress non-error output
            config_file: Explicit config file path
            include_projects: Optional list of project names to clone (filters all projects)
            ssh_debug: Enable verbose SSH debugging (-vvv) for authentication issues
            exit_on_error: Exit immediately when the first clone error occurs

        Returns:
            Configured Config object

        Raises:
            ConfigurationError: If configuration is invalid or missing required values
        """
        # Load file configuration first (lowest precedence)
        file_config = self._load_file_config(config_file)

        # Load environment variables (medium precedence)
        env_config = self._load_env_config()

        # CLI arguments (highest precedence) - passed as parameters
        cli_config = self._build_cli_config(
            host=host,
            port=port,
            base_url=base_url,
            ssh_user=ssh_user,
            ssh_identity_file=ssh_identity_file,
            path_prefix=path_prefix,
            skip_archived=skip_archived,

            allow_nested_git=allow_nested_git,
            nested_protection=nested_protection,
            move_conflicting=move_conflicting,
            threads=threads,
            depth=depth,
            branch=branch,
            use_https=use_https,
            keep_remote_protocol=keep_remote_protocol,
            strict_host_checking=strict_host_checking,
            clone_timeout=clone_timeout,
            retry_attempts=retry_attempts,
            retry_base_delay=retry_base_delay,
            retry_factor=retry_factor,
            retry_max_delay=retry_max_delay,
            manifest_filename=manifest_filename,
            verbose=verbose,
            quiet=quiet,
            include_projects=include_projects,
            exclude_projects=exclude_projects,
            ssh_debug=ssh_debug,
            exit_on_error=exit_on_error,
        )

        # Merge configurations with precedence
        merged = self._merge_configs(file_config, env_config, cli_config)

        # Build and validate final configuration
        return self._build_config(merged)

    def _load_file_config(
        self, config_file: str | Path | None = None
    ) -> dict[str, Any]:
        """Load configuration from file."""
        if config_file is not None:
            # Explicit config file specified
            config_path = Path(config_file)
            if not config_path.exists():
                raise ConfigurationError(f"Config file not found: {config_path}")
            return self._parse_config_file(config_path)

        # Try default config file locations
        for config_path in self._config_paths:
            if config_path.exists():
                return self._parse_config_file(config_path)

        # No config file found - return empty dict
        return {}

    def _parse_config_file(self, config_path: Path) -> dict[str, Any]:
        """Parse configuration file (YAML or JSON)."""
        try:
            content = config_path.read_text(encoding="utf-8")

            if config_path.suffix.lower() in (".yaml", ".yml"):
                result = yaml.safe_load(content)
                return result if isinstance(result, dict) else {}
            elif config_path.suffix.lower() == ".json":
                result = json.loads(content)
                return result if isinstance(result, dict) else {}
            else:
                raise ConfigurationError(
                    f"Unsupported config file format: {config_path.suffix}"
                )

        except (yaml.YAMLError, json.JSONDecodeError) as e:
            raise ConfigurationError(
                f"Error parsing config file {config_path}: {e}"
            ) from e
        except OSError as e:
            raise ConfigurationError(
                f"Error reading config file {config_path}: {e}"
            ) from e

    def _load_env_config(self) -> dict[str, Any]:
        """Load configuration from environment variables."""
        config: dict[str, Any] = {}

        # Load each configuration section
        self._load_connection_env_vars(config)
        self._load_clone_behavior_env_vars(config)
        self._load_security_env_vars(config)
        self._load_retry_env_vars(config)
        self._load_debug_env_vars(config)

        return config

    def _load_connection_env_vars(self, config: dict[str, Any]) -> None:
        """Load connection-related environment variables."""
        if host := os.getenv("GERRIT_HOST"):
            config["host"] = host
        if port_str := os.getenv("GERRIT_PORT"):
            config["port"] = self._parse_int(port_str, "GERRIT_PORT")
        if base_url := os.getenv("GERRIT_BASE_URL"):
            config["base_url"] = base_url
        if ssh_user := os.getenv("GERRIT_SSH_USER"):
            config["ssh_user"] = ssh_user
        if ssh_identity_file := os.getenv("GERRIT_SSH_PRIVATE_KEY"):
            config["ssh_identity_file"] = ssh_identity_file

        # Path settings (support both new and legacy env var names)
        if path_prefix := (
            os.getenv("GERRIT_PATH_PREFIX") or os.getenv("GERRIT_OUTPUT_DIR")
        ):
            config["path_prefix"] = path_prefix

    def _load_clone_behavior_env_vars(self, config: dict[str, Any]) -> None:
        """Load clone behavior environment variables."""
        if skip_archived_str := os.getenv("GERRIT_SKIP_ARCHIVED"):
            config["skip_archived"] = self._parse_bool(
                skip_archived_str, "GERRIT_SKIP_ARCHIVED"
            )

        if allow_nested_git_str := os.getenv("GERRIT_ALLOW_NESTED_GIT"):
            config["allow_nested_git"] = self._parse_bool(
                allow_nested_git_str, "GERRIT_ALLOW_NESTED_GIT"
            )
        if nested_protection_str := os.getenv("GERRIT_NESTED_PROTECTION"):
            config["nested_protection"] = self._parse_bool(
                nested_protection_str, "GERRIT_NESTED_PROTECTION"
            )
        if move_conflicting_str := os.getenv("GERRIT_MOVE_CONFLICTING"):
            config["move_conflicting"] = self._parse_bool(
                move_conflicting_str, "GERRIT_MOVE_CONFLICTING"
            )
        if threads_str := os.getenv("GERRIT_THREADS"):
            config["threads"] = self._parse_int(threads_str, "GERRIT_THREADS")
        if depth_str := os.getenv("GERRIT_CLONE_DEPTH"):
            config["depth"] = self._parse_int(depth_str, "GERRIT_CLONE_DEPTH")
        if branch := os.getenv("GERRIT_BRANCH"):
            config["branch"] = branch
        if use_https_str := os.getenv("GERRIT_USE_HTTPS"):
            config["use_https"] = self._parse_bool(use_https_str, "GERRIT_USE_HTTPS")
        if keep_remote_protocol_str := os.getenv("GERRIT_KEEP_REMOTE_PROTOCOL"):
            config["keep_remote_protocol"] = self._parse_bool(
                keep_remote_protocol_str, "GERRIT_KEEP_REMOTE_PROTOCOL"
            )

    def _load_security_env_vars(self, config: dict[str, Any]) -> None:
        """Load security-related environment variables."""
        if strict_host_str := os.getenv("GERRIT_STRICT_HOST"):
            config["strict_host_checking"] = self._parse_bool(
                strict_host_str, "GERRIT_STRICT_HOST"
            )
        if clone_timeout_str := os.getenv("GERRIT_CLONE_TIMEOUT"):
            config["clone_timeout"] = self._parse_int(
                clone_timeout_str, "GERRIT_CLONE_TIMEOUT"
            )

    def _load_retry_env_vars(self, config: dict[str, Any]) -> None:
        """Load retry-related environment variables."""
        if retry_attempts_str := os.getenv("GERRIT_RETRY_ATTEMPTS"):
            config["retry_attempts"] = self._parse_int(
                retry_attempts_str, "GERRIT_RETRY_ATTEMPTS"
            )
        if retry_base_delay_str := os.getenv("GERRIT_RETRY_BASE_DELAY"):
            config["retry_base_delay"] = self._parse_float(
                retry_base_delay_str, "GERRIT_RETRY_BASE_DELAY"
            )
        if retry_factor_str := os.getenv("GERRIT_RETRY_FACTOR"):
            config["retry_factor"] = self._parse_float(
                retry_factor_str, "GERRIT_RETRY_FACTOR"
            )
        if retry_max_delay_str := os.getenv("GERRIT_RETRY_MAX_DELAY"):
            config["retry_max_delay"] = self._parse_float(
                retry_max_delay_str, "GERRIT_RETRY_MAX_DELAY"
            )

    def _load_debug_env_vars(self, config: dict[str, Any]) -> None:
        """Load debugging-related environment variables."""
        if ssh_debug_str := os.getenv("GERRIT_SSH_DEBUG"):
            config["ssh_debug"] = self._parse_bool(ssh_debug_str, "GERRIT_SSH_DEBUG")

        # Support both new and old environment variable names for exit_on_error
        if exit_on_error_str := (
            os.getenv("GERRIT_EXIT_ON_ERROR") or os.getenv("GERRIT_STOP_ON_FIRST_ERROR")
        ):
            config["exit_on_error"] = self._parse_bool(exit_on_error_str, "GERRIT_EXIT_ON_ERROR")

    def _build_cli_config(self, **kwargs: Any) -> dict[str, Any]:
        """Build configuration dict from CLI arguments."""
        config = {}

        for key, value in kwargs.items():
            if value is not None:
                config[key] = value

        return config

    def _merge_configs(self, *configs: dict[str, Any]) -> dict[str, Any]:
        """Merge multiple configuration dictionaries with precedence."""
        merged = {}

        for config in configs:
            merged.update(config)

        return merged

    def _build_config(self, config_dict: dict[str, Any]) -> Config:
        """Build Config object from merged configuration dictionary."""
        # Extract retry policy settings
        retry_config = {}
        if "retry_attempts" in config_dict:
            retry_config["max_attempts"] = config_dict.pop("retry_attempts")
        if "retry_base_delay" in config_dict:
            retry_config["base_delay"] = config_dict.pop("retry_base_delay")
        if "retry_factor" in config_dict:
            retry_config["factor"] = config_dict.pop("retry_factor")
        if "retry_max_delay" in config_dict:
            retry_config["max_delay"] = config_dict.pop("retry_max_delay")

        # Create retry policy
        retry_policy = RetryPolicy(**retry_config) if retry_config else RetryPolicy()

        # Handle path_prefix conversion
        if "path_prefix" in config_dict:
            config_dict["path_prefix"] = Path(config_dict["path_prefix"])

        # Handle ssh_identity_file conversion
        if "ssh_identity_file" in config_dict:
            config_dict["ssh_identity_file"] = Path(config_dict["ssh_identity_file"])

        # Smart port defaulting based on protocol
        use_https = config_dict.get("use_https", False)
        if "port" not in config_dict:
            # No port specified, use protocol-appropriate default
            config_dict["port"] = 443 if use_https else 29418
        elif config_dict["port"] == 29418 and use_https:
            # SSH port specified but using HTTPS, switch to HTTPS port
            config_dict["port"] = 443

        # Validate required fields
        if "host" not in config_dict:
            raise ConfigurationError(
                "host is required (set via --host, GERRIT_HOST, or config file)"
            )

        try:
            return Config(retry_policy=retry_policy, **config_dict)
        except (ValueError, TypeError) as e:
            raise ConfigurationError(f"Invalid configuration: {e}") from e

    def _parse_bool(self, value: str, env_var: str) -> bool:
        """Parse boolean value from string."""
        if value.lower() in ("1", "true", "yes", "on"):
            return True
        elif value.lower() in ("0", "false", "no", "off"):
            return False
        else:
            raise ConfigurationError(f"Invalid boolean value for {env_var}: {value}")

    def _parse_int(self, value: str, env_var: str) -> int:
        """Parse integer value from string."""
        try:
            return int(value)
        except ValueError as e:
            raise ConfigurationError(
                f"Invalid integer value for {env_var}: {value}"
            ) from e

    def _parse_float(self, value: str, env_var: str) -> float:
        """Parse float value from string."""
        try:
            return float(value)
        except ValueError as e:
            raise ConfigurationError(
                f"Invalid float value for {env_var}: {value}"
            ) from e


def load_config(**kwargs: Any) -> Config:
    """Convenience function to load configuration."""
    manager = ConfigManager()
    return manager.load_config(**kwargs)
