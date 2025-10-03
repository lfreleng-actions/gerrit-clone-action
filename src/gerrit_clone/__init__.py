# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2025 Matthew Watkins <mwatkins@linuxfoundation.org>

"""Gerrit Clone - A multi-threaded CLI tool for bulk cloning Gerrit repositories."""

try:
    from importlib.metadata import version
    __version__ = version("gerrit-clone")
except ImportError:
    # Fallback for development/editable installs
    __version__ = "0.0.0+dev"

__author__ = "Matthew Watkins"
__email__ = "mwatkins@linuxfoundation.org"

from gerrit_clone.models import CloneResult, Config, Project, RetryPolicy

__all__ = [
    "__version__",
    "__author__",
    "__email__",
    "CloneResult",
    "Config",
    "Project",
    "RetryPolicy",
]
