# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2025 Matthew Watkins <mwatkins@linuxfoundation.org>

"""Unit tests for models module."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from gerrit_clone.models import (
    BatchResult,
    CloneResult,
    CloneStatus,
    Config,
    Project,
    ProjectState,
    RetryPolicy,
)


class TestProjectState:
    """Test ProjectState enum."""

    def test_project_state_values(self):
        """Test ProjectState enum values."""
        # Test all enum values in a single assertion to avoid unreachable code warnings
        expected_values = {
            ProjectState.ACTIVE: "ACTIVE",
            ProjectState.READ_ONLY: "READ_ONLY",
            ProjectState.HIDDEN: "HIDDEN",
        }
        for state, expected in expected_values.items():
            assert state == expected


class TestProject:
    """Test Project dataclass."""

    def test_project_creation(self):
        """Test basic project creation."""
        project = Project(
            name="test/project",
            state=ProjectState.ACTIVE,
            description="Test project",
        )

        assert project.name == "test/project"
        assert project.state == ProjectState.ACTIVE
        assert project.description == "Test project"
        assert project.web_links is None

    def test_project_is_active(self):
        """Test is_active property."""
        active_project = Project("test", ProjectState.ACTIVE)
        readonly_project = Project("test", ProjectState.READ_ONLY)

        assert active_project.is_active is True
        assert readonly_project.is_active is False

    def test_project_filesystem_path(self):
        """Test filesystem_path property."""
        project = Project("dir/subdir/repo", ProjectState.ACTIVE)
        expected_path = Path("dir/subdir/repo")

        assert project.filesystem_path == expected_path


class TestRetryPolicy:
    """Test RetryPolicy dataclass."""

    def test_retry_policy_defaults(self):
        """Test default retry policy values."""
        policy = RetryPolicy()

        assert policy.max_attempts == 3
        assert policy.base_delay == 2.0
        assert policy.factor == 2.0
        assert policy.max_delay == 30.0
        assert policy.jitter is True

    def test_retry_policy_custom(self):
        """Test custom retry policy values."""
        policy = RetryPolicy(
            max_attempts=5,
            base_delay=1.0,
            factor=1.5,
            max_delay=60.0,
            jitter=False,
        )

        assert policy.max_attempts == 5
        assert policy.base_delay == 1.0
        assert policy.factor == 1.5
        assert policy.max_delay == 60.0
        assert policy.jitter is False

    def test_retry_policy_validation(self):
        """Test retry policy validation."""
        with pytest.raises(ValueError, match="max_attempts must be at least 1"):
            RetryPolicy(max_attempts=0)

        with pytest.raises(ValueError, match="base_delay must be positive"):
            RetryPolicy(base_delay=-1.0)

        with pytest.raises(ValueError, match="factor must be at least 1"):
            RetryPolicy(factor=0.5)

        with pytest.raises(ValueError, match="max_delay must be >= base_delay"):
            RetryPolicy(base_delay=10.0, max_delay=5.0)


class TestConfig:
    """Test Config dataclass."""

    def test_config_minimal(self):
        """Test minimal config creation."""
        config = Config(host="gerrit.example.org")

        assert config.host == "gerrit.example.org"
        assert config.port == 29418
        assert config.base_url == "https://gerrit.example.org"
        assert config.ssh_user is None
        assert config.path_prefix == Path().resolve()
        assert config.skip_archived is True
        assert config.threads is None
        assert config.depth is None
        assert config.branch is None
        assert config.strict_host_checking is True
        assert config.clone_timeout == 600
        assert isinstance(config.retry_policy, RetryPolicy)
        assert config.manifest_filename == "clone-manifest.json"
        assert config.verbose is False
        assert config.quiet is False

    def test_config_custom(self):
        """Test custom config values."""
        retry_policy = RetryPolicy(max_attempts=5)
        config = Config(
            host="gerrit.example.org",
            port=22,
            base_url="https://custom.example.org",
            ssh_user="testuser",
            path_prefix=Path("/tmp/repos"),
            skip_archived=False,
            threads=8,
            depth=10,
            branch="main",
            strict_host_checking=False,
            clone_timeout=300,
            retry_policy=retry_policy,
            manifest_filename="manifest.json",
            verbose=True,
            quiet=False,
        )

        assert config.host == "gerrit.example.org"
        assert config.port == 22
        assert config.base_url == "https://custom.example.org"
        assert config.ssh_user == "testuser"
        assert config.path_prefix == Path("/tmp/repos").resolve()
        assert config.skip_archived is False
        assert config.threads == 8
        assert config.depth == 10
        assert config.branch == "main"
        assert config.strict_host_checking is False
        assert config.clone_timeout == 300
        assert config.retry_policy is retry_policy
        assert config.manifest_filename == "manifest.json"
        assert config.verbose is True
        assert config.quiet is False

    def test_config_validation(self):
        """Test config validation."""
        with pytest.raises(ValueError, match="host is required"):
            Config(host="")

        with pytest.raises(ValueError, match="port must be between 1 and 65535"):
            Config(host="test", port=0)

        with pytest.raises(ValueError, match="port must be between 1 and 65535"):
            Config(host="test", port=70000)

        with pytest.raises(ValueError, match="threads must be at least 1"):
            Config(host="test", threads=0)

        with pytest.raises(ValueError, match="depth must be at least 1"):
            Config(host="test", depth=0)

        with pytest.raises(ValueError, match="clone_timeout must be positive"):
            Config(host="test", clone_timeout=-1)

    def test_effective_threads(self):
        """Test effective_threads property."""
        # Explicit threads
        config = Config(host="test", threads=16)
        assert config.effective_threads == 16

        # Auto threads (should be reasonable)
        config = Config(host="test")
        threads = config.effective_threads
        assert isinstance(threads, int)
        assert threads > 0
        assert threads <= 32

    def test_projects_url(self):
        """Test projects_url property."""
        config = Config(host="gerrit.example.org")
        assert config.projects_url == "https://gerrit.example.org/projects/?d"

        config = Config(host="test", base_url="https://custom.example.org")
        assert config.projects_url == "https://custom.example.org/projects/?d"

    def test_git_ssh_command(self):
        """Test git_ssh_command property."""
        config = Config(host="test", strict_host_checking=True)
        assert (
            config.git_ssh_command
            == "ssh -o BatchMode=yes -o ConnectTimeout=10 -o ServerAliveInterval=5 -o ServerAliveCountMax=3 -o ConnectionAttempts=2 -o StrictHostKeyChecking=yes"
        )

        config = Config(host="test", strict_host_checking=False)
        assert (
            config.git_ssh_command
            == "ssh -o BatchMode=yes -o ConnectTimeout=10 -o ServerAliveInterval=5 -o ServerAliveCountMax=3 -o ConnectionAttempts=2 -o StrictHostKeyChecking=accept-new"
        )


class TestCloneResult:
    """Test CloneResult dataclass."""

    def test_clone_result_creation(self):
        """Test basic clone result creation."""
        project = Project("test", ProjectState.ACTIVE)
        path = Path("/tmp/test")
        started = datetime.now(UTC)

        result = CloneResult(
            project=project,
            status=CloneStatus.SUCCESS,
            path=path,
            attempts=1,
            duration_seconds=5.5,
            started_at=started,
        )

        assert result.project is project
        assert result.status == CloneStatus.SUCCESS
        assert result.path == path
        assert result.attempts == 1
        assert result.duration_seconds == 5.5
        assert result.started_at == started
        assert result.completed_at is None
        assert result.error_message is None

    def test_clone_result_properties(self):
        """Test clone result properties."""
        project = Project("test", ProjectState.ACTIVE)
        path = Path("/tmp/test")

        # Success
        success_result = CloneResult(project, CloneStatus.SUCCESS, path)
        assert success_result.success is True
        assert success_result.failed is False
        assert success_result.skipped is False

        # Failed
        failed_result = CloneResult(project, CloneStatus.FAILED, path)
        assert failed_result.success is False
        assert failed_result.failed is True
        assert failed_result.skipped is False

        # Skipped
        skipped_result = CloneResult(project, CloneStatus.SKIPPED, path)
        assert skipped_result.success is False
        assert skipped_result.failed is False
        assert skipped_result.skipped is True

        # Already exists (counts as success)
        exists_result = CloneResult(project, CloneStatus.ALREADY_EXISTS, path)
        assert exists_result.success is True
        assert exists_result.failed is False
        assert exists_result.skipped is False

    def test_clone_result_to_dict(self):
        """Test clone result serialization."""
        project = Project("test/repo", ProjectState.ACTIVE)
        path = Path("/tmp/test")
        started = datetime(2025, 1, 15, 12, 0, 0)
        completed = datetime(2025, 1, 15, 12, 0, 5)

        result = CloneResult(
            project=project,
            status=CloneStatus.SUCCESS,
            path=path,
            attempts=2,
            duration_seconds=5.123,
            error_message="Test error",
            started_at=started,
            completed_at=completed,
        )

        result_dict = result.to_dict()

        assert result_dict["project"] == "test/repo"
        assert result_dict["path"] == str(path)
        assert result_dict["status"] == "success"
        assert result_dict["attempts"] == 2
        assert result_dict["duration_s"] == 5.123
        assert result_dict["error"] == "Test error"
        assert result_dict["started_at"] == "2025-01-15T12:00:00"
        assert result_dict["completed_at"] == "2025-01-15T12:00:05"


class TestBatchResult:
    """Test BatchResult dataclass."""

    def test_batch_result_creation(self):
        """Test basic batch result creation."""
        config = Config(host="test")
        started = datetime.now(UTC)

        result = BatchResult(
            config=config,
            results=[],
            started_at=started,
        )

        assert result.config is config
        assert result.results == []
        assert result.started_at == started
        assert result.completed_at is None

    def test_batch_result_counts(self):
        """Test batch result count properties."""
        config = Config(host="test")
        project1 = Project("repo1", ProjectState.ACTIVE)
        project2 = Project("repo2", ProjectState.ACTIVE)
        project3 = Project("repo3", ProjectState.ACTIVE)
        project4 = Project("repo4", ProjectState.ACTIVE)
        path = Path("/tmp")

        results = [
            CloneResult(project1, CloneStatus.SUCCESS, path),
            CloneResult(project2, CloneStatus.FAILED, path),
            CloneResult(project3, CloneStatus.SKIPPED, path),
            CloneResult(project4, CloneStatus.ALREADY_EXISTS, path),
        ]

        batch_result = BatchResult(
            config=config,
            results=results,
            started_at=datetime.now(UTC),
        )

        assert batch_result.total_count == 4
        assert batch_result.success_count == 2  # SUCCESS + ALREADY_EXISTS
        assert batch_result.failed_count == 1
        assert batch_result.skipped_count == 1

    def test_batch_result_duration(self):
        """Test batch result duration calculation."""
        config = Config(host="test")
        started = datetime(2025, 1, 15, 12, 0, 0)
        completed = datetime(2025, 1, 15, 12, 0, 30)

        # Without completed_at
        result1 = BatchResult(config, [], started)
        assert result1.duration_seconds == 0.0

        # With completed_at
        result2 = BatchResult(config, [], started, completed)
        assert result2.duration_seconds == 30.0

    def test_batch_result_success_rate(self):
        """Test batch result success rate calculation."""
        config = Config(host="test")
        project1 = Project("repo1", ProjectState.ACTIVE)
        project2 = Project("repo2", ProjectState.ACTIVE)
        project3 = Project("repo3", ProjectState.ACTIVE)
        project4 = Project("repo4", ProjectState.ACTIVE)
        path = Path("/tmp")

        # Empty results
        empty_result = BatchResult(config, [], datetime.now(UTC))
        assert empty_result.success_rate == 0.0

        # Mixed results
        results = [
            CloneResult(project1, CloneStatus.SUCCESS, path),
            CloneResult(project2, CloneStatus.SUCCESS, path),
            CloneResult(project3, CloneStatus.FAILED, path),
            CloneResult(project4, CloneStatus.ALREADY_EXISTS, path),
        ]

        batch_result = BatchResult(config, results, datetime.now(UTC))
        assert batch_result.success_rate == 75.0  # 3 out of 4 successful

    def test_batch_result_to_dict(self):
        """Test batch result serialization."""
        config = Config(host="gerrit.example.org", port=29418)
        project = Project("test/repo", ProjectState.ACTIVE)
        path = Path("/tmp/test")

        result = CloneResult(
            project, CloneStatus.SUCCESS, path, attempts=1, duration_seconds=2.5
        )

        started = datetime(2025, 1, 15, 12, 0, 0)
        completed = datetime(2025, 1, 15, 12, 0, 10)

        batch_result = BatchResult(
            config=config,
            results=[result],
            started_at=started,
            completed_at=completed,
        )

        batch_dict = batch_result.to_dict()

        assert batch_dict["version"] == "1.0"
        assert batch_dict["host"] == "gerrit.example.org"
        assert batch_dict["port"] == 29418
        assert batch_dict["total"] == 1
        assert batch_dict["succeeded"] == 1
        assert batch_dict["failed"] == 0
        assert batch_dict["skipped"] == 0
        assert batch_dict["success_rate"] == 100.0
        assert batch_dict["duration_s"] == 10.0
        assert "config" in batch_dict
        assert "results" in batch_dict
        assert len(batch_dict["results"]) == 1

        # Check config subset
        config_subset = batch_dict["config"]
        assert config_subset["skip_archived"] is True
        assert config_subset["strict_host_checking"] is True
        assert config_subset["path_prefix"] == str(config.path_prefix)
