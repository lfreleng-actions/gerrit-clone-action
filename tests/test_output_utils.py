# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2025 Matthew Watkins <mwatkins@linuxfoundation.org>

"""Unit tests for output_utils module."""

from __future__ import annotations

import logging
from io import StringIO
from unittest.mock import patch

from rich.console import Console
from rich.table import Table

from source_clone.output_utils import format_rate_limit_table, log_and_print


class TestLogAndPrint:
    """Tests for the log_and_print dual-channel output function."""

    def test_log_and_print_info_level(self) -> None:
        """Test that info level logs and prints correctly."""
        stream = StringIO()
        handler = logging.StreamHandler(stream)
        handler.setLevel(logging.DEBUG)
        logger = logging.getLogger("test_info_level")
        logger.handlers.clear()
        logger.addHandler(handler)
        logger.setLevel(logging.DEBUG)

        console = Console(file=StringIO(), width=80)
        log_and_print(logger, console, "Test message", level="info")

        log_output = stream.getvalue()
        assert "Test message" in log_output

    def test_log_and_print_debug_level(self) -> None:
        """Test that debug level logs the message at debug severity."""
        stream = StringIO()
        handler = logging.StreamHandler(stream)
        handler.setLevel(logging.DEBUG)
        logger = logging.getLogger("test_debug_level")
        logger.handlers.clear()
        logger.addHandler(handler)
        logger.setLevel(logging.DEBUG)

        console = Console(file=StringIO(), width=80)
        log_and_print(logger, console, "Debug message", level="debug")

        log_output = stream.getvalue()
        assert "Debug message" in log_output

    def test_log_and_print_warning_level(self) -> None:
        """Test that warning level logs the message at warning severity."""
        stream = StringIO()
        handler = logging.StreamHandler(stream)
        handler.setLevel(logging.DEBUG)
        logger = logging.getLogger("test_warning_level")
        logger.handlers.clear()
        logger.addHandler(handler)
        logger.setLevel(logging.DEBUG)

        console = Console(file=StringIO(), width=80)
        log_and_print(logger, console, "Warning message", level="warning")

        log_output = stream.getvalue()
        assert "Warning message" in log_output

    def test_log_and_print_error_level(self) -> None:
        """Test that error level logs the message at error severity."""
        stream = StringIO()
        handler = logging.StreamHandler(stream)
        handler.setLevel(logging.DEBUG)
        logger = logging.getLogger("test_error_level")
        logger.handlers.clear()
        logger.addHandler(handler)
        logger.setLevel(logging.DEBUG)

        console = Console(file=StringIO(), width=80)
        log_and_print(logger, console, "Error message", level="error")

        log_output = stream.getvalue()
        assert "Error message" in log_output

    def test_log_and_print_with_style(self) -> None:
        """Test that when style is provided, console.print is used."""
        stream = StringIO()
        handler = logging.StreamHandler(stream)
        logger = logging.getLogger("test_with_style")
        logger.handlers.clear()
        logger.addHandler(handler)
        logger.setLevel(logging.DEBUG)

        console_output = StringIO()
        console = Console(file=console_output, width=80, force_terminal=False)

        with patch("builtins.print") as mock_print:
            log_and_print(
                logger, console, "Styled message", style="bold red", level="info"
            )
            mock_print.assert_not_called()

        console_text = console_output.getvalue()
        assert "Styled message" in console_text

    def test_log_and_print_without_style(self) -> None:
        """Test that when style is None, console.print is used."""
        stream = StringIO()
        handler = logging.StreamHandler(stream)
        logger = logging.getLogger("test_without_style")
        logger.handlers.clear()
        logger.addHandler(handler)
        logger.setLevel(logging.DEBUG)

        console_stream = StringIO()
        console = Console(file=console_stream, width=80, force_terminal=False)

        log_and_print(logger, console, "Plain message", level="info")

        console_text = console_stream.getvalue()
        assert "Plain message" in console_text

    def test_log_and_print_default_level(self) -> None:
        """Test that the default level is info."""
        stream = StringIO()
        handler = logging.StreamHandler(stream)
        handler.setLevel(logging.DEBUG)
        formatter = logging.Formatter("%(levelname)s:%(message)s")
        handler.setFormatter(formatter)
        logger = logging.getLogger("test_default_level")
        logger.handlers.clear()
        logger.addHandler(handler)
        logger.setLevel(logging.DEBUG)

        console = Console(file=StringIO(), width=80)
        log_and_print(logger, console, "Default level message")

        log_output = stream.getvalue()
        assert "INFO" in log_output
        assert "Default level message" in log_output

    def test_log_and_print_invalid_level_fallback(self) -> None:
        """Test that an invalid level falls back to logger.info."""
        stream = StringIO()
        handler = logging.StreamHandler(stream)
        handler.setLevel(logging.DEBUG)
        formatter = logging.Formatter("%(levelname)s:%(message)s")
        handler.setFormatter(formatter)
        logger = logging.getLogger("test_invalid_level")
        logger.handlers.clear()
        logger.addHandler(handler)
        logger.setLevel(logging.DEBUG)

        console = Console(file=StringIO(), width=80)
        log_and_print(logger, console, "Fallback message", level="nonexistent")

        log_output = stream.getvalue()
        assert "INFO" in log_output
        assert "Fallback message" in log_output


