# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2025 Matthew Watkins <mwatkins@linuxfoundation.org>

"""Configuration management for Gerrit clone operations."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import yaml

from source_clone.config_env import EnvConfigLoader
from source_clone.config_errors import ConfigurationError
from source_clone.models import Config, DiscoveryMethod, RetryPolicy, SourceType
from source_clone.ssh_identity import KEY_CONTENT_HINT, looks_like_key_content

__all__ = ["ConfigManager", "ConfigurationError", "load_config"]


class ConfigManager(EnvConfigLoader):
    """Manages configuration from multiple sources with precedence."""

    def __init__(self) -> None:
        """Initialize configuration manager."""
        # ~/.config/gerrit-clone/ is the directory earlier releases read,
        # kept as a fallback so existing configuration keeps working.
        self._config_paths = [
            Path.home() / ".config" / name / filename
            for name in ("source-clone", "gerrit-clone")
            for filename in ("config.yaml", "config.json")
        ]

    def load_config(
        self,
        host: str | None = None,
        port: int | None = None,
        base_url: str | None = None,
        ssh_user: str | None = None,
        ssh_identity_file: str | Path | None = None,
        path: str | Path | None = None,
        skip_archived: bool | None = None,
        discovery_method: str | DiscoveryMethod | None = None,
        allow_nested_git: bool | None = None,
        nested_protection: bool | None = None,
        move_conflicting: bool | None = None,
        threads: int | None = None,
        depth: int | None = None,
        branch: str | None = None,
        mirror: bool | None = None,
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
        source_type: str | None = None,
        github_token: str | None = None,
        github_org: str | None = None,
        use_gh_cli: bool | None = None,
        auto_refresh: bool | None = None,
        force_refresh: bool | None = None,
        fetch_only: bool | None = None,
        skip_conflicts: bool | None = None,
    ) -> Config:
        """Load configuration from all sources with precedence.

        Precedence order: CLI args > Environment variables > Config file > Defaults

        Args:
            host: Gerrit server hostname
            port: Gerrit SSH port
            base_url: Base URL for Gerrit API (overrides host-based default)
            ssh_user: SSH username
            ssh_identity_file: SSH private key file for authentication
            path: Base directory for clones
            skip_archived: Skip non-active repositories
            discovery_method: Method for discovering projects (ssh/http/both)
            allow_nested_git: Permit nested git working trees
            nested_protection: Auto-add nested child paths to parent .git/info/exclude
            move_conflicting: Move conflicting files/directories in parent repos to [NAME].parent
            threads: Number of concurrent clone threads
            depth: Git clone depth (shallow clone)
            branch: Specific branch to clone
            mirror: Use git clone --mirror for complete metadata (default: True)
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
            include_projects: Optional list of project name patterns to include.
                Supports shell-style wildcards (*, ?, [seq]) and hierarchical
                matching. Must be provided as a list of pattern strings.
            exclude_projects: Optional list of project name patterns to exclude.
                Applied after include filters. Uses the same pattern syntax as
                include_projects and must be provided as a list of pattern strings.
            ssh_debug: Enable verbose SSH debugging (-vvv) for authentication issues
            exit_on_error: Exit immediately when the first clone error occurs
            source_type: Source type (gerrit or github)
            github_token: GitHub personal access token
            github_org: GitHub organization or user name
            use_gh_cli: Use GitHub CLI for cloning
            auto_refresh: Auto-refresh existing repositories during clone (default: True)
            force_refresh: Force refresh with stash and detached HEAD fixes
            fetch_only: Only fetch changes without merging
            skip_conflicts: Skip repositories with uncommitted changes

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
            path=path,
            skip_archived=skip_archived,
            discovery_method=discovery_method,
            allow_nested_git=allow_nested_git,
            nested_protection=nested_protection,
            move_conflicting=move_conflicting,
            threads=threads,
            depth=depth,
            branch=branch,
            mirror=mirror,
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
            source_type=source_type,
            github_token=github_token,
            github_org=github_org,
            use_gh_cli=use_gh_cli,
            auto_refresh=auto_refresh,
            force_refresh=force_refresh,
            fetch_only=fetch_only,
            skip_conflicts=skip_conflicts,
        )

        # Merge configurations with precedence
        merged = self._merge_configs(file_config, env_config, cli_config)

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
        retry_config = {}
        if "retry_attempts" in config_dict:
            retry_config["max_attempts"] = config_dict.pop("retry_attempts")
        if "retry_base_delay" in config_dict:
            retry_config["base_delay"] = config_dict.pop("retry_base_delay")
        if "retry_factor" in config_dict:
            retry_config["factor"] = config_dict.pop("retry_factor")
        if "retry_max_delay" in config_dict:
            retry_config["max_delay"] = config_dict.pop("retry_max_delay")

        retry_policy = RetryPolicy(**retry_config) if retry_config else RetryPolicy()

        # A config file may hold "path: null"; treat it as unset.
        if config_dict.get("path") is None:
            config_dict.pop("path", None)
        else:
            config_dict["path"] = Path(config_dict["path"])

        if "ssh_identity_file" in config_dict:
            identity = config_dict["ssh_identity_file"]
            if looks_like_key_content(str(identity)):
                raise ConfigurationError(
                    f"ssh_identity_file (GERRIT_SSH_PRIVATE_KEY) {KEY_CONTENT_HINT}"
                )
            config_dict["ssh_identity_file"] = Path(identity)

        # Handle discovery_method conversion. An empty or whitespace value is
        # treated as unset so Config can derive it from the source type and
        # clone protocol; drop it here rather than failing validation.
        if "discovery_method" in config_dict:
            dm = config_dict["discovery_method"]
            if isinstance(dm, str):
                if dm.strip():
                    try:
                        config_dict["discovery_method"] = DiscoveryMethod(
                            dm.strip().lower()
                        )
                    except ValueError as err:
                        raise ConfigurationError(
                            f"Invalid discovery_method '{dm}'. Must be one of: ssh, http, both, github_api"
                        ) from err
                else:
                    config_dict.pop("discovery_method")

        if "source_type" in config_dict:
            st = config_dict["source_type"]
            if isinstance(st, str):
                try:
                    config_dict["source_type"] = SourceType(st.lower())
                except ValueError as err:
                    raise ConfigurationError(
                        f"Invalid source_type '{st}'. Must be one of: gerrit, github"
                    ) from err

        # Source-type-aware port and path defaulting
        source_type = config_dict.get("source_type", SourceType.GERRIT)

        # Auto-adjust path to include server/org structure when using the default.
        # This prevents naming conflicts when multiple clone operations run in the same directory.
        # The auto-structure applies when no source sets a path, or when the
        # value is empty or a spelling of "." ("." components and slashes with
        # no leading slash, such as "./" or "./."); Path() equals exactly those.
        # Any other value is used as given, including "..", "child/.." and an
        # absolute path, even when it names the current directory.
        if "host" in config_dict:
            host = config_dict["host"]
            path = config_dict.get("path")

            if path is None or path == Path():
                if source_type == SourceType.GITHUB:
                    # GitHub: ./github.com/{ORG} or ./{ENTERPRISE_SERVER}/{ORG}
                    # E.g., host="github.com/opennetworkinglab" -> path="./github.com/opennetworkinglab"
                    # Or: host="github.enterprise.com/org" -> path="./github.enterprise.com/org"
                    config_dict["path"] = Path(host)
                elif source_type == SourceType.GERRIT:
                    # Gerrit: ./{GERRIT_SERVER_NAME}
                    # E.g., host="gerrit.example.org" -> path="./gerrit.example.org"
                    # Extract just the hostname (without port if present)
                    server_name = host.split(":")[0] if ":" in host else host
                    config_dict["path"] = Path(server_name)

        # For GitHub sources, port should always be None (not used)
        if source_type == SourceType.GITHUB:
            # User may have set port for GitHub - remove it (not meaningful)
            config_dict.pop("port", None)
        elif "port" not in config_dict:
            # Default to the Gerrit SSH port. ``port`` is the SSH port only:
            # HTTPS discovery and cloning use ``base_url``, never this port, so
            # it is intentionally left unchanged when use_https is set. This
            # prevents SSH discovery from ever targeting the HTTPS port (443).
            config_dict["port"] = 29418

        # Coerce string include/exclude_projects to single-element lists
        # so Config.__post_init__ can safely iterate and normalize them.
        for key in ("include_projects", "exclude_projects"):
            val = config_dict.get(key)
            if isinstance(val, str):
                config_dict[key] = [val]

        if "host" not in config_dict:
            raise ConfigurationError(
                "host is required (set via --host, GERRIT_HOST, or config file)"
            )

        try:
            return Config(retry_policy=retry_policy, **config_dict)
        except (ValueError, TypeError) as e:
            raise ConfigurationError(f"Invalid configuration: {e}") from e


def load_config(**kwargs: Any) -> Config:
    """Convenience function to load configuration."""
    manager = ConfigManager()
    return manager.load_config(**kwargs)
