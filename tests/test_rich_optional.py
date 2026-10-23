# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""Tests for the guarded Rich import surface.

``rich_optional`` exists so the progress display can degrade to plain
text when Rich cannot be imported. That only works if the module binds
every name it exports on the failure path: leaving them unbound turns a
missing optional dependency into an ``ImportError`` at package import
time, which is exactly what the fallback is meant to avoid.
"""

from __future__ import annotations

import builtins
import importlib
import sys
from contextlib import contextmanager
from typing import TYPE_CHECKING, Any

from source_clone import rich_optional

if TYPE_CHECKING:
    from collections.abc import Generator
    from types import ModuleType

_MODULE = "source_clone.rich_optional"


@contextmanager
def rich_unavailable() -> Generator[ModuleType]:
    """Re-import :mod:`source_clone.rich_optional` with Rich blocked."""
    real_import = builtins.__import__

    def fake_import(name: str, *args: Any, **kwargs: Any) -> Any:
        if name == "rich" or name.startswith("rich."):
            raise ImportError(f"blocked for test: {name}")
        return real_import(name, *args, **kwargs)

    saved = sys.modules.pop(_MODULE, None)
    builtins.__import__ = fake_import
    try:
        yield importlib.import_module(_MODULE)
    finally:
        builtins.__import__ = real_import
        sys.modules.pop(_MODULE, None)
        if saved is not None:
            sys.modules[_MODULE] = saved


class TestRichOptional:
    """The exported surface must survive Rich being absent."""

    def test_every_exported_name_is_bound_without_rich(self) -> None:
        with rich_unavailable() as module:
            assert module.RICH_AVAILABLE is False
            missing = [n for n in module.__all__ if not hasattr(module, n)]
            assert not missing, f"unbound without Rich: {missing}"

    def test_importers_still_load_without_rich(self) -> None:
        """The real regression: a consumer doing a from-import."""
        with rich_unavailable():
            saved = sys.modules.pop("source_clone.progress_display", None)
            try:
                importlib.import_module("source_clone.progress_display")
            finally:
                sys.modules.pop("source_clone.progress_display", None)
                if saved is not None:
                    sys.modules["source_clone.progress_display"] = saved

    def test_reports_available_when_rich_is_installed(self) -> None:
        assert rich_optional.RICH_AVAILABLE is True
        unbound = [
            n for n in rich_optional.__all__ if getattr(rich_optional, n) is None
        ]
        assert not unbound, f"unexpectedly None with Rich present: {unbound}"
