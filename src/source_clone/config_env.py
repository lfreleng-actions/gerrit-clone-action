# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""Configuration sourced from environment variables.

Reads the ``GERRIT_*`` variables into a plain mapping and converts the
raw strings to the scalar types the configuration expects, reporting a
:class:`~source_clone.config_errors.ConfigurationError` naming the
offending variable when a value will not convert.

Separated from :mod:`source_clone.config` so the precedence and merging
logic there is not buried under the per-variable plumbing.
"""

from __future__ import annotations

import os
from typing import Any

from source_clone.config_errors import ConfigurationError


class EnvConfigLoader:
    """Reads and converts the ``GERRIT_*`` environment variables."""

    def _load_env_config(self) -> dict[str, Any]:
        """Load configuration from environment variables."""
        config: dict[str, Any] = {}

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
        if path := (
            os.getenv("OUTPUT_PATH")
            or os.getenv("GERRIT_PATH_PREFIX")
            or os.getenv("GERRIT_OUTPUT_DIR")
        ):
            config["path"] = path

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
        if mirror_str := os.getenv("GERRIT_MIRROR"):
            config["mirror"] = self._parse_bool(mirror_str, "GERRIT_MIRROR")
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
            config["exit_on_error"] = self._parse_bool(
                exit_on_error_str, "GERRIT_EXIT_ON_ERROR"
            )

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
