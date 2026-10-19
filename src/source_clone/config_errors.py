# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""Configuration error type shared by the configuration modules.

Lives on its own so :mod:`source_clone.config_env` can raise it without
importing :mod:`source_clone.config`, which imports the loader back.
"""

from __future__ import annotations

__all__ = ["ConfigurationError"]


class ConfigurationError(Exception):
    """Raised when configuration is invalid or cannot be loaded."""
