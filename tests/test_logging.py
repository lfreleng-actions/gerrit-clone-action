# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2025 Matthew Watkins <mwatkins@linuxfoundation.org>

"""Unit tests for logging module."""

from __future__ import annotations

import logging
import time
from io import StringIO

from rich.console import Console

from gerrit_clone.logging import (
    GerritRichHandler,
    get_logger,
    setup_logging,
)


class TestGerritRichHandler:
    """Test GerritRichHandler custom logging handler."""

    def test_gerrit_rich_handler_creation(self) -> None:
        """Test basic GerritRichHandler creation."""
        handler = GerritRichHandler()
        assert isinstance(handler, logging.Handler)
        assert hasattr(handler, "console")

    def test_gerrit_rich_handler_with_console(self) -> None:
        """Test GerritRichHandler with custom console."""
        custom_console = Console(file=StringIO(), width=80)
        handler = GerritRichHandler(console=custom_console)
        assert handler.console is custom_console

    def test_gerrit_rich_handler_with_kwargs(self) -> None:
        """Test GerritRichHandler with additional kwargs."""
        handler = GerritRichHandler(show_time=False, show_level=True, show_path=False)
        assert isinstance(handler, logging.Handler)

    def test_gerrit_rich_handler_formatting(self) -> None:
        """Test that GerritRichHandler formats messages correctly."""
        output = StringIO()
        console = Console(file=output, width=80, force_terminal=False)
        handler = GerritRichHandler(console=console, show_time=False)

        logger = logging.Logger("test")
        logger.addHandler(handler)
        logger.setLevel(logging.INFO)

        # Test different log levels
        logger.info("Test info message")
        logger.warning("Test warning message")
        logger.error("Test error message")

        output_text = output.getvalue()
        assert "Test info message" in output_text
        assert "Test warning message" in output_text
        assert "Test error message" in output_text


class TestGetLogger:
    """Test get_logger function."""

    def test_get_logger_basic(self) -> None:
        """Test basic logger creation."""
        logger = get_logger("test_module")
        assert isinstance(logger, logging.Logger)
        assert logger.name == "test_module"

    def test_get_logger_same_name_returns_same_instance(self) -> None:
        """Test that get_logger returns the same instance for same name."""
        logger1 = get_logger("test_module")
        logger2 = get_logger("test_module")
        assert logger1 is logger2

    def test_get_logger_different_names(self) -> None:
        """Test that different names return different loggers."""
        logger1 = get_logger("module1")
        logger2 = get_logger("module2")
        assert logger1 is not logger2
        assert logger1.name == "module1"
        assert logger2.name == "module2"

    def test_get_logger_with_package_name(self) -> None:
        """Test get_logger with package-style names."""
        logger = get_logger("gerrit_clone.test")
        assert logger.name == "gerrit_clone.test"


class TestSetupLogging:
    """Test setup_logging function."""

    def test_setup_logging_basic(self) -> None:
        """Test basic logging setup."""
        # Test that setup_logging doesn't raise exceptions
        setup_logging(verbose=False, quiet=False)
        setup_logging(verbose=True, quiet=False)
        setup_logging(verbose=False, quiet=True)

    def test_setup_logging_with_console(self) -> None:
        """Test setup_logging with custom console."""
        custom_console = Console(file=StringIO(), width=80)
        # Should not raise exception
        setup_logging(verbose=False, quiet=False, console=custom_console)


class TestLoggingIntegration:
    """Integration tests for logging functionality."""

    def test_end_to_end_logging_setup_and_use(self) -> None:
        """Test complete logging setup and usage."""
        output = StringIO()
        console = Console(file=output, width=80, force_terminal=False)

        # Setup logging
        setup_logging(verbose=True, quiet=False, console=console)

        # Get a logger and use it
        logger = get_logger("test_integration")
        logger.debug("Debug message")
        logger.info("Info message")
        logger.warning("Warning message")
        logger.error("Error message")

        output_text = output.getvalue()

        # Should have some output
        assert len(output_text) > 0

    def test_logger_hierarchy(self) -> None:
        """Test that logger hierarchy works correctly."""
        # Get loggers at different levels
        parent_logger = get_logger("gerrit_clone")
        child_logger = get_logger("gerrit_clone.api")
        grandchild_logger = get_logger("gerrit_clone.api.client")

        # All should be different instances
        assert parent_logger is not child_logger
        assert child_logger is not grandchild_logger

    def test_logging_performance(self) -> None:
        """Test that logging setup and usage is reasonably performant."""

        output = StringIO()
        console = Console(file=output, width=80, force_terminal=False)

        # Time the setup
        start_time = time.time()
        setup_logging(verbose=True, quiet=False, console=console)
        setup_time = time.time() - start_time

        # Setup should be fast
        assert setup_time < 1.0  # Should take less than 1 second