class TestFormatRateLimitTable:
    """Tests for the format_rate_limit_table Rich table builder."""

    def test_basic_rate_limit_table(self) -> None:
        """Test table creation with standard rate-limit headers."""
        rate_info = {
            "X-RateLimit-Remaining": "42",
            "X-RateLimit-Limit": "60",
            "X-RateLimit-Used": "18",
            "X-RateLimit-Resource": "core",
            "X-RateLimit-Reset": "1700000000",
        }
        table = format_rate_limit_table(rate_info)

        assert isinstance(table, Table)
        assert table.title == "⚠️ API Error Diagnostics"
        assert table.row_count > 0

    def test_rate_limit_table_with_status(self) -> None:
        """Test that response_status and response_body appear in output."""
        rate_info = {
            "X-RateLimit-Remaining": "0",
            "X-RateLimit-Limit": "60",
        }
        table = format_rate_limit_table(
            rate_info,
            response_status=403,
            response_body="rate limit exceeded",
        )

        assert isinstance(table, Table)

        # Render the table to a string and verify contents
        console_output = StringIO()
        console = Console(file=console_output, width=120, force_terminal=False)
        console.print(table)
        rendered = console_output.getvalue()

        assert "403" in rendered
        assert "rate limit exceeded" in rendered

    def test_rate_limit_table_with_empty_info(self) -> None:
        """Test that an empty dict still produces a valid table."""
        table = format_rate_limit_table({})

        assert isinstance(table, Table)
        assert table.row_count == 0
        assert table.title == "⚠️ API Error Diagnostics"

    def test_rate_limit_table_truncates_body(self) -> None:
        """Test that response_body longer than 200 chars is truncated."""
        rate_info: dict[str, str] = {}
        long_body = "x" * 300

        table = format_rate_limit_table(
            rate_info,
            response_body=long_body,
        )

        # Render the table to inspect the truncated output
        console_output = StringIO()
        console = Console(file=console_output, width=400, force_terminal=False)
        console.print(table)
        rendered = console_output.getvalue()

        # The full 300-char body should NOT appear; it should be truncated
        assert long_body not in rendered
        # The truncated version (200 chars + ellipsis) should be present
        assert "x" * 200 in rendered
        assert "…" in rendered

    def test_zero_remaining_preserved(self) -> None:
        """Test that a '0' remaining value is not replaced by '?'."""
        rate_info = {
            "X-RateLimit-Remaining": "0",
            "X-RateLimit-Limit": "5000",
        }
        table = format_rate_limit_table(rate_info)

        console_output = StringIO()
        console = Console(file=console_output, width=400, force_terminal=False)
        console.print(table)
        rendered = console_output.getvalue()

        assert "0 / 5000" in rendered
        assert "?" not in rendered

    def test_retry_after_numeric_gets_suffix(self) -> None:
        """Test that a numeric Retry-After value gets an 's' suffix."""
        rate_info = {"Retry-After": "120"}
        table = format_rate_limit_table(rate_info)

        console_output = StringIO()
        console = Console(file=console_output, width=400, force_terminal=False)
        console.print(table)
        rendered = console_output.getvalue()

        assert "120s" in rendered

    def test_retry_after_float_gets_suffix(self) -> None:
        """Test that a float Retry-After value gets an 's' suffix."""
        rate_info = {"Retry-After": "30.5"}
        table = format_rate_limit_table(rate_info)

        console_output = StringIO()
        console = Console(file=console_output, width=400, force_terminal=False)
        console.print(table)
        rendered = console_output.getvalue()

        assert "30.5s" in rendered

    def test_retry_after_http_date_no_suffix(self) -> None:
        """Test that an HTTP-date Retry-After is shown without 's' suffix."""
        rate_info = {"Retry-After": "Thu, 01 Jan 2026 00:00:00 GMT"}
        table = format_rate_limit_table(rate_info)

        console_output = StringIO()
        console = Console(file=console_output, width=400, force_terminal=False)
        console.print(table)
        rendered = console_output.getvalue()

        assert "Thu, 01 Jan 2026 00:00:00 GMT" in rendered
        assert "GMTs" not in rendered
