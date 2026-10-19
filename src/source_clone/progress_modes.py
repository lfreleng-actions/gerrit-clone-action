# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""Progress display modes.

Leaf module defining the display strategies a progress tracker can run in.
Kept separate so the rendering and lifecycle helpers can depend on the mode
enumeration without importing the tracker itself.
"""

from __future__ import annotations

from enum import Enum


class ProgressMode(Enum):
    """Progress display modes."""

    RICH_PERIODIC = "rich_periodic"  # Rich UI with periodic updates (no Live)
    RICH_SIMPLE = "rich_simple"  # Simple Rich progress without Live
    TEXT_ONLY = "text_only"  # Plain text logging only
    DISABLED = "disabled"  # No progress display
