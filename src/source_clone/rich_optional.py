# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""Guarded Rich imports shared by the progress modules.

The drawing code checks ``RICH_AVAILABLE`` before constructing any Rich
widget and falls back to plain text when it is false. Performing that
guarded import once, here, keeps the decision in a single place instead
of repeating it in every module that draws something, and keeps
``RICH_AVAILABLE`` consistent across them.

The guard covers the drawing code alone. Rich is a declared, hard
dependency and several other modules -- ``source_clone.logging`` among
them -- import it unguarded, so the package as a whole still requires
it. Reconciling that is worth doing, but not in a refactor.

The ``TYPE_CHECKING`` branch binds the same names unconditionally. A
type checker cannot know the import succeeded, so without it every use
site reads as possibly unbound; at runtime only the guarded branch below
ever executes.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from rich.console import Console, Group
    from rich.live import Live
    from rich.panel import Panel
    from rich.progress import (
        BarColumn,
        MofNCompleteColumn,
        Progress,
        SpinnerColumn,
        TextColumn,
    )
    from rich.table import Table
    from rich.text import Text

    RICH_AVAILABLE = True
else:
    try:
        from rich.console import Console, Group
        from rich.live import Live
        from rich.panel import Panel
        from rich.progress import (
            BarColumn,
            MofNCompleteColumn,
            Progress,
            SpinnerColumn,
            TextColumn,
        )
        from rich.table import Table
        from rich.text import Text

        RICH_AVAILABLE = True
    except ImportError:
        # Bind every exported name so this module, and the modules that
        # import from it, still import cleanly without Rich. Leaving them
        # unbound turns a missing optional dependency into an ImportError
        # at package import time, which defeats the fallback entirely.
        # Every use site is guarded by RICH_AVAILABLE.
        BarColumn = None
        Console = None
        Group = None
        Live = None
        MofNCompleteColumn = None
        Panel = None
        Progress = None
        SpinnerColumn = None
        Table = None
        Text = None
        TextColumn = None
        RICH_AVAILABLE = False

__all__ = [
    "RICH_AVAILABLE",
    "BarColumn",
    "Console",
    "Group",
    "Live",
    "MofNCompleteColumn",
    "Panel",
    "Progress",
    "SpinnerColumn",
    "Table",
    "Text",
    "TextColumn",
]
