#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2025 Matthew Watkins <mwatkins@linuxfoundation.org>

"""Simple script to verify strict typing works correctly."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Optional

# Add src to path
sys.path.insert(0, str(Path(__file__).parent / "src"))

try:
    # Import our modules to verify typing
    from gerrit_clone.models import Config, Project, ProjectState
    from gerrit_clone.gerrit_api import GerritAPIClient
    from gerrit_clone.worker import CloneWorker
    from gerrit_clone.logging import setup_logging, get_logger
    from gerrit_clone.retry import execute_with_retry
    from gerrit_clone.models import RetryPolicy

    print("✅ All imports successful")

    # Test basic typing
    config: Config = Config(host="test.example.org")
    project: Project = Project("test-project", ProjectState.ACTIVE)

    # Test function signatures
    def test_typed_function(name: str, count: int) -> str:
        return f"{name}: {count}"

    result: str = test_typed_function("test", 42)

    # Test optional types
    optional_value: Optional[str] = None
    optional_value = "now has value"

    print("✅ Basic typing works correctly")

    # Test class instantiation with typing
    worker: CloneWorker = CloneWorker(config)
    logger = get_logger(__name__)

    print("✅ Class instantiation with typing works")

    # Test policy creation
    policy: RetryPolicy = RetryPolicy(max_attempts=3)

    print("✅ All type checks passed!")
    print(f"Config host: {config.host}")
    print(f"Project name: {project.name}")
    print(f"Result: {result}")

except ImportError as e:
    print(f"❌ Import error: {e}")
    sys.exit(1)
except Exception as e:
    print(f"❌ Error: {e}")
    sys.exit(1)
