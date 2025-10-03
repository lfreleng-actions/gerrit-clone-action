# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2025 The Linux Foundation

"""Tests for dynamic log file naming changes."""

import os
import tempfile
from pathlib import Path
from unittest.mock import patch

from gerrit_clone.file_logging import get_default_log_path, init_logging


class TestDynamicLogFileNaming:
    """Test dynamic log file naming functionality."""

    def test_get_default_log_path_no_host(self) -> None:
        """Test default log path without host parameter."""
        log_path = get_default_log_path()
        assert log_path.name == "gerrit-clone.log"
        assert log_path.parent == Path.cwd()

    def test_get_default_log_path_with_host(self) -> None:
        """Test log path with hostname."""
        log_path = get_default_log_path("gerrit.example.org")
        assert log_path.name == "gerrit.example.org.log"
        assert log_path.parent == Path.cwd()

    def test_get_default_log_path_with_port(self) -> None:
        """Test log path strips port numbers."""
        test_cases = [
            ("gerrit.example.org:443", "gerrit.example.org.log"),
            ("gerrit.example.org:29418", "gerrit.example.org.log"),
            ("localhost:8080", "localhost.log"),
        ]

        for host, expected_name in test_cases:
            log_path = get_default_log_path(host)
            assert log_path.name == expected_name

    def test_get_default_log_path_sanitizes_special_chars(self) -> None:
        """Test log path sanitizes special characters."""
        test_cases = [
            ("host/with/path", "host_with_path.log"),
            (r"host\with\backslash", "host_with_backslash.log"),
            ("host:443/path", "host.log"),  # Port and path after colon are removed
        ]

        for host, expected_name in test_cases:
            log_path = get_default_log_path(host)
            assert log_path.name == expected_name

    def test_get_default_log_path_empty_host(self) -> None:
        """Test log path with empty host falls back to default."""
        test_cases = [None, "", "   "]

        for host in test_cases:
            log_path = get_default_log_path(host)
            assert log_path.name == "gerrit-clone.log"

    def test_init_logging_with_host_parameter(self) -> None:
        """Test init_logging creates log file with dynamic name."""
        with tempfile.TemporaryDirectory() as tmpdir:
            original_cwd = Path.cwd()
            try:
                # Change to temp directory for test
                os.chdir(tmpdir)

                # Initialize logging with host parameter
                _file_logger, _error_collector = init_logging(
                    disable_file=False,
                    host="gerrit.example.org",
                    quiet=True,  # Suppress console output during tests
                )

                # Check that log file was created with correct name
                expected_log_path = Path(tmpdir) / "gerrit.example.org.log"
                assert expected_log_path.exists()

                # Verify log file has header content
                content = expected_log_path.read_text()
                assert "GERRIT CLONE EXECUTION LOG" in content

            finally:
                os.chdir(original_cwd)

    def test_init_logging_without_host_uses_default(self) -> None:
        """Test init_logging without host uses default name."""
        with tempfile.TemporaryDirectory() as tmpdir:
            original_cwd = Path.cwd()
            try:
                os.chdir(tmpdir)

                # Initialize logging without host parameter
                _file_logger, _error_collector = init_logging(
                    disable_file=False,
                    quiet=True,
                )

                # Check that default log file was created
                expected_log_path = Path(tmpdir) / "gerrit-clone.log"
                assert expected_log_path.exists()

            finally:
                os.chdir(original_cwd)

    def test_init_logging_with_explicit_log_file_overrides_host(self) -> None:
        """Test explicit log file parameter overrides host-based naming."""
        with tempfile.TemporaryDirectory() as tmpdir:
            original_cwd = Path.cwd()
            try:
                os.chdir(tmpdir)

                custom_log_path = Path(tmpdir) / "custom.log"

                # Initialize logging with both explicit log file and host
                _file_logger, _error_collector = init_logging(
                    log_file=custom_log_path,
                    disable_file=False,
                    host="gerrit.example.org",
                    quiet=True,
                )

                # Check that explicit log file was used, not host-based name
                assert custom_log_path.exists()

                # Verify host-based log file was NOT created
                host_log_path = Path(tmpdir) / "gerrit.example.org.log"
                assert not host_log_path.exists()

            finally:
                os.chdir(original_cwd)

    def test_hostname_sanitization_edge_cases(self) -> None:
        """Test edge cases in hostname sanitization."""
        test_cases = [
            (
                "gerrit.very-long-hostname.example.org:443",
                "gerrit.very-long-hostname.example.org.log",
            ),
            ("192.168.1.1:8080", "192.168.1.1.log"),
            (
                "::1:8080",
                "gerrit-clone.log",
            ),  # IPv6 - first part before colon is empty, falls back to default
            ("host-with-dashes.example.org", "host-with-dashes.example.org.log"),
            ("host_with_underscores", "host_with_underscores.log"),
            ("UPPERCASE.EXAMPLE.ORG", "UPPERCASE.EXAMPLE.ORG.log"),
        ]

        for host, expected_name in test_cases:
            log_path = get_default_log_path(host)
            assert log_path.name == expected_name

    def test_log_file_naming_consistency(self) -> None:
        """Test that log file naming is consistent across calls."""
        host = "gerrit.example.org:443"

        # Call multiple times and ensure same result
        path1 = get_default_log_path(host)
        path2 = get_default_log_path(host)
        path3 = get_default_log_path(host)

        assert path1 == path2 == path3
        assert all(p.name == "gerrit.example.org.log" for p in [path1, path2, path3])

    def test_init_logging_host_parameter_propagation(self) -> None:
        """Test that host parameter is properly propagated through init_logging."""
        with tempfile.TemporaryDirectory() as tmpdir:
            original_cwd = Path.cwd()
            try:
                os.chdir(tmpdir)

                test_host = "test.gerrit.server.org:29418"

                # Mock get_default_log_path to verify it receives the host
                with patch(
                    "gerrit_clone.file_logging.get_default_log_path"
                ) as mock_get_log_path:
                    mock_log_path = Path(tmpdir) / "test.log"
                    mock_get_log_path.return_value = mock_log_path

                    init_logging(
                        disable_file=False,
                        host=test_host,
                        quiet=True,
                    )

                    # Verify get_default_log_path was called with the host
                    mock_get_log_path.assert_called_once_with(test_host)

            finally:
                os.chdir(original_cwd)
