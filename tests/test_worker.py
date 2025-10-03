# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2025 Matthew Watkins <mwatkins@linuxfoundation.org>

"""Unit tests for worker module."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from unittest.mock import Mock

from gerrit_clone.models import CloneStatus, Config, Project, ProjectState
from gerrit_clone.worker import CloneWorker


class TestCloneWorker:
    """Test CloneWorker class."""

    def test_worker_initialization(self) -> None:
        """Test worker initialization."""
        config = Config(host="gerrit.example.org")
        worker = CloneWorker(config)

        assert worker.config is config

    def test_clone_project_basic(self) -> None:
        """Test basic clone project functionality."""
        project = Project("test-project", ProjectState.ACTIVE)
        config = Config(host="gerrit.example.org")
        worker = CloneWorker(config)

        result = worker.clone_project(project)

        assert result.project is project
        assert isinstance(result.status, CloneStatus)
        assert isinstance(result.path, Path)

    def test_clone_project_timing(self) -> None:
        """Test that clone results include timing information."""
        project = Project("timing-project", ProjectState.ACTIVE)
        config = Config(host="gerrit.example.org")
        worker = CloneWorker(config)

        result = worker.clone_project(project)

        assert isinstance(result.started_at, datetime)
        assert isinstance(result.duration_seconds, float)
        assert result.duration_seconds >= 0

    def test_build_ssh_url_basic(self) -> None:
        """Test SSH URL building."""
        config = Config(host="gerrit.example.org")
        worker = CloneWorker(config)
        project = Project("test-project", ProjectState.ACTIVE)

        url = worker._build_ssh_url(project)

        assert url == "ssh://gerrit.example.org:29418/test-project"

    def test_build_ssh_url_with_user(self) -> None:
        """Test SSH URL with username."""
        config = Config(host="gerrit.example.org", ssh_user="testuser")
        worker = CloneWorker(config)
        project = Project("group/project", ProjectState.ACTIVE)

        url = worker._build_ssh_url(project)

        assert url == "ssh://testuser@gerrit.example.org:29418/group/project"

    def test_build_ssh_url_custom_port(self) -> None:
        """Test SSH URL with custom port."""
        config = Config(host="gerrit.example.org", port=2222)
        worker = CloneWorker(config)
        project = Project("test-project", ProjectState.ACTIVE)

        url = worker._build_ssh_url(project)

        assert url == "ssh://gerrit.example.org:2222/test-project"

    def test_format_duration(self) -> None:
        """Test duration formatting."""
        config = Config(host="gerrit.example.org")
        worker = CloneWorker(config)

        # Test various durations
        assert "0.5s" in worker._format_duration(0.5)
        assert "1.2s" in worker._format_duration(1.23)
        assert "1m" in worker._format_duration(65.0)
        assert "1h" in worker._format_duration(3665.0)

    def test_analyze_clone_error_permission_denied(self) -> None:
        """Test error analysis for permission denied."""
        config = Config(host="gerrit.example.org")
        worker = CloneWorker(config)

        # Mock CompletedProcess with permission denied error
        mock_result = Mock()
        mock_result.stderr = "Permission denied (publickey)."
        mock_result.stdout = ""
        mock_result.returncode = 128

        result = worker._analyze_clone_error(mock_result, "test-project")

        assert "Permission denied" in result
        assert "SSH auth failed" in result

    def test_analyze_clone_error_host_key_verification(self) -> None:
        """Test error analysis for host key verification."""
        config = Config(host="gerrit.example.org")
        worker = CloneWorker(config)

        # Mock CompletedProcess with host key error
        mock_result = Mock()
        mock_result.stderr = "Host key verification failed."
        mock_result.stdout = ""
        mock_result.returncode = 128

        result = worker._analyze_clone_error(mock_result, "test-project")

        assert "Host key verification failed" in result
        assert "known_hosts" in result
        assert "ssh-keyscan" in result

    def test_analyze_clone_error_connection_refused(self) -> None:
        """Test error analysis for connection refused."""
        config = Config(host="gerrit.example.org")
        worker = CloneWorker(config)

        # Mock CompletedProcess with connection refused error
        mock_result = Mock()
        mock_result.stderr = (
            "ssh: connect to host gerrit.example.org port 22: Connection refused"
        )
        mock_result.stdout = ""
        mock_result.returncode = 255

        result = worker._analyze_clone_error(mock_result, "test-project")

        assert "Connection refused" in result
        assert "SSH service is running" in result
        assert "gerrit.example.org:29418" in result

    def test_analyze_clone_error_hostname_resolution(self) -> None:
        """Test error analysis for hostname resolution."""
        config = Config(host="gerrit.example.org")
        worker = CloneWorker(config)

        # Mock CompletedProcess with hostname resolution error
        mock_result = Mock()
        mock_result.stderr = "ssh: Could not resolve hostname gerrit.example.org"
        mock_result.stdout = ""
        mock_result.returncode = 255

        result = worker._analyze_clone_error(mock_result, "test-project")

        assert "DNS resolution failed" in result
        assert "cannot resolve" in result
        assert "gerrit.example.org" in result

    def test_analyze_clone_error_generic(self) -> None:
        """Test error analysis for generic errors."""
        config = Config(host="gerrit.example.org")
        worker = CloneWorker(config)

        # Mock CompletedProcess with generic error
        mock_result = Mock()
        mock_result.stderr = "Some unknown error occurred"
        mock_result.stdout = ""
        mock_result.returncode = 1

        result = worker._analyze_clone_error(mock_result, "test-project")

        assert "Some unknown error occurred" in result

    def test_is_retryable_clone_error_retryable(self) -> None:
        """Test retryable error detection."""
        config = Config(host="gerrit.example.org")
        worker = CloneWorker(config)

        retryable_errors = [
            "Connection timeout",
            "ssh: connect to host: Connection refused",
            "Network is unreachable",
            "Temporary failure in name resolution",
            "early EOF",
            "The remote end hung up unexpectedly",
            "transfer closed",
            "RPC failed",
            "could not resolve hostname",
        ]

        for error in retryable_errors:
            # Mock CompletedProcess with retryable error
            mock_result = Mock()
            mock_result.stderr = error
            mock_result.stdout = ""
            mock_result.returncode = 128

            assert worker._is_retryable_clone_error(mock_result) is True

    def test_is_retryable_clone_error_non_retryable(self) -> None:
        """Test non-retryable error detection."""
        config = Config(host="gerrit.example.org")
        worker = CloneWorker(config)

        non_retryable_errors = [
            "Permission denied",
            "Host key verification failed",
            "fatal: repository 'test' does not exist",
            "Authentication failed",
            "Access denied",
        ]

        for error in non_retryable_errors:
            # Mock CompletedProcess with non-retryable error
            mock_result = Mock()
            mock_result.stderr = error
            mock_result.stdout = ""
            mock_result.returncode = 128

            assert worker._is_retryable_clone_error(mock_result) is False

    def test_worker_with_different_configs(self) -> None:
        """Test worker behavior with different configurations."""
        configs = [
            Config(host="gerrit1.example.org"),
            Config(host="gerrit2.example.org", port=2222, ssh_user="user"),
            Config(host="gerrit3.example.org", depth=1, branch="develop"),
        ]

        for config in configs:
            worker = CloneWorker(config)
            assert worker.config is config

            project = Project("test-project", ProjectState.ACTIVE)
            url = worker._build_ssh_url(project)
            assert config.host in url
            assert str(config.port) in url
